# Plex Music Library Organizer

A powerful **Streamlit** application running on Unraid (or locally) to organize, automate, and curate your Plex Music library.

**Key Features:**
* **Export Metadata:** Dump your entire library (Tracks, Albums, Artists) to CSV.
* **Smart Playlist Creator:** Generate "Sonic" playlists using Plex's sonic analysis, listening history, and custom seeds (e.g., "Jazz Mix" or "Radiohead Radio").
* **Bulk Editor:** Update metadata via CSV (Genres, Ratings, Dates, Collections) using single or chained scripts.
* **Metadata Comparator:** Compare two export files to see exactly what changed in your library over time.
* **Docker/Unraid Ready:** Designed to run as a container with environment variables.

---

## 1. Installation

### 1.1 Local Installation
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

### 1.2 Unraid (Docker)
1.  **Install from App Store (coming soon):** Search for "Plex Music Organizer" (once published) or add the container manually.
2.  **Configure Environment Variables:**
    * `PLEX_URL`: Your local server address (e.g., `http://192.168.1.50:32400`).
    * `PLEX_TOKEN`: Your X-Plex-Token.
    * `PLEX_LIBRARY_NAME`: The exact name of your music library (Default: `Music`).
3.  **Map Volumes:**
    * `/app/config.txt`: (Optional) Map if you prefer file-based config.
    * `/app/Exports`: **Crucial.** Map this to a share (e.g., `/mnt/user/appdata/plex-organizer/Exports`) to access your CSV dumps.


---


## 2 Configuration

### 2.1  Local Configuration

Create a file named config.txt in the root folder. 

```text
Plex URL: e.g., http://192.168.1.50:32400
Plex Token: abcXYZ123token
Plex Library: Music
```

Note: Add the txt file to your .gitignore to keep your token safe!

### 2.2  Unraid Version

Edit Environment Variables in Unraid Docker App.

The app needs these variable:
* PLEX_URL
* PLEX_TOKEN
* PLEX_LIBRARY_NAME (e.g., "Music", "HiFi", "FLAC")

## 3. Export

The Export module allows you to extract metadata from your Plex library into CSV files. This is useful for backing up your library data, analyzing your listening habits in Excel, or preparing data for mass updates.
* Functionality: Connects to your Plex server and scans the selected music library.
* Export Levels:
    * Tracks: Exports individual song data (e.g., title, rating, play count, skip count, date added, playlist/collection information).
    * Albums: Exports album-level data (e.g., album title, year, track counts, average bitrate).
    * Artists: Exports artist-level data (e.g., album lists for each artist, track counts, collection information, total file size).
* Output: Generates timestamped CSV files in the Exports folder. These files serve as the template for the Update modules.

## 4. Update from CSV (Single Script)

This tool allows you to mass-edit your Plex metadata by uploading a modified CSV file. It is an efficient way to perform bulk changes (e.g., re-tagging genres, manually setting ratings, or fixing track titles).

Workflow:
1.	Export: First export track details from the Export tab. This provides a detailed CSV file of your music library at three levels of granularity:
* track (“YY_MM_DD Track_Level_Info.csv”)
* album (“YY_MM_DD Artist_Album_Info.csv”)
* artist (“YY_MM_DD Artist_Level_Info.csv”)
2.	Edit: Make your edits to the track level csv, this is the file you will upload. Tip: Just copy the rows you are editing to a new file. Remember to include the file header row.
3.	Variable Mapping: The tool matches rows to your Plex content using unique track IDs (ratingKey). The tool is looking for specific variable names based on the specific action you choose. Check out the Expected CSV schema & values dropdown box for information for each action type.
4.	Choose an Action for Your Update: Select your action from the Choose an action list.
5.	Upload csv with your edits: Either drag and drop or look up your file on the Drag and drop file here button.
6.	Confirm action: Type CONFIRM to sign off on your submission. Warning: Edits to metadata in plex can cause hard-to-fix errors or even corrupt your database. Make sure to make a backup of your data first and complete small test runs (3-5 tracks) before committing an action. 
7.	Run: Commits the changes to your Plex Media Server.

Notes: 
* Editing track genres is computationally taxing because each track’s genre metadata must be ‘locked’ after updating. Be mindful when using this action and check computation times with small batches (50 tracks or less) before committing to a longer run. 
* Editing artist titles is not possible via the API. This must be manually changed in the Plex app. 

5. Update from CSV (Multiple Scripts)

This tool works like the Update from CSV (Single Script), but it allows for multiple scripts to run one after another. For example, you can edit an artists’ genre, album genre, and track genre information in one run. The same rules for variable mapping apply  for each action. See section 2. Update from CSV (Single Script) for more details.

6. Compare Exported Metadata

This analysis tool takes two export files (e.g., "Export_Jan_01.csv" and "Export_Feb_01.csv") and highlights the differences.

•	Purpose:
•	Track how your library has changed across time.
•	Verify that a bulk update script worked correctly.
•	Identify accidental metadata changes or lost ratings.
•	Output: Features a dynamic column selector allowing you to choose exactly which variables to check (e.g., Bitrate, Mood, Rating). Generates a detailed report of differences side-by-side.


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

