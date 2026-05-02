"""
obs_controller.py
-----------------
Handles graceful shutdown of OBS Studio when a Bambu print job finishes.

Connects to the OBS WebSocket (obs-websocket v5 protocol), stops any active
stream, sends WM_CLOSE for a graceful exit, and falls back to a force-kill if
OBS does not close within the grace period.

Configuration is read from environment variables (set in .env):
    OBS_WEBSOCKET_IP       – IP address of the machine running OBS (default: 127.0.0.1)
    OBS_WEBSOCKET_PORT     – OBS WebSocket port (default: 4455)
    OBS_WEBSOCKET_PASSWORD – OBS WebSocket password (leave blank if auth is disabled)
    OBS_AUTO_KILL          – Set to "true" to enable auto-kill (default: false)
    OBS_FINISH_DELAY       – Seconds to wait after print finishes before killing OBS (default: 30)
    SERVER_IDLE_TIMEOUT    – Minutes to wait before shutting down the server when idle (default: 15)
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import base64
import ctypes
import subprocess
import threading
import logging

try:
    import websocket  # websocket-client package
    _WEBSOCKET_AVAILABLE = True
except ImportError:
    websocket = None  # type: ignore[assignment]
    _WEBSOCKET_AVAILABLE = False

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _get_obs_config():
    return {
        "ip":       os.getenv("OBS_WEBSOCKET_IP", "127.0.0.1"),
        "port":     int(os.getenv("OBS_WEBSOCKET_PORT", "4455")),
        "password": os.getenv("OBS_WEBSOCKET_PASSWORD", ""),
        "delay":    int(os.getenv("OBS_FINISH_DELAY", "30")),
        "enabled":  os.getenv("OBS_AUTO_KILL", "false").strip().lower() in ("1", "true", "yes", "on"),
        "idle_timeout_minutes": int(os.getenv("SERVER_IDLE_TIMEOUT", "15")),
    }

# ---------------------------------------------------------------------------
# OBS WebSocket v5 auth
# ---------------------------------------------------------------------------

def _compute_auth_token(password: str, challenge: str, salt: str) -> str:
    """Compute the OBS WebSocket v5 authentication token."""
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("utf-8")
    auth = base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("utf-8")
    return auth

# ---------------------------------------------------------------------------
# Graceful WM_CLOSE via ctypes (Windows only)
# ---------------------------------------------------------------------------

def _send_wm_close_to_obs():
    """Send WM_CLOSE to the OBS window for a graceful shutdown."""
    try:
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010

        # EnumWindows callback to find obs64 window
        found_hwnd = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        def enum_callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                # Check if this window belongs to obs64
                try:
                    if _PSUTIL_AVAILABLE and psutil is not None:
                        proc = psutil.Process(pid.value)
                        if "obs" in proc.name().lower():
                            found_hwnd.append(hwnd)
                except Exception:
                    pass
            return True

        user32.EnumWindows(enum_callback, 0)

        if found_hwnd:
            for hwnd in found_hwnd:
                user32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
            logger.info("WM_CLOSE sent to %d OBS window(s).", len(found_hwnd))
            return True
        else:
            logger.warning("No OBS window found for WM_CLOSE.")
            return False
    except Exception as exc:
        logger.warning("WM_CLOSE attempt failed: %s", exc)
        return False

# ---------------------------------------------------------------------------
# Force-kill fallback
# ---------------------------------------------------------------------------

def _force_kill_obs():
    """Force-terminate obs64.exe as a last resort."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "obs64.exe"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logger.info("OBS force-killed via taskkill.")
        else:
            logger.warning("taskkill returned non-zero: %s", result.stderr.strip())
    except Exception as exc:
        logger.error("Force-kill failed: %s", exc)

# ---------------------------------------------------------------------------
# OBS WebSocket interaction
# ---------------------------------------------------------------------------

