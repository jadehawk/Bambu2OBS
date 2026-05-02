# Bambu2OBS

## Overview

Bambu2OBS is a tool that connects your Bambu Lab 3D printer to OBS Studio, allowing you to monitor and display real-time print progress directly in OBS. Ideal for content creators who want to showcase live 3D printing projects or create timelapses with accurate status updates.

This project integrates Bambu 3D printers with OBS Studio, offering real-time monitoring and display of printing progress. It uses local printer MQTT/FTPS for live status and the `bambu-lab-cloud-api` client for cloud task metadata (profile name, cover image, etc.), with Flask serving dynamic overlay content.

You can watch live demonstrations of this setup on my [Twitch channel](https://www.twitch.tv/fluidprints), where I monitor my 3D prints.

### Features

- Real-time status updates on printing progress
- SVG integration for visualizing print data
- Configurable through environment variables for easy setup

## Setup and Installation

### Prerequisites

- Python 3.x installed on your system
- OBS Studio for display integration
- InkScape for SVG manipulation

### Setting up a Virtual Environment

It's recommended to use a virtual environment for Python projects to manage dependencies efficiently. Follow these steps to set up and activate a virtual environment:

1. Open a terminal or command prompt.
2. Navigate to your project directory:

   ```bash
   cd path/to/Bambu2OBS
   ```

3. Create a virtual environment named `venv` (or any other name you prefer):

   ```bash
   python -m venv b2obsvenv
   ```

4. Activate the virtual environment:
   - On Windows:

     ```bash
     .\b2obsvenv\Scripts\activate
     ```

   - On macOS and Linux:

     ```bash
     source b2obsvenv/bin/activate
     ```

### Installing Dependencies

With the virtual environment activated, install the required Python packages using:

```bash
pip install -r requirements.txt
```

### Configuration

Copy `example.env` to `.env` and adjust the configuration parameters according to your environment and Bambu printer settings.

In order to connect to your Bambu printer, set the following environment variables in `.env`:

- **EMAIL**: Your Bambu Cloud account email.
- **PASSWORD**: Your Bambu Cloud account password.
- **REGION**: Set to `china` if using the Chinese Bambu Cloud; otherwise set to `global`.
- **PRINTER_SN**: The serial number of your Bambu Lab printer.
- **PRINTER_IP**: The IP address of your printer on your local network.
- **ACCESS_CODE**: Access code from your printer settings.
- **BASE_DIR**: Name of the folder that will store the print job data used for overlays.
- **BAMBU_CLOUD_TOKEN**: Optional but recommended cloud token. This avoids interactive verification-code (2FA) login issues in unattended runs.

If `BAMBU_CLOUD_TOKEN` is empty, Bambu2OBS will attempt login with `EMAIL` + `PASSWORD`. Accounts that require verification-code login should set `BAMBU_CLOUD_TOKEN`.

### OBS Auto-Kill (Auto-Stop Stream When Print Finishes)

Bambu2OBS can automatically stop your OBS stream and close OBS Studio when a print job completes. This is controlled by the following `.env` variables:

| Variable                 | Default     | Description                                                                                                |
| ------------------------ | ----------- | ---------------------------------------------------------------------------------------------------------- |
| `OBS_AUTO_KILL`          | `false`     | Set to `true` to enable the feature.                                                                       |
| `OBS_WEBSOCKET_IP`       | `127.0.0.1` | IP address of the machine running OBS. Use `127.0.0.1` if OBS is on the same machine.                      |
| `OBS_WEBSOCKET_PORT`     | `4455`      | OBS WebSocket server port.                                                                                 |
| `OBS_WEBSOCKET_PASSWORD` | _(empty)_   | Password configured in OBS → Tools → WebSocket Server Settings. Leave blank if authentication is disabled. |
| `OBS_FINISH_DELAY`       | `30`        | Seconds to wait after the print finishes before stopping OBS.                                              |

**How it works:**

1. When the printer reports `gcode_state=FINISH` via MQTT, a background timer starts.
2. After `OBS_FINISH_DELAY` seconds, Bambu2OBS connects to the OBS WebSocket, stops the stream if it is active, then sends a graceful close signal to OBS.
3. If OBS does not close within 10 seconds, it is force-terminated as a fallback.

**Prerequisites:**

- OBS WebSocket server must be enabled: **OBS → Tools → WebSocket Server Settings → Enable WebSocket server**.
- Install the `websocket-client` dependency (included in `requirements.txt`).

### Running the Application

1. Start the Flask server:

   ```bash
   python .\src\bambu2obs.py
   ```

2. Configure OBS Studio to display the progress bar and SVGs by adding browser sources pointing to the Flask server's URLs.

### Importing the OBS Scene

> ⚠️ **Do NOT import the files from the `obs_template/` folder directly into OBS.**
> Those files contain placeholder tokens (e.g. `BAMBU2OBS_DATA/`) that OBS cannot resolve, which causes missing-file errors for every source.
>
> **Always import the generated files** that the server writes to your `BASE_DIR` folder on startup:
>
> - Default location: `data/` inside the repo root — e.g. `<repo>\data\Bambu2OBS_Dashboard_generated.json`
> - If you set a custom `BASE_DIR` in `.env`, look there instead.
>
> The server must be run at least once before these files exist.

The OBS scene templates in `obs_template/` use placeholder tokens instead of hardcoded paths.
**You must generate the importable files before importing into OBS** — the server does this automatically.

#### Option A — Let the server generate them (recommended)

1. Configure your `.env` file (see [Configuration](#configuration) above).
2. Start the server once:
   ```bash
   python .\src\bambu2obs.py
   ```
3. The server writes two ready-to-import files into your `BASE_DIR` folder on startup:
   - `Bambu2OBS_Dashboard_generated.json` ← main dashboard (recommended)
   - `Bambu2OBS_Scene_generated.json` ← alternative minimal scene
4. Open OBS Studio.
5. Go to **Scene Collection → Import** and select one of the generated `.json` files from your `BASE_DIR` folder.
6. All source paths (data files, icons, progress bar) will already point to the correct locations for your installation — no manual editing required.

#### Option B — Generate without starting the full server

```bash
python -c "
import os, sys
sys.path.insert(0, 'src')
from dotenv import load_dotenv
load_dotenv()
import bambu2obs
"
```

This runs only the startup portion of the server (template generation + BASE_DIR creation) without connecting to the printer.

#### Saving your OBS layout back to the repo

If you rearrange sources or groups in OBS and want to commit the updated layout:

```bash
python sync_obs_template.py
```

This reads the live OBS scene file directly from AppData (OBS does **not** need to be closed), replaces all machine-specific paths with portable placeholder tokens, and saves the result to `obs_template/Bambu2OBS_Dashboard.json`. Restart the server once afterward to regenerate the importable file in `BASE_DIR`.

> **Note:** `sync_obs_template.py` only syncs `Bambu2OBS_Dashboard.json`. If you modify `Bambu2OBS_Scene.json`, edit it manually and run `python normalise_templates.py` to replace any hardcoded paths with placeholder tokens before committing.

![Bambu2OBS OBS Scene Example](images/obs_scene_example.png)

### Attribution

This project is open-source under the MIT License. If you use or adapt Bambu2OBS, please provide credit to [fluidman](https://github.com/fluidman) and reference this repository.

Special thanks to Greg Hesp for the `pybambu` library, which facilitates communication with Bambu 3D printers. GitHub repository: [pybambu](https://github.com/greghesp/pybambu).

Cloud API integration is powered by [coelacant1/Bambu-Lab-Cloud-API](https://github.com/coelacant1/Bambu-Lab-Cloud-API).

### License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## About

Bambu2OBS is designed for OBS Studio users who want to display live updates from Bambu Lab 3D printers. This tool supports most Bambu printer models and works best with OBS Studio and Flask, providing a flexible solution for showcasing real-time print progress in streaming setups.
