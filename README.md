# Plex Music Library — Organizer (Streamlit)

A simple Streamlit app to **export** your Plex music metadata and **apply bulk updates from CSV** files.

---

## Features

- **One-click Export** (via `Scripts/export_library_metadata.py`) with live status logs.
- **Update from CSV**:
  - Add artist genre information for each track.
  - Bulk relabel: album titles, album genres, disc numbers, track artist, track numbers, track ratings, and track title.
  - Create music collections (track, album, or artist level). 
  - Create music playlists. 
  - Shows **expected columns** and **expected values** for ease of use. 
- **Configuration**:
  - Reads `./config.txt` to prefill **Plex URL** and **Plex Token** (quoted or plain values).
    
---


## Create a batch file to run program
1. Create text document on desktop
2. Enter code below, changing "[working directory]" to your local directory where the streamlit app is saved. 

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