def _obs_websocket_stop_and_close(ip: str, port: int, password: str):
    """
    Connect to OBS WebSocket, stop the stream if active, then disconnect.
    Returns True if the WebSocket session completed successfully.
    """
    if not _WEBSOCKET_AVAILABLE or websocket is None:
        logger.warning(
            "websocket-client is not installed. "
            "Skipping WebSocket stop-stream step. "
            "Install it with: pip install websocket-client"
        )
        return False

    ws_url = f"ws://{ip}:{port}"
    logger.info("Connecting to OBS WebSocket at %s ...", ws_url)

    try:
        ws = websocket.create_connection(ws_url, timeout=5)
    except Exception as exc:
        logger.warning("Could not connect to OBS WebSocket: %s", exc)
        return False

    try:
        # Step 1: Receive Hello (op=0)
        raw = ws.recv()
        hello = json.loads(raw)
        if hello.get("op") != 0:
            logger.warning("Expected Hello (op=0), got: %s", hello.get("op"))
            return False

        # Step 2: Build Identify payload (op=1)
        identify_data: dict[str, object] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth_info = hello.get("d", {}).get("authentication")
        if auth_info and password:
            identify_data["authentication"] = _compute_auth_token(
                password,
                auth_info["challenge"],
                auth_info["salt"]
            )

        ws.send(json.dumps({"op": 1, "d": identify_data}))

        # Step 3: Receive Identified (op=2)
        raw = ws.recv()
        identified = json.loads(raw)
        if identified.get("op") != 2:
            logger.warning("Expected Identified (op=2), got: %s", identified.get("op"))
            return False

        logger.info("Authenticated with OBS WebSocket.")

        # Step 4: Check stream status
        ws.send(json.dumps({
            "op": 6,
            "d": {
                "requestType": "GetStreamStatus",
                "requestId": "streamCheck",
                "requestData": {}
            }
        }))
        raw = ws.recv()
        status_resp = json.loads(raw)
        is_streaming = (
            status_resp.get("op") == 7
            and status_resp.get("d", {}).get("requestType") == "GetStreamStatus"
            and status_resp.get("d", {}).get("responseData", {}).get("outputActive") is True
        )

        # Step 5: Stop stream if active
        if is_streaming:
            logger.info("OBS is streaming — sending StopStream.")
            ws.send(json.dumps({
                "op": 6,
                "d": {
                    "requestType": "StopStream",
                    "requestId": "stopStream",
                    "requestData": {}
                }
            }))
            try:
                ws.recv()  # consume the response
            except Exception:
                pass
            time.sleep(2)  # brief pause to let the stream stop cleanly
        else:
            logger.info("OBS is not streaming — skipping StopStream.")

        return True

    except Exception as exc:
        logger.warning("OBS WebSocket session error: %s", exc)
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _obs_is_streaming(ip: str, port: int, password: str) -> bool:
    """
    Connect to OBS WebSocket and return True if a stream is currently active.
    Returns False if OBS is unreachable or not streaming.
    """
    if not _WEBSOCKET_AVAILABLE or websocket is None:
        return False

    ws_url = f"ws://{ip}:{port}"
    try:
        ws = websocket.create_connection(ws_url, timeout=5)
    except Exception as exc:
        logger.warning("Could not connect to OBS WebSocket to check stream status: %s", exc)
        return False

    try:
        # Hello (op=0)
        raw = ws.recv()
        hello = json.loads(raw)
        if hello.get("op") != 0:
            return False

        # Identify (op=1)
        identify_data: dict[str, object] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth_info = hello.get("d", {}).get("authentication")
        if auth_info and password:
            identify_data["authentication"] = _compute_auth_token(
                password, auth_info["challenge"], auth_info["salt"]
            )
        ws.send(json.dumps({"op": 1, "d": identify_data}))

        # Identified (op=2)
        raw = ws.recv()
        if json.loads(raw).get("op") != 2:
            return False

        # GetStreamStatus
        ws.send(json.dumps({
            "op": 6,
            "d": {
                "requestType": "GetStreamStatus",
                "requestId": "streamCheck",
                "requestData": {}
            }
        }))
        raw = ws.recv()
        resp = json.loads(raw)
        return (
            resp.get("op") == 7
            and resp.get("d", {}).get("requestType") == "GetStreamStatus"
            and resp.get("d", {}).get("responseData", {}).get("outputActive") is True
        )
    except Exception as exc:
        logger.warning("OBS stream-status check failed: %s", exc)
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass


def kill_obs(delay_seconds: int | None = None):
    """
    Stop the OBS stream (if active) and shut down OBS.

    Steps:
      1. Wait ``delay_seconds`` (or OBS_FINISH_DELAY from env if None).
      2. Connect to OBS WebSocket and check stream status.
         If OBS is reachable but NOT streaming, skip the shutdown entirely —
         this prevents closing OBS when it was opened manually (e.g. to tweak
         the dashboard) and no stream was ever started.
      3. Stop stream → disconnect.
      4. Send WM_CLOSE for a graceful exit.
      5. Wait up to 10 seconds; if OBS is still running, force-kill it.
    """
    cfg = _get_obs_config()
    wait = delay_seconds if delay_seconds is not None else cfg["delay"]

    if wait > 0:
        logger.info("Print finished. Waiting %d seconds before stopping OBS...", wait)
        time.sleep(wait)

    logger.info("Initiating OBS shutdown sequence.")

    # Check whether OBS is actually streaming before doing anything destructive.
    # If OBS is running but idle (not streaming), leave it alone.
    if _WEBSOCKET_AVAILABLE:
        streaming = _obs_is_streaming(cfg["ip"], cfg["port"], cfg["password"])
        if not streaming:
            logger.info(
                "OBS is reachable but not streaming — skipping shutdown. "
                "Start a stream first for auto-kill to take effect."
            )
            return

    # Try WebSocket stop-stream first
    _obs_websocket_stop_and_close(cfg["ip"], cfg["port"], cfg["password"])

    # Graceful WM_CLOSE
    wm_sent = _send_wm_close_to_obs()

    if wm_sent:
        # Give OBS up to 10 seconds to close gracefully
        logger.info("Waiting up to 10 seconds for OBS to close gracefully...")
        for _ in range(10):
            time.sleep(1)
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq obs64.exe"],
                capture_output=True, text=True
            )
            if "obs64.exe" not in result.stdout:
                logger.info("OBS closed gracefully.")
                return

    # Fallback: force-kill
    logger.info("OBS did not close gracefully — force-killing.")
    _force_kill_obs()


