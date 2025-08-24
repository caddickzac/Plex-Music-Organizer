# Plex Music Library — Organizer (Streamlit)

A simple Streamlit app to **export** your Plex music metadata and **apply bulk updates from CSV** by calling your own Python scripts in the `Scripts/` folder.

---

## Features

- **One-click Export** (via `Scripts/export_library_metadata.py`) with live logs and a download button.
- **Update from CSV**:
  - Auto-discovers update scripts in `./Scripts` (uses optional sidecar JSON for labels & hints).
  - Passes credentials via environment and the CSV path via JSON on **stdin**.
  - Shows **expected columns** and **expected values** (from sidecar JSON) in a single two-column table.
  - Friendly success messages (parses `Done. Edited=N Skipped=M` from script output).
- **Configuration**:
  - Reads `./config.txt` to prefill **Plex URL** and **Plex Token** (quoted or plain values).
- **Windows-safe Unicode**: child scripts run with UTF-8 so emoji/logs don’t crash.

---

## Quick Start

### 1) Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -U streamlit pandas plexapi python-dotenv
