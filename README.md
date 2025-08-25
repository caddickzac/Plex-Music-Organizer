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

## Create a batch file to open program 

```bash
@echo off
REM set working directory
cd [working directory]

REM Activate the virtual environment
call venv/Scripts/activate

REM Run the Streamlit app using the Python installation in the virtual environment
streamlit run Plex_Streamlit_App.py

REM Pause command to keep the terminal open after the script runs (optional)
pause
```
3. Save and close text document.
4. Change file type from ".txt" to ".bat"
5. Now you can run the batch file as a shortcut icon and avoid having to enter any code in the command line!
