from flask import Flask, jsonify, Response, send_from_directory, render_template_string, url_for
from werkzeug.utils import secure_filename, safe_join
from flask_cors import CORS
import os
import time
import logging
import re
import json
from datetime import datetime
from dotenv import load_dotenv
from threading import Thread
from pathlib import Path

# Load environment variables
load_dotenv()

def resolve_base_dir():
    configured = os.getenv('BASE_DIR', 'data')
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
SVG_FILES = ['Filaments.svg', 'ActiveFilament.svg']
JOB_NAME_FILES = ['designTitle.txt', 'printProfile.txt']
COVER_IMAGE_NAME = 'printCover.png'
COVER_IMAGE_CANDIDATES = (COVER_IMAGE_NAME, 'printcover.png')
PLACEHOLDER_PREVIEW_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 400'>
<rect width='300' height='400' fill='#2f2f2f'/>
<line x1='150' y1='0' x2='150' y2='400' stroke='#0d0d0d' stroke-width='35'/>
<line x1='0' y1='280' x2='150' y2='220' stroke='#0d0d0d' stroke-width='35'/>
<line x1='150' y1='220' x2='300' y2='140' stroke='#0d0d0d' stroke-width='35'/>
</svg>"""

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Configure logging
app.logger.setLevel(logging.DEBUG)

PROGRESS_FILE_PATH = os.path.join(BASE_DIR, 'progress.txt')
PROGRESS_FILE_FALLBACK = os.path.join(BASE_DIR, 'progressPercent.txt')
WRITER_STATUS_FILE_PATH = os.path.join(BASE_DIR, 'writerStatus.json')
PROGRESS_NUMBER_RE = re.compile(r'[-+]?\d*\.?\d+')
SVG_DIR = BASE_DIR  # Assuming SVG files are stored in the BASE_DIR

def read_first_nonempty_text(file_candidates):
    """Return the first non-empty text value from the provided filenames."""
    for filename in file_candidates:
        path = os.path.join(BASE_DIR, filename)
        try:
            with open(path, 'r', encoding='utf-8') as file:
                content = file.read().strip()
                if content:
                    return content
        except FileNotFoundError:
            continue
    return None

def _parse_progress_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    text = str(raw_value).strip()
    if not text:
        return None
    match = PROGRESS_NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None

def read_progress_value():
    details = read_progress_details()
    return details['progress']

def read_progress_details():
    """Return parsed progress plus file-level diagnostics for troubleshooting."""
    file_details = []
    selected_progress = None
    selected_source_path = None

    for path in (PROGRESS_FILE_PATH, PROGRESS_FILE_FALLBACK):
        detail = {
            'path': path,
            'filename': os.path.basename(path),
            'exists': os.path.exists(path),
            'parsed_progress': None
        }
        file_details.append(detail)

        if not detail['exists']:
            continue

        try:
            stat = os.stat(path)
            detail['size_bytes'] = stat.st_size
            detail['mtime'] = datetime.fromtimestamp(stat.st_mtime).isoformat()
            detail['age_seconds'] = max(0.0, round(time.time() - stat.st_mtime, 3))
            with open(path, 'r', encoding='utf-8') as file:
                raw_value = file.read()
                trimmed = raw_value.strip()
                detail['raw'] = trimmed[:64]
                value = _parse_progress_value(raw_value)
                detail['parsed_progress'] = value
                if value is None:
                    detail['error'] = "No numeric value found"
                elif selected_progress is None:
                    selected_progress = value
                    selected_source_path = path
        except OSError as exc:
            detail['error'] = str(exc)
            continue

    return {
        'progress': selected_progress,
        'source_path': selected_source_path,
        'files': file_details
    }

def file_watcher(filename, last_known_stamp=0):
    """
    Generator function to watch for file changes.
    """
    while True:
        try:
            stat = os.stat(os.path.join(SVG_DIR, filename))
            if stat.st_mtime != last_known_stamp:
                last_known_stamp = stat.st_mtime
                yield f"data: update\n\n"
        except FileNotFoundError:
            pass
        time.sleep(1)

def get_job_name():
    """Retrieve the most relevant job name available on disk."""
    job_name = read_first_nonempty_text(JOB_NAME_FILES)
    return job_name or "Unknown Print"

def cover_image_name():
    """Return the available cover image filename, if any."""
    for candidate in COVER_IMAGE_CANDIDATES:
        candidate_path = os.path.join(BASE_DIR, candidate)
        if os.path.exists(candidate_path):
            return candidate
    return None

@app.route('/job-info')
def job_info():
    cover_name = cover_image_name()
    cover_available = cover_name is not None
    payload = {
        'job_name': get_job_name(),
        'has_cover': cover_available,
        'cover_url': url_for('serve_print_cover') if cover_available else None,
        'preview_image_url': url_for('print_preview_image')
    }
    return jsonify(payload)

@app.route('/print-cover')
def serve_print_cover():
    cover_name = cover_image_name()
    if cover_name:
        return send_from_directory(BASE_DIR, cover_name)
    return "File not found", 404

@app.route('/print-preview-image')
def print_preview_image():
    cover_name = cover_image_name()
    if cover_name:
        response = send_from_directory(BASE_DIR, cover_name)
    else:
        response = Response(PLACEHOLDER_PREVIEW_SVG, mimetype='image/svg+xml')
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response

@app.route('/progress')
def get_progress():
    try:
        progress_details = read_progress_details()
        progress_value = progress_details['progress']
        source_path = progress_details['source_path']
        if progress_value is None:
            response = jsonify({
                'progress': None,
                'source': None,
                'error': "No readable progress value found in progress.txt or progressPercent.txt"
            })
        else:
            source_name = os.path.basename(source_path) if source_path else None
            response = jsonify({'progress': progress_value, 'source': source_name})
            if source_name:
                response.headers['X-Progress-Source'] = source_name
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/diagnostics/progress')
def diagnostics_progress():
    progress_details = read_progress_details()
    source_path = progress_details['source_path']
    response = jsonify({
        'base_dir': BASE_DIR,
        'cwd': os.getcwd(),
        'checked_at': datetime.utcnow().isoformat() + 'Z',
        'selected_progress': progress_details['progress'],
        'selected_source': os.path.basename(source_path) if source_path else None,
        'files': progress_details['files']
    })
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response

@app.route('/diagnostics/writer')
def diagnostics_writer():
    payload = {
        'base_dir': BASE_DIR,
        'cwd': os.getcwd(),
        'checked_at': datetime.utcnow().isoformat() + 'Z',
        'status_file': WRITER_STATUS_FILE_PATH,
        'status_file_exists': os.path.exists(WRITER_STATUS_FILE_PATH),
        'writer_status': None
    }

    if payload['status_file_exists']:
        try:
            stat = os.stat(WRITER_STATUS_FILE_PATH)
            payload['status_file_mtime'] = datetime.fromtimestamp(stat.st_mtime).isoformat()
            payload['status_file_age_seconds'] = max(0.0, round(time.time() - stat.st_mtime, 3))
            with open(WRITER_STATUS_FILE_PATH, 'r', encoding='utf-8') as status_file:
                payload['writer_status'] = json.load(status_file)
        except (OSError, ValueError) as exc:
            payload['error'] = str(exc)
    else:
        payload['error'] = 'writerStatus.json not found. Start bambu2obs.py to populate writer diagnostics.'

    response = jsonify(payload)
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response

@app.route('/view/printpreview')
def printpreview_view():
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Print Preview</title>
        <style>
            html, body {
                margin: 0;
                padding: 0;
                width: 100%;
                height: 100%;
                overflow: hidden;
                background: transparent;
            }
            .preview-wrapper {
                width: 100%;
                height: 100%;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .preview-image {
                width: 100%;
                height: 100%;
                object-fit: contain;
            }
        </style>
    </head>
    <body>
        <div class="preview-wrapper">
            <img id="print-preview" class="preview-image" alt="Print preview image">
        </div>
        <script>
            const previewImage = document.getElementById('print-preview');
            const baseUrl = (window.location.origin && window.location.origin !== 'null')
                ? window.location.origin
                : 'http://localhost:5000';

            function buildUrl(path) {
                const url = new URL(path, baseUrl);
                url.searchParams.set('t', Date.now());
                return url.toString();
            }

            function refreshPreview() {
                previewImage.src = buildUrl('/print-preview-image');
            }

            refreshPreview();
            setInterval(refreshPreview, 2500);
        </script>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/view/progressbar')
def progressbar_view():
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Customized Bootstrap Progress Bar for OBS</title>
        <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
        <style>
            html, body {
                margin: 0;
                padding: 0;
                overflow: hidden;
            }
            .overlay-wrapper {
                padding: 10px;
                max-width: 600px;
                margin: 0 auto;
                display: flex;
                flex-direction: column;
                gap: 10px;
                font-family: "Segoe UI", Tahoma, sans-serif;
                color: #f5f5f5;
                background-color: rgba(0,0,0,0.0);
            }
            .job-info {
                display: flex;
                align-items: center;
            }
            .job-cover {
                width: 128px;
                height: 128px;
                border-radius: 8px;
                object-fit: contain;
                margin-right: 14px;
                background-color: transparent;
                display: none;
                /* filter: invert(100%); */
            }
            .job-cover.visible {
                display: block;
            }
            .job-name {
                font-size: 1.1rem;
                font-weight: 600;
                margin-bottom: 8px;
            }
            .progress {
                background-color: #EEEEEE;
                height: 30px;
                margin: 0;
                width: 100%;
            }
            .progress-bar {
                background-color: #00AE42;
            }
        </style>
    </head>
    <body>

    <div class="overlay-wrapper">
        <div class="job-info">
            <img id="job-cover" class="job-cover" alt="Print cover preview">
            <div class="job-details w-100">
                <div id="job-name" class="job-name">Loading print...</div>
                <div class="progress">
                    <div id="progress-bar" class="progress-bar" role="progressbar" style="width: 0%;" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100"></div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const PLACEHOLDER_COVER = "data:image/svg+xml;charset=UTF-8,%3Csvg%20xmlns%3D%27http%3A//www.w3.org/2000/svg%27%20viewBox%3D%270%200%20300%20400%27%3E%3Crect%20width%3D%27300%27%20height%3D%27400%27%20fill%3D%27%232f2f2f%27/%3E%3Cline%20x1%3D%27150%27%20y1%3D%270%27%20x2%3D%27150%27%20y2%3D%27400%27%20stroke%3D%27%230d0d0d%27%20stroke-width%3D%2735%27/%3E%3Cline%20x1%3D%270%27%20y1%3D%27280%27%20x2%3D%27150%27%20y2%3D%27220%27%20stroke%3D%27%230d0d0d%27%20stroke-width%3D%2735%27/%3E%3Cline%20x1%3D%27150%27%20y1%3D%27220%27%20x2%3D%27300%27%20y2%3D%27140%27%20stroke%3D%27%230d0d0d%27%20stroke-width%3D%2735%27/%3E%3C/svg%3E";
        const jobCover = document.getElementById('job-cover');
        const jobName = document.getElementById('job-name');
        const progressBar = document.getElementById('progress-bar');
        const baseUrl = (window.location.origin && window.location.origin !== 'null')
            ? window.location.origin
            : 'http://localhost:5000';

        function buildUrl(path) {
            const url = new URL(path, baseUrl);
            url.searchParams.set('t', Date.now());
            return url.toString();
        }

        function showPlaceholderCover() {
            jobCover.src = PLACEHOLDER_COVER;
            jobCover.classList.add('visible');
        }

        jobCover.addEventListener('error', () => {
            showPlaceholderCover();
        }, { once: false });

        function updateProgress() {
            fetch(buildUrl('/progress'), { cache: 'no-store' })
                .then(response => response.json())
                .then(data => {
                    const numericValue = parseFloat(data.progress);
                    if (!Number.isFinite(numericValue)) {
                        return;
                    }
                    const clampedValue = Math.min(100, Math.max(0, numericValue));
                    progressBar.style.width = `${clampedValue}%`;
                    progressBar.setAttribute('aria-valuenow', clampedValue);
                })
                .catch(error => console.error('Error fetching progress:', error));
        }

        function updateJobInfo() {
            fetch(buildUrl('/job-info'), { cache: 'no-store' })
                .then(response => response.json())
                .then(data => {
                    jobName.textContent = data.job_name || 'Unknown Print';
                    if (data.has_cover && data.cover_url) {
                        const coverUrl = new URL(data.cover_url, baseUrl);
                        coverUrl.searchParams.set('t', Date.now());
                        jobCover.src = coverUrl.toString();
                        jobCover.classList.add('visible');
                    } else {
                        showPlaceholderCover();
                    }
                })
                .catch(error => console.error('Error fetching job info:', error));
        }

        function refreshOverlay() {
            updateProgress();
            updateJobInfo();
        }

        refreshOverlay();
        setInterval(refreshOverlay, 2500);
    </script>

    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/updates/<filename>')
def updates(filename):
    if filename in SVG_FILES:
        return Response(file_watcher(filename), content_type='text/event-stream')
    return "File not found", 404

@app.route('/svg/<filename>')
def serve_svg(filename):
    filename = secure_filename(filename)
    filepath = safe_join(SVG_DIR, filename)
    print(f"Attempting to serve: {filepath}: {secure_filename} in directory: {SVG_DIR}")  # Debug print
    if os.path.exists(filepath):
        print("File found, serving...")  # Debug print
        return send_from_directory(SVG_DIR, filename)
    else:
        app.logger.error(f"File not found: {secure_filename} in directory: {SVG_DIR}")
        return "File not found", 404
    
@app.route('/view/<filename>')
def view_svg(filename):
    if filename in SVG_FILES:
        html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>SVG Viewer - {filename}</title>
        </head>
        <body>
            <img src="/svg/{filename}" id="svgImage">
            <script>
                const evtSource = new EventSource("/updates/{filename}");
                evtSource.onmessage = function(event) {{
                    const img = document.getElementById('svgImage');
                    const src = img.src.split('?')[0];
                    img.src = `${{src}}?t=${{new Date().getTime()}}`;
                }};
            </script>
        </body>
        </html>
        """
        return render_template_string(html)
    return "File not found", 404

if __name__ == '__main__':
    # Disable the Flask reloader to avoid recursive subprocess spawning when
    # this script is launched from bambu2obs.py.
    app.run(debug=False, use_reloader=False, port=5000)
