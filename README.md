# Plex Music Library Organizer (Streamlit App)

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
  - Example:
```bash
Plex URL: "http://xxx.xxx.x.xxx:xxxxx"
Plex Token: abcd1234...

```

## Requirements
- Python 3.10+
- pip install -r requirements.txt (dependencies: streamlit, plexapi, pandas)

## Plex API & URL Setup Guide
**Find your Plex API key**
1. Sign in to your Plex account in Plex Web App
2. Browse to a library item and view the XML for it
3. Look in the URL and find the token as the X-Plex-Token value

   
**Find your Plex URL:**
1. Plex Settings -> Remote Access
2. Find private URL

## Create a batch file to run program (optional)
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
1. Begin by exporting your Plex music libraries metadata to a csv file. (It is recommended that you save a copy of the file for restoring any unwanted changes.)
2. Make the desired edits to a copy of the exported csv file. (Note: examine "Expected CSV schema & values" tab for information on input variables and formatting. Playlist and collection scripts need a new column variable column added to the exported csv file.)
3. Upload csv file within "Update from CSV" tab after choosing your desired action and run script. 

## Exported File Data Dictionary 
- Information on the exported metadata variables can be found in "Plex_Organizer_Data_Dictionary" file.  

## Troubleshooting
- “Must include items to add when creating new playlist.”
Your CSV produced no valid track objects. Check:
  - Track_ID values are integers and exist on your server.
  - The playlist column is named exactly as expected (e.g., Add_to_playlist).
  - Commas separate multiple names (no semicolons or pipes).
- Note: Bulk writes can be destructive. Always export first. Test with 3–5 rows before running a big CSV.

