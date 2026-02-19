from dotenv import load_dotenv
import os
import ssl
import ftplib
from pybambu.const import SPEED_PROFILE, FILAMENT_NAMES, CURRENT_STAGE_IDS
import paho.mqtt.client as mqtt
import json
import threading
import io
import zipfile
import re
from datetime import datetime, timedelta
import time
import socket
import subprocess
import signal
import sys
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

try:
    from bambulab import (
        BambuAuthenticator,
        BambuAuthError,
        BambuClient as CloudApiClient,
        BambuAPIError,
    )
except ImportError:
    BambuAuthenticator = None
    BambuAuthError = Exception
    CloudApiClient = None
    BambuAPIError = Exception

# Load environment variables
load_dotenv()

# Additional global variable to track the first run
is_first_run = True

subprocesses = []  # List to keep track of subprocesses

# Retrieve environment variables
REGION = os.getenv('REGION')
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')
USERNAME = os.getenv('USERNAME')
PRINTER_SN = os.getenv('PRINTER_SN')
PRINTER_IP = os.getenv('PRINTER_IP')
ACCESS_CODE = os.getenv('ACCESS_CODE')
def resolve_base_dir():
    configured = os.getenv('BASE_DIR') or 'data'
    repo_root = Path(__file__).resolve().parent.parent
    if os.path.isabs(configured):
        return configured

    repo_candidate = (repo_root / configured).resolve()
    if repo_candidate.is_dir():
        return str(repo_candidate)

    cwd_candidate = Path(os.path.abspath(configured))
    if cwd_candidate.is_dir():
        return str(cwd_candidate)

    return str(repo_candidate)

BASE_DIR = resolve_base_dir()
# Optional pre-fetched cloud auth token to bypass password/2FA login flows
CLOUD_AUTH_TOKEN = os.getenv('BAMBU_CLOUD_TOKEN')
# Enable message dumps only when explicitly set
DEBUG_DUMPS = os.getenv('DEBUG_DUMPS', '').lower() in ('1', 'true', 'yes', 'on', 'debug')

# Define the path for persisted files in the data subdirectory
DUMPS_FILE_PATH = os.path.join(BASE_DIR, 'ConnectionDumps.json')
TASK_ID_FILE_PATH = os.path.join(BASE_DIR, 'latest_task_id.txt')
WRITER_STATUS_FILE_PATH = os.path.join(BASE_DIR, 'writerStatus.json')
PRINT_COVER_IMAGE_PATH = os.path.join(BASE_DIR, 'printCover.png')
PRINT_COVER_LEGACY_PATH = os.path.join(BASE_DIR, 'printcover.png')
CONNECTION_DUMPS_MAX_ENTRIES = 50
connection_dumps = deque(maxlen=CONNECTION_DUMPS_MAX_ENTRIES)
writer_status_lock = threading.Lock()
sd_preview_lock = threading.Lock()
latest_print_data = {}
sd_preview_attempt_state = {"job_key": None, "attempted_at": 0.0, "success": False}
SD_PREVIEW_RETRY_SECONDS = int(os.getenv('SD_PREVIEW_RETRY_SECONDS', '45'))
SD_PREVIEW_ARCHIVE_SUFFIX = '.3mf'
SD_PREVIEW_PRIORITY_MEMBERS = (
    'Metadata/plate_1.png',
    'Metadata/plate_1_small.png',
    'Metadata/plate_no_light_1.png',
    'Metadata/top_1.png',
    'Metadata/pick_1.png'
)
writer_status = {
    "started_at": datetime.utcnow().isoformat() + "Z",
    "pid": os.getpid(),
    "base_dir": BASE_DIR,
    "state": "initializing",
    "mqtt_message_count": 0,
    "write_count": 0
}

def update_writer_status(**fields):
    """Persist lightweight runtime diagnostics for writer-side troubleshooting."""
    with writer_status_lock:
        writer_status.update(fields)
        writer_status["updated_at"] = datetime.utcnow().isoformat() + "Z"
        try:
            with open(WRITER_STATUS_FILE_PATH, 'w', encoding='utf-8') as status_file:
                json.dump(writer_status, status_file, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"Failed to persist writer status: {exc}")

class ImplicitReusedFTP_TLS(ftplib.FTP_TLS):
    """Implicit FTPS client with TLS session reuse for printer compatibility."""

    def connect(self, host='', port=0, timeout=-999):
        if host:
            self.host = host
        if port:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), self.timeout, source_address=self.source_address)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

def _strip_print_suffixes(value: str) -> str:
    name = str(value or '').strip().replace('\\', '/').split('/')[-1]
    lowered = name.lower()
    for suffix in ('.gcode.3mf', '.3mf', '.gcode'):
        if lowered.endswith(suffix):
            return name[:-len(suffix)]
    return os.path.splitext(name)[0]

def _normalize_preview_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())

def _extract_preview_name_candidates(task_data=None, print_data=None):
    candidates = []
    seen = set()
    ignored_basenames = {'', 'plate_1', 'plate_1_small', 'plate_no_light_1', 'top_1', 'pick_1'}
    source_pairs = (
        (task_data or {}, ('designTitle', 'title', 'modelId')),
        (print_data or {}, ('subtask_name', 'task_name', 'project_name', 'gcode_file', 'file'))
    )

    for data, fields in source_pairs:
        for field in fields:
            value = str(data.get(field, '')).strip()
            if not value or value in ('N/A', 'Unknown'):
                continue
            stripped = _strip_print_suffixes(value).strip()
            if stripped.lower() in ignored_basenames:
                continue
            if stripped not in seen:
                seen.add(stripped)
                candidates.append(stripped)

    return candidates

