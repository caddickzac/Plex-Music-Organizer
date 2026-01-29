Note: I've added a bunch of new functionality and have been slow to update the readme.


# Plex Music Library Organizer (Streamlit App)

A simple Streamlit app to **export** your Plex music metadata and **apply bulk updates from CSV** files.

---

## Features
- **One-click Export** (via `Scripts/export_library_metadata.py`) with live status logs.
- **Update from CSV**:
  - Add artist-genre information for each track.
  - Bulk relabel album dates, album titles, album genres, disc numbers, track artist, track date created, track numbers, track ratings, and track title.
  - Create music collections (track, album, or artist level). 
  - Create music playlists. 
  - Details **expected columns** and **expected values** for ease of use. 
- **Configuration**:
  - Reads `./config.txt` to prefill **Plex URL** and **Plex Token** (quoted or plain values).
  - Example:
```bash
Plex URL: "http://xxx.xxx.x.xxx:xxxxx"
Plex Token: abcd1234...

```

## Screenshots of App
![View 1](App%20Screenshots/View%201.png?raw=true)
![View 2](App%20Screenshots/View%202.png?raw=true)

## Requirements
- Python 3.10+
- pip install -r requirements.txt (dependencies: streamlit, plexapi, pandas)

## Plex API & URL Setup Guide
**Find your Plex API key**
1. Sign in to the Plex Web App
2. Browse to any library item and open its XML view
3. In the address bar, copy the value of X-Plex-Token

**Find your Plex URL:**
1. Go to Settings -> Remote Access in the Plex Web App
2. Copy the Local (private) URL

## Create a batch file to run the program (optional)
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

## How to Use
1. Export your Plex music library’s metadata to a CSV file. (Tip: Save a copy of the exported file so you can restore any unwanted changes.)
2. Make edits to a copy of the exported CSV. See the “Expected CSV schema & values” section in the app for input fields and formats.
Note: Playlist and collection scripts require adding new columns to the exported CSV (e.g., Add_to_playlist, Add_to_album_collection, Add_to_artist_collection, Add_to_track_collection).
3. In the Update from CSV tab, upload your edited CSV, choose an action, and run the script. 

## Exported File Data Dictionary 
See the **Plex_Organizer_Data_Dictionary** file for descriptions of the exported metadata fields.

## Troubleshooting
- Error: “Must include items to add when creating new playlist.”
Your CSV produced no valid track objects. Check:
  - Track_ID values are integers and exist on your server.
  - The playlist column is named exactly as expected (e.g., Add_to_playlist).
  - Multiple names are comma-separated (no semicolons or pipes).
- Note: Bulk writes can be destructive. Always export first. Test with 3–5 rows before running a big CSV.

