"""
One-time helper: reads both obs_template JSON files and replaces every
absolute path that points to the data/ or assets/ directory with the
neutral placeholder tokens used by generate_obs_template():

    BAMBU2OBS_DATA   → replaced at runtime with BASE_DIR
    BAMBU2OBS_ASSETS → replaced at runtime with <repo>/assets

Run once from the repo root:
    python normalise_templates.py
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATES = [
    REPO_ROOT / "obs_template" / "Bambu2OBS_Dashboard.json",
    REPO_ROOT / "obs_template" / "Bambu2OBS_Scene.json",
]

for template_path in TEMPLATES:
    if not template_path.exists():
        print(f"SKIP (not found): {template_path}")
        continue

    content = template_path.read_text(encoding="utf-8")

    # Replace any absolute path ending in /data/ or \data\
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
    # Replace any absolute path ending in /src/templates/ (for progressbar HTML)
    content = re.sub(
        r'[A-Za-z]:[/\\][^"]*?[/\\]src[/\\]templates[/\\]',
        "BAMBU2OBS_SRC_TEMPLATES/",
        content
    )

    template_path.write_text(content, encoding="utf-8")
    print(f"Normalised: {template_path}")

print("Done.")
