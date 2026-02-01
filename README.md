# Plex Music Library Organizer

A powerful **Streamlit** application running on Unraid (or locally) to organize, automate, and curate your Plex Music library.

**Key Features:**
* **Export Metadata:** Dump your entire library (Tracks, Albums, Artists) to CSV.
* **Smart Playlist Creator:** Generate "Sonic" playlists using Plex's sonic analysis, listening history, and custom seeds (e.g., "Jazz Mix" or "Radiohead Radio").
* **Bulk Editor:** Update metadata via CSV (Genres, Ratings, Dates, Collections) using single or chained scripts.
* **Metadata Comparator:** Compare two export files to see exactly what changed in your library over time.
* **Docker/Unraid Ready:** Designed to run as a container with environment variables.

---

## Installation

### Option A: Unraid (Docker)
1.  **Install from App Store (coming soon):** Search for "Plex Music Organizer" (once published) or add the container manually.
2.  **Configure Environment Variables:**
    * `PLEX_URL`: Your local server address (e.g., `http://192.168.1.50:32400`).
    * `PLEX_TOKEN`: Your X-Plex-Token.
    * `PLEX_LIBRARY_NAME`: The exact name of your music library (Default: `Music`).
3.  **Map Volumes:**
    * `/app/config.txt`: (Optional) Map if you prefer file-based config.
    * `/app/Exports`: **Crucial.** Map this to a share (e.g., `/mnt/user/appdata/plex-organizer/Exports`) to access your CSV dumps.

### Option B: Local (Python)
1.  **Clone the Repo:**
    ```bash
    git clone https://github.com/caddickzac/Plex-Music-Organizer.git
    cd Plex-Music-Organizer
    ```
2.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Run the App:**
    ```bash
    streamlit run Plex_Streamlit_App.py
    ```

---

## ⚙️ Configuration

### Method 1: Environment Variables (Recommended for Docker)
The app prioritizes these variables if they exist.
* `PLEX_URL`
* `PLEX_TOKEN`
* `PLEX_LIBRARY_NAME` (e.g., "Music", "HiFi", "FLAC")

### Method 2: `config.txt` (Recommended for Local)
Create a file named `config.txt` in the root folder. **Add this file to your `.gitignore` to keep your token safe!**

```text
Plex URL: [http://192.168.1.50:32400](http://192.168.1.50:32400)
Plex Token: abcXYZ123token
Plex Library: Music
```

## Features in Detail
1. Playlist Creator (Sonic & History)
Uses Plex's Sonic Analysis to build intelligent playlists.
* Seed Mode: Start with a Track, Artist, Album, or Genre.
* Sonic Journey: Connects tracks based on audio similarity.
* Mixes: "Deep Dive" (Artist discography), "History + Seeds" (Your favorites mixed with new discoveries).
* Requirement: You must have "Sonic Analysis" enabled on your Plex server.

2. Export & Compare
* Export: One-click dump of Track_Level_Info.csv containing ratings, play counts, and file paths.
* Compare: Upload an "Old" and "New" CSV to generate a difference report. Great for tracking what you added, lost, or rated differently.

3. Bulk Updates (CSV)
Upload a CSV to apply changes. You can run one script or chain multiple together.
* Relabel: Bulk rename genres, dates, or titles.
* Collections: Add tracks/albums/artists to Plex Collections in bulk.
* Playlists: Create static playlists from CSV lists.

## Automation (Unraid User Scripts)
You can automate the export process using Unraid's User Scripts plugin to get a daily backup of your library metadata.

Script:
```text
#!/bin/bash
# Replace 'plex-music-organizer' with your actual container name
docker exec -t plex-music-organizer python /app/Scripts/export_library_metadata.py
```
* Schedule: Set to "Daily" or "Weekly".
* Result: A fresh CSV will appear in your mapped Exports folder automatically.

## Safety & Troubleshooting
* Token Security: Never commit your config.txt to GitHub.
* "Sonic Analysis" Error: If the Playlist Creator fails, ensure you have enabled "Sonic Analysis" in your Plex Library settings and that the scheduled task has completed processing your music.
* Bulk Edits: Writes to Plex are potentially destructive. Always Export a backup CSV before running a bulk update script.

## Screenshots of App
![View 1](App%20Screenshots/View%201.png?raw=true)

![View 2](App%20Screenshots/View%202.png?raw=true)

![View 3](App%20Screenshots/View%203.png?raw=true)

## Exported File Data Dictionary 
See the **Plex_Organizer_Data_Dictionary** file for descriptions of the exported metadata fields.

## Troubleshooting
- Error: “Must include items to add when creating new playlist.”
Your CSV produced no valid track objects. Check:
  - Track_ID values are integers and exist on your server.
  - The playlist column is named exactly as expected (e.g., Add_to_playlist).
  - Multiple names are comma-separated (no semicolons or pipes).
- Note: Bulk writes can be destructive. Always export first. Test with 3–5 rows before running a big CSV.

