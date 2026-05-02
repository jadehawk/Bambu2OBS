"""
Reads the OBS scene collection and replaces all machine-specific absolute paths
with the neutral placeholder tokens used in the committed templates, then writes
the result back to obs_template/Bambu2OBS_Dashboard.json.

Sources checked in order:
  1. Desktop export:  %USERPROFILE%\\Desktop\\Bambu2OBS_Dashboard.json
  2. Live OBS file:   %APPDATA%\\obs-studio\\basic\\scenes\\Bambu2OBS_Dashboard.json

Run this any time you update the OBS layout and want to save it to the repo.
OBS does NOT need to be closed first — it writes its scene file to disk
periodically while running.

Placeholder tokens written back to the template:
    BAMBU2OBS_DATA/          ← any absolute path ending in /data/
    BAMBU2OBS_ASSETS/        ← any absolute path ending in /assets/
    BAMBU2OBS_SRC_TEMPLATES/ ← any absolute path ending in /src/templates/

At server startup, generate_obs_template() in bambu2obs.py replaces these
tokens with the correct absolute paths for the current installation and writes
ready-to-import copies into BASE_DIR.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
REPO_TEMPLATE = REPO_ROOT / "obs_template" / "Bambu2OBS_Dashboard.json"

# Check Desktop export first, then fall back to live OBS AppData file
DESKTOP_EXPORT = Path.home() / "Desktop" / "Bambu2OBS_Dashboard.json"
OBS_SCENES_DIR = Path.home() / "AppData" / "Roaming" / "obs-studio" / "basic" / "scenes"
OBS_FILE = OBS_SCENES_DIR / "Bambu2OBS_Dashboard.json"

if DESKTOP_EXPORT.exists():
    source = DESKTOP_EXPORT
    print(f"Using Desktop export: {source}")
elif OBS_FILE.exists():
    source = OBS_FILE
    print(f"Using live OBS file: {source}")
else:
    print(f"ERROR: No source file found.")
    print(f"  Checked: {DESKTOP_EXPORT}")
    print(f"  Checked: {OBS_FILE}")
    raise SystemExit(1)

content = source.read_text(encoding="utf-8")

# Replace any absolute path ending in /data/ or \data\ with the placeholder token
content = re.sub(
    r'[A-Za-z]:[/\\][^"]*?[/\\]data[/\\]',
    "BAMBU2OBS_DATA/",
    content
)
# Replace any absolute path ending in /assets/ or \assets\
content = re.sub(
    r'[A-Za-z]:[/\\][^"]*?[/\\]assets[/\\]',
    "BAMBU2OBS_ASSETS/",
    content
)
# Replace any absolute path ending in /src/templates/ (progress bar HTML)
content = re.sub(
    r'[A-Za-z]:[/\\][^"]*?[/\\]src[/\\]templates[/\\]',
    "BAMBU2OBS_SRC_TEMPLATES/",
    content
)

REPO_TEMPLATE.write_text(content, encoding="utf-8")
print(f"Repo template updated.")
print(f"  Source : {source}")
print(f"  Output : {REPO_TEMPLATE}")
print()
print("All machine-specific paths replaced with placeholder tokens.")
print("Run the server (Start_Server.bat) to regenerate the importable JSON files in BASE_DIR.")