def _parse_ftp_list_line(line: str):
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    mode, size_text, month, day, time_or_year, name = parts[0], parts[4], parts[5], parts[6], parts[7], parts[8]
    mtime = None
    try:
        if ':' in time_or_year:
            parsed = datetime.strptime(f"{month} {day} {time_or_year}", "%b %d %H:%M")
            mtime = parsed.replace(year=datetime.utcnow().year)
        else:
            mtime = datetime.strptime(f"{month} {day} {time_or_year}", "%b %d %Y")
    except ValueError:
        pass

    try:
        size = int(size_text)
    except ValueError:
        size = 0

    return {
        'name': name,
        'is_dir': mode.startswith('d'),
        'size': size,
        'mtime': mtime
    }

def _list_printer_3mf_archives(ftp_client):
    lines = []
    ftp_client.retrlines('LIST', lines.append)
    entries = []
    for line in lines:
        parsed = _parse_ftp_list_line(line)
        if not parsed or parsed['is_dir']:
            continue
        if parsed['name'].lower().endswith(SD_PREVIEW_ARCHIVE_SUFFIX):
            entries.append(parsed)
    entries.sort(key=lambda item: (item.get('mtime') or datetime.min, item.get('size', 0)), reverse=True)
    return entries

def _score_archive_name(archive_name: str, normalized_candidates: list[str]) -> int:
    if not normalized_candidates:
        return 0
    normalized_archive = _normalize_preview_name(_strip_print_suffixes(archive_name))
    best_score = 0
    for candidate in normalized_candidates:
        if not candidate:
            continue
        if normalized_archive == candidate:
            best_score = max(best_score, 100)
        elif normalized_archive.startswith(candidate) or candidate.startswith(normalized_archive):
            best_score = max(best_score, 80)
        elif candidate in normalized_archive:
            best_score = max(best_score, 60)
    if archive_name.lower().endswith('.gcode.3mf'):
        best_score += 5
    return best_score

def _select_best_archive(entries: list[dict], normalized_candidates: list[str]):
    if not entries:
        return None, 'none'

    scored_entries = []
    for entry in entries:
        score = _score_archive_name(entry['name'], normalized_candidates)
        scored_entries.append((score, entry))
    scored_entries.sort(key=lambda item: (item[0], item[1].get('mtime') or datetime.min, item[1].get('size', 0)), reverse=True)

    best_score, best_entry = scored_entries[0]
    if normalized_candidates and best_score > 0:
        return best_entry, 'name_match'
    return entries[0], 'latest_file'

def _download_ftp_file_bytes(ftp_client, remote_name: str) -> bytes:
    buffer = io.BytesIO()
    ftp_client.retrbinary(f"RETR {remote_name}", buffer.write)
    return buffer.getvalue()

def _extract_preview_bytes_from_3mf(archive_bytes: bytes):
    with zipfile.ZipFile(io.BytesIO(archive_bytes), 'r') as archive:
        members = archive.namelist()
        for preferred in SD_PREVIEW_PRIORITY_MEMBERS:
            if preferred in members:
                return archive.read(preferred), preferred
        image_members = [
            member for member in members
            if member.lower().startswith('metadata/') and member.lower().endswith(('.png', '.jpg', '.jpeg'))
        ]
        if image_members:
            image_members.sort(key=lambda member: (0 if 'plate_1' in member.lower() else 1, len(member)))
            selected = image_members[0]
            return archive.read(selected), selected
    return None, None