# One-shot scheduling state
_kill_scheduled = threading.Event()
_kill_lock = threading.Lock()


def schedule_obs_kill_once(delay_seconds: int | None = None):
    """
    Schedule a one-shot OBS kill in a background thread.
    Subsequent calls while a kill is already scheduled are ignored.
    """
    cfg = _get_obs_config()
    if not cfg["enabled"]:
        logger.debug("OBS_AUTO_KILL is not enabled — skipping.")
        return

    with _kill_lock:
        if _kill_scheduled.is_set():
            logger.info("OBS kill already scheduled — ignoring duplicate trigger.")
            return
        _kill_scheduled.set()

    wait = delay_seconds if delay_seconds is not None else cfg["delay"]
    logger.info(
        "Scheduling OBS auto-kill in %d seconds (OBS_AUTO_KILL=true).", wait
    )

    thread = threading.Thread(
        target=_run_kill_and_reset,
        args=(wait,),
        daemon=True,
        name="obs-auto-kill"
    )
    thread.start()


def _run_kill_and_reset(delay_seconds: int):
    """Internal: run kill_obs then reset the scheduled flag and restart idle timer."""
    try:
        kill_obs(delay_seconds=delay_seconds)
    finally:
        with _kill_lock:
            _kill_scheduled.clear()
        # After OBS has been killed (print finished), restart the idle shutdown
        # timer so the server exits automatically if no new print starts within
        # SERVER_IDLE_TIMEOUT minutes.
        logger.info(
            "OBS kill sequence complete — restarting idle shutdown timer in case "
            "no new print starts."
        )
        schedule_idle_shutdown()


def reset_kill_schedule():
    """
    Clear the one-shot guard so a new print job can re-arm the auto-kill.
    Call this when a new print job is detected.
    """
    with _kill_lock:
        _kill_scheduled.clear()
    logger.debug("OBS kill schedule reset for new print job.")


# ---------------------------------------------------------------------------
# Idle server shutdown
# ---------------------------------------------------------------------------

_idle_timer: threading.Timer | None = None
_idle_timer_lock = threading.Lock()


def _shutdown_server():
    """Terminate the current server process after the idle timeout expires."""
    logger.info(
        "Idle timeout reached — no active print detected and no OBS stream was "
        "started. Shutting down the Bambu2OBS server."
    )
    # Use os._exit so the shutdown is immediate even if daemon threads are running.
    os._exit(0)


def schedule_idle_shutdown():
    """
    Start (or restart) the idle shutdown timer.

    The timer fires if no active print is detected within SERVER_IDLE_TIMEOUT
    minutes of the server starting (or after OBS has been killed following a
    completed print).  Call ``cancel_idle_shutdown()`` as soon as an active
    print state is observed to prevent the server from shutting itself down.

    Set SERVER_IDLE_TIMEOUT=0 in .env to disable this feature entirely.
    """
    cfg = _get_obs_config()
    timeout_minutes = cfg["idle_timeout_minutes"]

    if timeout_minutes <= 0:
        logger.info("SERVER_IDLE_TIMEOUT=0 — idle shutdown is disabled.")
        return

    timeout_seconds = timeout_minutes * 60

    with _idle_timer_lock:
        global _idle_timer
        # Cancel any existing timer before starting a new one.
        if _idle_timer is not None and _idle_timer.is_alive():
            _idle_timer.cancel()
            logger.debug("Existing idle timer cancelled before rescheduling.")

        _idle_timer = threading.Timer(timeout_seconds, _shutdown_server)
        _idle_timer.daemon = True
        _idle_timer.name = "idle-shutdown-timer"
        _idle_timer.start()

    logger.info(
        "Idle shutdown timer started — server will stop in %d minute(s) if no "
        "print activity is detected.",
        timeout_minutes,
    )


def cancel_idle_shutdown():
    """
    Cancel the idle shutdown timer.

    Call this when an active print is detected so the server stays alive for
    the duration of the print.  The OBS auto-kill (``schedule_obs_kill_once``)
    handles the end-of-print shutdown independently.
    """
    with _idle_timer_lock:
        global _idle_timer
        if _idle_timer is not None and _idle_timer.is_alive():
            _idle_timer.cancel()
            _idle_timer = None
            logger.info("Idle shutdown timer cancelled — active print detected.")
        else:
            logger.debug("cancel_idle_shutdown called but no active timer found.")