def clear_print_cover_files():
    removed = False
    for path in (PRINT_COVER_IMAGE_PATH, PRINT_COVER_LEGACY_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed = True
        except OSError as exc:
            print(f"Failed to remove stale cover file {path}: {exc}")
    if removed:
        update_writer_status(preview_source='none', preview_image_cleared_at=datetime.utcnow().isoformat() + "Z")
    return removed

def save_print_cover_image(image_bytes: bytes, source_label: str):
    if not image_bytes:
        return False
    try:
        with open(PRINT_COVER_IMAGE_PATH, 'wb') as cover_file:
            cover_file.write(image_bytes)
        update_writer_status(
            preview_source=source_label,
            preview_image_size=len(image_bytes),
            preview_image_updated_at=datetime.utcnow().isoformat() + "Z"
        )
        return True
    except OSError as exc:
        update_writer_status(
            last_error=f"Failed to save print cover: {exc}",
            last_error_at=datetime.utcnow().isoformat() + "Z"
        )
        print(f"Failed to save print cover image: {exc}")
        return False

def _build_preview_job_key(task_data=None, print_data=None):
    key_parts = []
    for data in (task_data or {}, print_data or {}):
        for field in ('id', 'task_id', 'title', 'designTitle', 'subtask_name', 'gcode_file', 'file'):
            value = str(data.get(field, '')).strip()
            if value and value not in ('N/A', 'Unknown'):
                key_parts.append(value)
    if not key_parts:
        key_parts.append("unknown_job")
    return '|'.join(key_parts)

def _should_attempt_sd_preview(job_key: str, force: bool):
    now = time.time()
    with sd_preview_lock:
        last_key = sd_preview_attempt_state.get('job_key')
        last_attempt = float(sd_preview_attempt_state.get('attempted_at', 0.0))
        if force or job_key != last_key:
            sd_preview_attempt_state['job_key'] = job_key
            sd_preview_attempt_state['attempted_at'] = now
            sd_preview_attempt_state['success'] = False
            return True
        if now - last_attempt >= SD_PREVIEW_RETRY_SECONDS:
            sd_preview_attempt_state['attempted_at'] = now
            return True
    return False

def _record_sd_preview_result(job_key: str, success: bool):
    with sd_preview_lock:
        sd_preview_attempt_state['job_key'] = job_key
        sd_preview_attempt_state['attempted_at'] = time.time()
        sd_preview_attempt_state['success'] = success

def _get_last_sd_preview_job_key():
    with sd_preview_lock:
        return sd_preview_attempt_state.get('job_key')

def attempt_sdcard_preview_fallback(task_data=None, print_data=None, force=False):
    """Try to populate printCover.png from printer SD card .3mf metadata thumbnails."""
    job_key = _build_preview_job_key(task_data=task_data, print_data=print_data)
    if not _should_attempt_sd_preview(job_key, force=force):
        return False

    if not PRINTER_IP or not ACCESS_CODE:
        update_writer_status(
            sd_preview_status='skipped',
            sd_preview_reason='Missing PRINTER_IP or ACCESS_CODE',
            sd_preview_job_key=job_key
        )
        _record_sd_preview_result(job_key, False)
        return False

    candidate_names = _extract_preview_name_candidates(task_data=task_data, print_data=print_data)
    normalized_candidates = [_normalize_preview_name(name) for name in candidate_names if _normalize_preview_name(name)]
    ftp_client = None

    try:
        ftp_client = ImplicitReusedFTP_TLS()
        ftp_client.connect(PRINTER_IP, 990, timeout=10)
        ftp_client.login(user='bblp', passwd=ACCESS_CODE)
        ftp_client.prot_p()

        entries = _list_printer_3mf_archives(ftp_client)
        selected_entry, match_mode = _select_best_archive(entries, normalized_candidates)
        if not selected_entry:
            raise RuntimeError("No .3mf files found on printer storage")

        archive_name = selected_entry['name']
        archive_bytes = _download_ftp_file_bytes(ftp_client, archive_name)
        preview_bytes, preview_member = _extract_preview_bytes_from_3mf(archive_bytes)
        if not preview_bytes or not preview_member:
            raise RuntimeError(f"No preview image found in archive {archive_name}")

        saved = save_print_cover_image(preview_bytes, f"sdcard:{archive_name}#{preview_member}")
        if not saved:
            raise RuntimeError("Could not write preview image to printCover.png")

        write_to_file('printCover', f"sdcard://{archive_name}#{preview_member}")
        update_writer_status(
            sd_preview_status='success',
            sd_preview_job_key=job_key,
            sd_preview_archive=archive_name,
            sd_preview_member=preview_member,
            sd_preview_match_mode=match_mode
        )
        _record_sd_preview_result(job_key, True)
        print(f"Loaded SD-card preview from {archive_name}:{preview_member}")
        return True
    except Exception as exc:
        update_writer_status(
            sd_preview_status='error',
            sd_preview_job_key=job_key,
            last_error=f"SD preview fallback failed: {exc}",
            last_error_at=datetime.utcnow().isoformat() + "Z"
        )
        _record_sd_preview_result(job_key, False)
        print(f"SD-card preview fallback failed: {exc}")
        return False
    finally:
        if ftp_client is not None:
            try:
                ftp_client.quit()
            except Exception:
                pass

def launch_progress_server():
    """Launches the progress bar server as a separate process."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    server_path = os.path.join(script_dir, 'progressbarServer.py')
    env = os.environ.copy()
    env["BASE_DIR"] = BASE_DIR
    proc = subprocess.Popen([sys.executable, server_path], env=env)
    subprocesses.append(proc)
    return proc

def cleanup_subprocesses():
    """Terminates all running subprocesses initiated by this script."""
    for proc in subprocesses:
        proc.terminate()  # Terminate the subprocess
        proc.wait()       # Wait for the subprocess to exit

def stop_progressbar_server(proc=None):
    """Terminate the provided progress bar process and any tracked subprocesses."""
    if proc is not None:
        try:
            proc.terminate()
            proc.wait()
        except Exception as exc:
            print(f"Failed to stop progress bar server process: {exc}")
    cleanup_subprocesses()

total_layer_num_global = None

def normalize_cloud_region(region: str | None) -> str:
    value = str(region or '').strip().lower()
    return 'china' if value in {'china', 'cn'} else 'global'


def cloud_api_base_url(normalized_region: str) -> str:
    return 'https://api.bambulab.cn' if normalized_region == 'china' else 'https://api.bambulab.com'


class BambuCloud:
    def __init__(self, region: str, email: str, password: str, auth_token: str | None = None):
        self.region = normalize_cloud_region(region)
        self.email = email
        self.password = password
        self.auth_token = (auth_token or '').strip() or None
        self._device_id_cache = {}
        self.client = None
        self.session = None
        self.authenticator = None

        if BambuAuthenticator is not None:
            self.authenticator = BambuAuthenticator(region=self.region)

    def _requires_token_refresh(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return '401' in message or 'unauthorized' in message

    def _code_callback(self):
        raise BambuAuthError(
            "Bambu Cloud requires an interactive verification code login. "
            "Set BAMBU_CLOUD_TOKEN in .env (from Bambu Studio or Bambu Handy)."
        )

    def _build_client(self, token: str):
        if CloudApiClient is None:
            raise ImportError(
                "Missing dependency 'bambu-lab-cloud-api'. "
                "Install it with: pip install bambu-lab-cloud-api"
            )
        client = CloudApiClient(token=token)
        client.BASE_URL = cloud_api_base_url(self.region)
        return client

    def _obtain_token(self, force_new: bool = False) -> str:
        if self.auth_token and not force_new:
            return self.auth_token

        if self.authenticator is None:
            raise ImportError(
                "Missing dependency 'bambu-lab-cloud-api'. "
                "Install it with: pip install bambu-lab-cloud-api"
            )

        if not self.email or not self.password:
            raise ValueError("EMAIL and PASSWORD must be set when BAMBU_CLOUD_TOKEN is not provided.")

        try:
            token = self.authenticator.get_or_create_token(
                username=self.email,
                password=self.password,
                code_callback=self._code_callback,
                force_new=force_new
            )
        except BambuAuthError as exc:
            raise ValueError(f"Bambu Cloud authentication failed: {exc}") from exc

        if not token:
            raise ValueError("Bambu Cloud authentication returned an empty token.")

        self.auth_token = token
        return token

    def login(self, force_new: bool = False):
        """Initialize authenticated Cloud API client."""
        if self.client is not None and self.auth_token and not force_new:
            return

        if self.auth_token and not force_new:
            token = self.auth_token
        else:
            token = self._obtain_token(force_new=force_new)

        self.client = self._build_client(token)
        self.session = self.client.session

    def _api_get(self, endpoint: str, params: dict | None = None):
        if self.client is None:
            self.login()

        try:
            return self.client.get(endpoint, params=params)
        except BambuAPIError as exc:
            if self._requires_token_refresh(exc) and self.email and self.password:
                print("Auth token rejected (401). Refreshing token and retrying once.")
                self.login(force_new=True)
                return self.client.get(endpoint, params=params)
            raise

    def get_device_list(self):
        """Retrieve list of devices associated with account."""
        print("Getting device list from Bambu Cloud")
        payload = self._api_get('v1/iot-service/api/user/bind')
        devices = payload.get('devices') if isinstance(payload, dict) else None
        if devices is None and isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            devices = payload['data'].get('devices')
        return devices or []

    def _extract_task_hits(self, payload: dict) -> list:
        """Extract task list from known response shapes."""
        if not isinstance(payload, dict):
            return []

        tasks = payload.get('tasks')
        if isinstance(tasks, list):
            return tasks

        hits = payload.get('hits')
        if isinstance(hits, list):
            return hits
        if isinstance(hits, dict):
            inner_hits = hits.get('hits')
            if isinstance(inner_hits, list):
                return inner_hits

        data = payload.get('data')
        if isinstance(data, dict):
            tasks = data.get('tasks')
            if isinstance(tasks, list):
                return tasks

            hits = data.get('hits')
            if isinstance(hits, list):
                return hits
            if isinstance(hits, dict):
                inner_hits = hits.get('hits')
                if isinstance(inner_hits, list):
                    return inner_hits

            tasks = data.get('list')
            if isinstance(tasks, list):
                return tasks

        return []

    def get_device_id_for_serial(self, printer_sn: str):
        """Resolve a printer serial number to its cloud device ID."""
        if not printer_sn:
            return None
        cached = self._device_id_cache.get(printer_sn)
        if cached:
            return cached
        try:
            devices = self.get_device_list()
        except Exception as exc:
            print(f"Failed to fetch device list: {exc}")
            return None
        for device in devices:
            serial = (
                device.get('sn')
                or device.get('serial')
                or device.get('device_sn')
                or device.get('printerSn')
                or device.get('dev_id')
            )
            if serial == printer_sn:
                device_id = (
                    device.get('deviceId')
                    or device.get('dev_id')
                    or device.get('device_id')
                    or device.get('id')
                )
                if device_id:
                    self._device_id_cache[printer_sn] = device_id
                    return device_id
        print(f"No deviceId found for printer serial {printer_sn}.")
        return None

    def _task_matches_identifier(self, task: dict, identifier: str) -> bool:
        if not isinstance(task, dict):
            return False
        if not identifier:
            return True
        task_identifiers = (
            task.get('deviceId'),
            task.get('device_id'),
            task.get('device_sn'),
            task.get('sn'),
            task.get('printerSn')
        )
        return any(value == identifier for value in task_identifiers)

    def _task_sort_key(self, task: dict):
        task_id = str(task.get('id', '')).strip()
        timestamp_fields = (
            'endTime',
            'startTime',
            'createTime',
            'updatedAt',
            'createdAt'
        )
        for field in timestamp_fields:
            value = str(task.get(field, '')).strip()
            if not value:
                continue
            try:
                normalized = value.replace('Z', '+00:00')
                parsed_time = datetime.fromisoformat(normalized)
                return (2, parsed_time.timestamp(), task_id)
            except ValueError:
                continue

        # Fallback: sortable numeric ID when timestamp fields are unavailable.
        if task_id.isdigit():
            return (1, int(task_id), task_id)
        return (0, 0, task_id)

    def get_latest_task_for_printer(self, identifier: str):
        """Fetch the latest task for a printer by device ID or serial."""
        print(f"Fetching latest task for printer identifier: {identifier}")
        tasklist = self.get_tasklist(device_id=identifier, limit=20)
        tasks = self._extract_task_hits(tasklist)

        if not tasks:
            # Some accounts reject filter params; retry without them.
            tasklist = self.get_tasklist(limit=50)
            tasks = self._extract_task_hits(tasklist)

        matching = [task for task in tasks if self._task_matches_identifier(task, identifier)]
        candidates = matching or tasks
        if not candidates:
            return None

        candidates.sort(key=self._task_sort_key, reverse=True)
        return candidates[0]

    def get_tasklist(self, device_id: str | None = None, limit: int = 20):
        """Fetch the task list from Bambu Cloud."""
        print("Fetching task list from Bambu Cloud")
        params = {}
        if device_id:
            params['deviceId'] = device_id
        if limit:
            params['limit'] = int(limit)
        try:
            return self._api_get('v1/user-service/my/tasks', params=params or None)
        except Exception as primary_error:
            print(f"Primary task endpoint failed ({primary_error}); falling back to iot-service endpoint.")
            fallback_params = {}
            if device_id:
                fallback_params['device_id'] = device_id
            if limit:
                fallback_params['limit'] = int(limit)
            return self._api_get('v1/iot-service/api/user/task', params=fallback_params or None)


if not os.path.exists(BASE_DIR):
    os.makedirs(BASE_DIR, exist_ok=True)

update_writer_status(state="initialized")
        
def format_remaining_time(minutes):
    """Formats remaining time from minutes to '-HhMm'."""
    hours, minutes = divmod(minutes, 60)
    return f"-{hours}h{minutes}m"

def write_to_file(filename, content):
    """Writes content to a file within the data directory, ensuring numeric content is formatted correctly."""
    try:
        path = os.path.join(BASE_DIR, f"{filename}.txt")
        serialized_content = ''
        if content is None:
            serialized_content = ''
        elif isinstance(content, float):
            serialized_content = f"{content:.2f}"
        else:
            serialized_content = str(content)

        with open(path, 'w', encoding='utf-8') as file:
            file.write(serialized_content)

        new_write_count = int(writer_status.get("write_count", 0)) + 1
        update_writer_status(
            write_count=new_write_count,
            last_write_at=datetime.utcnow().isoformat() + "Z",
            last_write_file=f"{filename}.txt",
            last_write_value=serialized_content[:128]
        )
        print(f"Updated {filename}.txt with content: {content}")
    except Exception as e:
        update_writer_status(
            last_error=f"write_to_file({filename}): {e}",
            last_error_at=datetime.utcnow().isoformat() + "Z"
        )
        print(f"Failed to write to {filename}.txt: {e}")

def load_from_file(file_name, default=None):
    """Utility function to load data from a file, returning a default value if the file does not exist."""
    file_path = os.path.join(BASE_DIR, file_name + '.txt')
    try:
        with open(file_path, "r", encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return default

def load_last_processed_task_id(task_id_path: str = TASK_ID_FILE_PATH):
    """Load the last known task ID written to disk, if present."""
    try:
        with open(task_id_path, 'r', encoding='utf-8') as file:
            task_id = file.read().strip()
            return task_id or None
    except FileNotFoundError:
        return None

def persist_latest_task_id(task_id: str, task_id_path: str = TASK_ID_FILE_PATH):
    """Persist the provided task ID so that restarts can resume seamlessly."""
    if not task_id:
        return
    with open(task_id_path, 'w', encoding='utf-8') as file:
        file.write(str(task_id))

def reset_layer_tracking():
    """Clear cached layer tracking information when a new job starts."""
    global total_layer_num_global
    total_layer_num_global = None
    write_to_file("total_layer_num", None)
    write_to_file("layerOverview", "Layer: ? / ?")

def _parse_concatenated_json(content: str, max_entries: int):
    """Parse sequential JSON objects from a string, returning up to the last max_entries items."""
    entries = deque(maxlen=max_entries)
    decoder = json.JSONDecoder()
    idx = 0
    length = len(content)
    while idx < length:
        while idx < length and content[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            obj, offset = decoder.raw_decode(content, idx)
        except json.JSONDecodeError:
            break
        entries.append(obj)
        idx = offset
    return list(entries)

def persist_connection_dumps():
    """Write the current connection dumps deque to disk."""
    try:
        with open(DUMPS_FILE_PATH, 'w', encoding='utf-8') as dumps_file:
            for entry in connection_dumps:
                json.dump(entry, dumps_file, ensure_ascii=False)
                dumps_file.write('\n')
    except OSError as exc:
        print(f"Failed to persist connection dumps: {exc}")

def initialize_connection_dumps():
    """Load existing dumps from disk (if present) and trim to the last configured entries."""
    if not os.path.exists(DUMPS_FILE_PATH):
        return
    try:
        with open(DUMPS_FILE_PATH, 'r', encoding='utf-8') as file:
            content = file.read()
    except OSError as exc:
        print(f"Failed to read connection dumps: {exc}")
        return
    if not content.strip():
        return
    recent_entries = _parse_concatenated_json(content, CONNECTION_DUMPS_MAX_ENTRIES)
    connection_dumps.extend(recent_entries)
    persist_connection_dumps()

def append_connection_dump(entry: dict):
    """Append a new dump entry while ensuring the total count stays within the configured limit."""
    connection_dumps.append(entry)
    persist_connection_dumps()

# Initialize debug dump buffer after helpers are defined
initialize_connection_dumps()

def ensure_job_name_from_print(print_data):
    """
    Populate the designTitle file with the best available name from the MQTT payload.
    This acts as a fallback when the cloud task API cannot provide metadata (e.g. SD card prints).
    """
    existing_title = load_from_file("designTitle", "").strip()
    if existing_title and existing_title not in ("N/A", "Unknown"):
        return

    fallback_fields = ["subtask_name", "task_name", "project_name", "gcode_file"]
    for field in fallback_fields:
        candidate = print_data.get(field)
        if candidate and candidate not in ("", "N/A", "Unknown"):
            write_to_file("designTitle", candidate)
            break

def hex_to_rgb_percent(hex_color):
    """Convert hex color to an RGB percentage string."""
    hex_color = hex_color.lstrip('#')
    r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    return f"rgb({r/255*100}%,{g/255*100}%,{b/255*100}%)"

# Helper function to read filament color from file
def read_filament_color_from_file(tray_idx):
    color_file_path = os.path.join(BASE_DIR, f'ams{tray_idx}FilamentColor.txt')
    try:
        with open(color_file_path, 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        print(f"File {color_file_path} not found.")
        return None

# Helper function to read active ams tray from file
def read_active_ams_tray_from_file():
    amstray_file_path = os.path.join(BASE_DIR, f'activeAmsTray.txt')
    try:
        with open(amstray_file_path, 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        print(f"File {amstray_file_path} not found.")
        return None
    
# Function to update the SVG with colors for all trays
def update_svg_with_all_tray_colors():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_svg_path = os.path.join(script_dir, "templates", "Filaments.svg")
    active_input_svg_path = os.path.join(script_dir, "templates", "ActiveFilament.svg")
    output_svg_path = os.path.join(script_dir, os.pardir, "data", "Filaments.svg")
    active_output_svg_path = os.path.join(script_dir, os.pardir, "data", "ActiveFilament.svg")
    
    tree = ET.parse(input_svg_path)
    active_tree = ET.parse(active_input_svg_path)
    root = tree.getroot()
    active_root = active_tree.getroot()
    namespaces = {'svg': 'http://www.w3.org/2000/svg'}
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    
    # Correctly read the active tray value from file
    try:
        active_ams_tray = int(read_active_ams_tray_from_file())
    except (TypeError, ValueError):
        active_ams_tray = None
    
    for tray_idx in range(1, 5):
        filament_color = read_filament_color_from_file(tray_idx)
        rgb_color = hex_to_rgb_percent(filament_color) if filament_color and filament_color != 'N/A' else None
        
        if rgb_color:
            # Update Filaments.svg
            element_id = f'Color{tray_idx}'
            element = root.find(f".//*[@id='{element_id}']", namespaces)
            if element is not None:
                element.set('fill', rgb_color)
            
            # Update ActiveFilament.svg for lines
            for line_part in ['a', 'b', 'c']:
                line_id = f'Line{tray_idx}{line_part}'
                line_element = active_root.find(f".//*[@id='{line_id}']", namespaces)
                if line_element is not None:
                    line_element.set('fill', rgb_color)
                    # Set opacity based on active tray
                    line_element.set('opacity', '0' if tray_idx != active_ams_tray else '1')

        # Set active filament tray by highlighting the corrected numbered circle
        circle_element_id = f'Circle{tray_idx}'
        circle_element = root.find(f".//*[@id='{circle_element_id}']", namespaces)
        if circle_element is not None:
            if active_ams_tray is not None and tray_idx == active_ams_tray:
                circle_element.set('fill', 'green')  # Active tray
            else:
                circle_element.set('fill', 'gray')  # Inactive trays

    # Check if the active tray is within the expected range (1-4)
    if active_ams_tray is not None and 1 <= active_ams_tray <= 4:
        active_tray_color = read_filament_color_from_file(active_ams_tray)
        active_rgb_color = hex_to_rgb_percent(active_tray_color) if active_tray_color and active_tray_color != 'N/A' else None
        if active_rgb_color:
            color_element = active_root.find(".//*[@id='Extruder']/*[@id='Color']", namespaces)
            if color_element is not None:
                color_element.set('fill', active_rgb_color)
                color_element.set('opacity', '1')  # Ensure the active tray color is fully opaque
    else:
        # If active_ams_tray is outside the expected range, make the extruder color transparent
        color_element = active_root.find(".//*[@id='Extruder']/*[@id='Color']", namespaces)
        if color_element is not None:
            color_element.set('opacity', '0')  # Make the extruder color transparent if no valid tray is active


    # Save the modified SVGs
    tree.write(output_svg_path, xml_declaration=True, encoding='utf-8')
    active_tree.write(active_output_svg_path, xml_declaration=True, encoding='utf-8')
    print(f"Modified SVG saved as {output_svg_path}")
    print(f"Modified ActiveFilament SVG saved as {active_output_svg_path}")
    
# Load the persisted total_layer_num at script startup
total_layer_num_global = load_from_file("total_layer_num", None)
previous_task_id = load_last_processed_task_id()

def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"Connected with result code {reason_code}")
    update_writer_status(
        state="mqtt_connected",
        mqtt_connected_at=datetime.utcnow().isoformat() + "Z",
        mqtt_reason_code=str(reason_code),
        mqtt_topic=f"device/{PRINTER_SN}/report"
    )
    client.subscribe(f"device/{PRINTER_SN}/report")

def on_message(client, userdata, msg):
    global total_layer_num_global, previous_task_id, latest_print_data
    print(" ")
    print(f"Message received -> Topic: {msg.topic} Message: {msg.payload.decode('utf-8')}")
    try:
        message_count = int(writer_status.get("mqtt_message_count", 0)) + 1
        update_writer_status(
            state="mqtt_message_received",
            mqtt_message_count=message_count,
            last_mqtt_message_at=datetime.utcnow().isoformat() + "Z",
            last_mqtt_topic=msg.topic
        )
        message_data = json.loads(msg.payload.decode('utf-8'))

        # Convert numeric values to strings where necessary
        message_data_str = convert_all_to_str(message_data)

        if DEBUG_DUMPS:
            append_connection_dump({"timestamp": datetime.now().isoformat(), "message": message_data_str})

        if 'print' in message_data_str:
            latest_print_data = message_data_str['print']
            handle_print_data(message_data_str['print'])

            # Check if a new print job is detected
            current_task_id = message_data_str['print'].get('task_id')  # Assume the message contains a task ID
            if current_task_id and current_task_id != previous_task_id:
                print("New print job detected. Rerunning Bambu Cloud connection for the latest task.")
                reset_layer_tracking()
                clear_print_cover_files()
                write_to_file('printCover', 'N/A')
                previous_task_id = current_task_id  # Update the last known task ID
                persist_latest_task_id(current_task_id)
                # Reconnect to Bambu Cloud to fetch the latest task information
                try:
                    bambu_cloud = BambuCloud(REGION, EMAIL, PASSWORD, auth_token=CLOUD_AUTH_TOKEN)
                    bambu_cloud.login()
                    process_latest_task(bambu_cloud, PRINTER_SN, BASE_DIR, force_update=True)
                except Exception as auth_error:
                    update_writer_status(
                        last_error=f"Failed cloud refresh after new task: {auth_error}",
                        last_error_at=datetime.utcnow().isoformat() + "Z"
                    )
                    print(f"Failed to refresh latest task from cloud: {auth_error}")
    except Exception as e:
        update_writer_status(
            last_error=f"Error processing MQTT message: {e}",
            last_error_at=datetime.utcnow().isoformat() + "Z"
        )
        print(f"Error processing message: {e}")


def convert_all_to_str(data):
    """Recursively convert all values in the dictionary to strings."""
    if isinstance(data, dict):
        return {k: convert_all_to_str(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_all_to_str(v) for v in data]
    else:
        return str(data)

def handle_print_data(print_data):
    global total_layer_num_global, latest_print_data
    latest_print_data = dict(print_data)
    # Process print profile name
    if 'subtask_name' in print_data:
        write_to_file('printProfile', print_data['subtask_name'])
    ensure_job_name_from_print(print_data)

    if 'gcode_file' in print_data:
        write_to_file('gcodeFile', print_data['gcode_file'])
    if 'file' in print_data:
        write_to_file('printFile', print_data['file'])

    # Process print progress
    if 'mc_percent' in print_data:
        write_to_file('progressPercent', f"{print_data['mc_percent']}%")
        write_to_file('progress', print_data['mc_percent'])

    if 'mc_remaining_time' in print_data:
        formatted_time = format_remaining_time(int(print_data['mc_remaining_time']))
        write_to_file('remaining_time', formatted_time)

    # Process cooling fan speed
    if 'cooling_fan_speed' in print_data:
        cooling_fan_speed = float(print_data['cooling_fan_speed'])
        calculated_speed = (cooling_fan_speed / 15) * 100  # Assuming 15 is the max speed for normalization
        write_to_file('coolingFanSpeed', f"{calculated_speed:.2f}")

    # Process print speed level
    if "spd_lvl" in print_data:
        print(f"Received spd_lvl: {print_data['spd_lvl']}")
        print(f"SPEED_PROFILE keys: {list(SPEED_PROFILE.keys())}")

        spd_lvl_value = int(print_data["spd_lvl"])  # Convert spd_lvl to integer
        speed_level_name = SPEED_PROFILE.get(spd_lvl_value, "Unknown Speed Level")

        # Ensure the first letter is uppercase without altering the case of the rest of the string
        speed_level_name = speed_level_name[0].upper() + speed_level_name[1:]

        print(f"Matched speed level name: {speed_level_name}")  # Debug print
        write_to_file('printSpeed', speed_level_name)

    if "mc_print_stage" in print_data:
        print(f"Received mc_print_stage: {print_data['mc_print_stage']}")
        print(f"CURRENT_STAGE_IDS keys: {list(CURRENT_STAGE_IDS.keys())}")

        mc_print_stage_key = str(print_data['mc_print_stage'])
        mc_print_stage_name = CURRENT_STAGE_IDS.get(mc_print_stage_key, "Unknown Print Stage")
        print(f"Matched print stage name: {mc_print_stage_name}")  # Debug print
        write_to_file('printStage', mc_print_stage_name)
        
    # Process layer number
    if "layer_num" in print_data:
        current_layer = str(print_data['layer_num'])
        write_to_file("layer_num", current_layer)
        total_display = total_layer_num_global if total_layer_num_global not in (None, '') else '?'
        layer_overview_content = f"Layer: {current_layer} / {total_display}"
        write_to_file("layerOverview", layer_overview_content)

    # Process total layer number
    if "total_layer_num" in print_data:
        total_layer_num_global = str(print_data['total_layer_num'])
        write_to_file("total_layer_num", total_layer_num_global)
        last_layer = load_from_file("layer_num")
        if last_layer:
            write_to_file("layerOverview", f"Layer: {last_layer} / {total_layer_num_global}")

    # Process temperatures
    if 'bed_temper' in print_data:
        write_to_file('bedTemperature', f"{float(print_data['bed_temper']):.2f}")
    if 'nozzle_temper' in print_data:
        write_to_file('nozzleTemperature', f"{float(print_data['nozzle_temper']):.2f}")

    # Process AMS trays
    if 'ams' in print_data and 'ams' in print_data['ams']:
        ams_entries = print_data['ams']['ams']
        if ams_entries and 'tray' in ams_entries[0]:
            for tray in ams_entries[0]['tray']:
                tray_idx = int(tray['id']) + 1  # Adjusting from 0-based to 1-based indexing
                filament_id = tray.get('tray_info_idx', 'Unknown')
                filament_color = tray.get('tray_color', 'N/A')
                filament_name = FILAMENT_NAMES.get(filament_id, "Unknown Filament")
                write_to_file(f'ams{tray_idx}FilamentId', filament_id)
                write_to_file(f'ams{tray_idx}FilamentColor', filament_color)
                write_to_file(f'ams{tray_idx}FilamentName', filament_name)
        else:
            print("AMS data present but no tray info available; skipping tray processing.")

    # Process active AMS tray
    if 'ams' in print_data and 'tray_now' in print_data['ams']:
        try:
            active_tray_zero_based = int(print_data['ams']['tray_now'])
            if 0 <= active_tray_zero_based <= 3:
                active_tray = active_tray_zero_based + 1  # Adjusting from 0-based to 1-based indexing
                write_to_file('activeAmsTray', str(active_tray))
                update_svg_with_all_tray_colors()
            else:
                print(f"Ignoring invalid active AMS tray value: {active_tray_zero_based}")
        except (TypeError, ValueError):
            print(f"Could not parse active AMS tray value: {print_data['ams']['tray_now']}")

    print_type = str(print_data.get('print_type', '')).lower()
    gcode_state = str(print_data.get('gcode_state', '')).upper()
    should_attempt_sd_preview = (
        print_type in ('local', 'lan')
        and gcode_state not in ('IDLE', 'FAILED', 'FINISH')
    )
    if should_attempt_sd_preview:
        current_job_key = _build_preview_job_key(print_data=print_data)
        previous_job_key = _get_last_sd_preview_job_key()
        if previous_job_key and current_job_key != previous_job_key:
            clear_print_cover_files()
            write_to_file('printCover', 'N/A')
        attempt_sdcard_preview_fallback(print_data=print_data, force=False)

def format_time_hms(seconds):
    """Formats time from seconds to 'HhMmSs'."""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h {minutes}m {seconds}s"

def extract_task_preview_urls(task_data):
    """Return ordered preview URL candidates from task metadata."""
    if not isinstance(task_data, dict):
        return []

    urls = []
    seen = set()
    sources = [task_data]
    task_message = task_data.get('taskMessage')
    if isinstance(task_message, dict):
        sources.append(task_message)

    for source in sources:
        for field in ('cover', 'thumbnail', 'preview', 'image'):
            value = str(source.get(field, '')).strip()
            if not value or value in ('N/A', 'Unknown'):
                continue
            if value not in seen:
                seen.add(value)
                urls.append(value)

    return urls

def download_cloud_preview_image(bambu_cloud, preview_url: str):
    """Download preview bytes using the cloud client session."""
    if not bambu_cloud or not preview_url:
        return None, None

    if bambu_cloud.session is None:
        bambu_cloud.login()

    request_attempts = []
    if bambu_cloud.auth_token:
        request_attempts.append((
            "authorized",
            {'Authorization': f"Bearer {bambu_cloud.auth_token}"}
        ))
    request_attempts.append(("plain", None))

    for mode, headers in request_attempts:
        try:
            request_kwargs = {'timeout': 10}
            if headers:
                request_kwargs['headers'] = headers
            response = bambu_cloud.session.get(preview_url, **request_kwargs)
            if response.ok and response.content:
                return response.content, mode
            print(f"Preview download attempt '{mode}' returned HTTP {response.status_code} for {preview_url}")
        except Exception as exc:
            print(f"Preview download attempt '{mode}' failed for {preview_url}: {exc}")

    return None, None


def process_latest_task(bambu_cloud, printer_sn, base_dir, force_update=False):
    global is_first_run, previous_task_id, latest_print_data
    if bambu_cloud is None:
        print("Cloud client not available; skipping latest task processing.")
        attempt_sdcard_preview_fallback(print_data=latest_print_data, force=force_update)
        return
    if not printer_sn:
        print("Printer serial number not configured; skipping latest task processing.")
        return

    device_id = bambu_cloud.get_device_id_for_serial(printer_sn)
    lookup_id = device_id or printer_sn
    if device_id:
        print(f"Resolved device ID for {printer_sn}: {device_id}")
    latest_task = bambu_cloud.get_latest_task_for_printer(lookup_id)
    if not latest_task and device_id:
        latest_task = bambu_cloud.get_latest_task_for_printer(printer_sn)

    if not latest_task:
        print("No tasks found for this printer. Skipping task processing.")
        attempt_sdcard_preview_fallback(print_data=latest_print_data, force=force_update)
        return

    # Read the last processed task ID if exists
    task_id_path = os.path.join(base_dir, 'latest_task_id.txt')
    last_processed_task_id = load_last_processed_task_id(task_id_path)

    current_task_id = str(latest_task.get('id'))

    # Check if this is a forced update or if the task info has changed
    if force_update or current_task_id != last_processed_task_id:
        clear_print_cover_files()
        # Extract and write required information from the task
        designTitle = latest_task.get('designTitle', 'N/A')
        printProfile = latest_task.get('title', 'N/A')
        preview_urls = extract_task_preview_urls(latest_task)
        printCover = preview_urls[0] if preview_urls else 'N/A'
        totalWeight = str(latest_task.get('weight', 'N/A')) + "g"
        totalTime = int(latest_task.get('costTime', 0))
        totalTimeFormatted = format_time_hms(totalTime)  # Use the new format function

        # Write extracted information to files
        write_to_file('designTitle', designTitle)
        write_to_file('printProfile', printProfile)
        write_to_file('printCover', printCover)
        write_to_file('totalWeight', totalWeight)
        write_to_file('totalTime', totalTimeFormatted)

        # Download and save print cover image if available
        cover_downloaded = False
        if preview_urls:
            for preview_url in preview_urls:
                preview_bytes, download_mode = download_cloud_preview_image(bambu_cloud, preview_url)
                if preview_bytes:
                    cover_downloaded = save_print_cover_image(preview_bytes, f"cloud:{preview_url}")
                    if cover_downloaded:
                        print(f"Downloaded print cover from cloud URL ({download_mode}): {preview_url}")
                        break
        else:
            print("No print cover URL available for this task.")

        if not cover_downloaded:
            print("Attempting SD-card preview fallback.")
            attempt_sdcard_preview_fallback(task_data=latest_task, print_data=latest_print_data, force=force_update)

        # Update the last processed task ID
        persist_latest_task_id(current_task_id, task_id_path)
        previous_task_id = current_task_id

        print("Latest task processed successfully.")
    elif not force_update and current_task_id == last_processed_task_id:
        print("No new task or already processed. Skipping update.")

    # After processing, disable force_update for subsequent runs
    if is_first_run:
        is_first_run = False


def setup_mqtt_listener():
    try:
        update_writer_status(
            state="mqtt_connecting",
            mqtt_target=f"{PRINTER_IP}:8883",
            mqtt_connect_attempted_at=datetime.utcnow().isoformat() + "Z"
        )
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        # Fallback for older paho-mqtt versions
        client = mqtt.Client()
    client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.username_pw_set(username="bblp", password=ACCESS_CODE)
    client.connect(PRINTER_IP, 8883, 60)
    return client

def main():
    """
    Main function to initialize Bambu Cloud connection, start progress bar server,
    and handle MQTT messages for Bambu 3D printer status updates.
    """
    update_writer_status(state="starting")
    print("Initializing Bambu Cloud connection...")
    try:
        bambu_cloud = BambuCloud(REGION, EMAIL, PASSWORD, auth_token=CLOUD_AUTH_TOKEN)
        bambu_cloud.login()
        update_writer_status(state="cloud_connected")
        print("Bambu Cloud connection initialized.")
    except Exception as e:
        bambu_cloud = None
        update_writer_status(
            state="cloud_unavailable",
            last_error=f"Cloud init failed: {e}",
            last_error_at=datetime.utcnow().isoformat() + "Z"
        )
        print(f"Unable to initialize Bambu Cloud connection: {e}")
    
    # Process the latest task from Bambu Cloud, forcing update on the first run (if available)
    process_latest_task(bambu_cloud, PRINTER_SN, BASE_DIR, force_update=is_first_run)

    server_proc = launch_progress_server()
    update_writer_status(
        state="progress_server_started",
        progress_server_pid=getattr(server_proc, "pid", None)
    )
    print("Progress bar server started.")

    try:
        # Process the latest task from Bambu Cloud, if any
        process_latest_task(bambu_cloud, PRINTER_SN, BASE_DIR)
        print("Latest task processed.")

        # Setup and start MQTT listener for real-time printer status updates
        print("Connecting to the printer's local MQTT service...")
        mqtt_client = setup_mqtt_listener()
        update_writer_status(state="mqtt_loop_running")
        mqtt_client.loop_forever()
    except KeyboardInterrupt:
        update_writer_status(state="stopped_by_keyboard_interrupt")
        print("Interrupt received, stopping...")
    except Exception as e:
        update_writer_status(
            state="error",
            last_error=f"Unhandled exception: {e}",
            last_error_at=datetime.utcnow().isoformat() + "Z"
        )
        print(f"Unhandled exception: {e}")
    finally:
        stop_progressbar_server(server_proc)
        update_writer_status(state="stopped")
        print("Progress bar server stopped.")

if __name__ == "__main__":
    main()
    
