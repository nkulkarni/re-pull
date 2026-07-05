# re-pull: Collect Ontario Farm Listings Easily

**What is this tool?**

This is a simple computer program that automatically gathers information about farms that are for sale in Ontario from websites like farms.com and farmontario.com.

It saves all the information into one easy spreadsheet called `master_listings.csv`. You can open this file in Microsoft Excel, Google Sheets, or any spreadsheet program.

It does useful things automatically:
- Collects farm details like price, size, location
- Remembers what it already collected so it doesn't repeat work
- Adds map coordinates (latitude and longitude) even if the website doesn't show them
- Calculates "cost per acre" for you

**Who is this for?**

Anyone who wants to track farm real estate in Ontario without manually copying information from websites every week. Farmers, real estate agents, researchers, or people interested in land prices.

**Important notes before starting**
- This tool visits websites automatically. It is polite (it waits between pages), but please do not run it constantly.
- The data it collects is for your personal or research use.
- You need a computer with internet.

---

## Step-by-Step: Getting Started (No Tech Experience Needed)

### 1. Download the program

1. Go to this GitHub page: https://github.com/nkulkarni/re-pull
2. Click the green **Code** button (top right)
3. Click **Download ZIP**
4. Save the file and unzip it (double-click on most computers)
5. Rename the unzipped folder to `re-pull` and put it somewhere easy to find (for example, your Desktop)

### 2. Open your command line / terminal

This is where you type instructions to the computer.

- **On Mac**: Press Command + Space, type "Terminal", and open it.
- **On Windows**: Press the Windows key, type "Command Prompt" or "PowerShell", and open it.

### 3. Go into the re-pull folder

In the terminal, type this and press Enter (change the path if you put the folder somewhere else):

```bash
cd Desktop/re-pull
```

(If you're on Windows and used Command Prompt, it might look slightly different, but the idea is the same.)

### 4. Create a safe space for the program (one-time setup)

Type these lines **one at a time** and press Enter after each:

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

(On Windows it may be: `.venv\Scripts\activate`)

You should see `(.venv)` appear at the start of your command line. This means you're now using a private copy of the tools just for this program.

Next:

```bash
pip install -r requirements.txt
```

This downloads everything the program needs. It may take a few minutes.

Then install the browser tool it uses:

```bash
playwright install chromium
```

This downloads a special browser (about 150 MB). It only needs to be done once.

### 5. (Optional but recommended) Get map coordinates automatically

Some websites don't include exact map pins. This tool can add them for you using a free service called Geocodio.

1. Go to https://www.geocod.io/
2. Sign up for a free account (no credit card needed for the free tier).
3. Copy your API key (it looks like a long string of letters and numbers).
4. In your terminal, type this (replace `your_key_here` with the actual key):

```bash
export GEOCODIO_API_KEY=your_key_here
```

**Do this every time you open a new terminal window**, or ask someone how to make it permanent on your computer.

If you skip this step, the program will still work — it just won't add map coordinates for sources that don't provide them.

### 6. Run the program

To collect the latest farm listings:

```bash
python run.py
```

That's it!

- It will automatically visit the websites.
- It only collects **new** listings it hasn't seen before (smart "append" mode).
- It updates your master spreadsheet.
- If you set the Geocodio key, it will add map coordinates where missing.

The first run may take longer because it's being careful and downloading things.

---

## Looking at Your Data

After running, go to the `data` folder inside `re-pull`.

The most important file is:

**`master_listings.csv`**

- Double-click it to open in Excel or upload to Google Sheets.
- It contains columns like: title, price, acres, cost_per_acre, latitude, longitude, source, etc.
- `latitude` and `longitude` will have numbers (for mapping) or be blank if not available.
- `cost_per_acre` is calculated automatically when possible.

There may also be other files like `farmontario_listings_...csv` — these are daily snapshots. The `master_listings.csv` is the one you usually care about.

---

## Updating Your Data Over Time

Just run the same command again:

```bash
python run.py
```

It will:
- Remember what it already has (thanks to checkpoints)
- Only collect new farms
- Update the master spreadsheet with any new information

To start completely fresh (get everything again):

```bash
python run.py --fresh
```

---

## Geocoding (Adding Map Coordinates)

Some websites show exact map pins on the page. Others don't.

When you provide a Geocodio API key (see step 5 above), the program will automatically look up the address and add latitude + longitude for any listing that is missing them.

- It only does this for new listings that need it.
- Results are saved forever in a cache file so it doesn't waste your free daily limit.
- A column called `geocode_provider` will say "geocodio" for any that were added this way.

**Note**: For very rural "Lot and Concession" style addresses common in Ontario, the coordinates will usually point to the general area (township or road) rather than the exact field. This is normal for address-based lookup tools.

---

## Common Questions & Problems

**"Command not found" or "python not recognized"**
- Make sure you installed Python 3 (download from python.org if needed).
- Make sure you typed the commands exactly, including the dot in `.venv`.

**The program is slow the first time**
- Normal. It opens a browser behind the scenes and waits politely between pages. Later runs are much faster because of caching.

**I don't see any coordinates**
- Did you set the `GEOCODIO_API_KEY`?
- Some sources simply don't have good location data.
- Run it again after setting the key.

**I want to stop it**
- In the terminal, press Control + C (or Command + C on Mac).

**Where is everything stored?**
- All data goes into the `data` folder. This folder is ignored by git so you don't accidentally upload private farm data.

**Can I run this on a schedule?**
- Yes, but that's more advanced. You can use your computer's Task Scheduler (Windows) or cron (Mac/Linux).

---

## For People Who Already Know Some Programming

(Everything below this line is technical.)

See the original project structure, source registration in `run.py`, and the `Listing` dataclass in `sources/base.py`.

The engine defaults to running all sources in append mode and maintaining `data/master_listings.csv`.

New sources are added by creating a file in `sources/` and registering it.

---

## Weather Data Integration (New Phase)

We are beginning to enrich the farm listings with historical weather data from nearby weather stations.

A demo script is included: `download_weather_box.py`

It is deliberately written to be as foolproof as possible for non-technical users:
- You do **not** need to choose chunk sizes — it automatically breaks huge date ranges (even 50+ years of hourly data) into safe API-sized pieces.
- It is **resumable**: if it gets interrupted or hits a temporary rate limit, just run the exact same command again and it will skip what it already downloaded and continue.
- Prints very clear warnings and progress messages.
- At the end it writes a simple `download_summary.txt` in the output folder.

It uses the **Visual Crossing** weather API to:
- Discover all weather stations inside or near a lat/long bounding box.
- Download historical observations (hourly by default — use --resolution daily for much smaller/faster results) for the area.
- Attempt to pull direct historical data from each individual station in the box.
- Save clean CSVs (stations list + weather records) that you can join to your master listings by proximity or date.

### Quick start for weather data
1. Get a free Visual Crossing API key at https://www.visualcrossing.com/ (first 1000 records/day free for commercial use too).
2. `export VISUALCROSSING_API_KEY=your_key_here`
3. Run the demo (uses a southern Ontario box by default):
   ```bash
   python download_weather_box.py --start 2023-01-01 --end 2023-12-31
   ```
4. Look in the new `weather_data/` folder for:
   - `stations_in_box.csv`
   - `area_weather_*.csv`
   - Per-station files (where available)

You can change the box with --lat-min / --lat-max / --lon-min / --lon-max and the date range.

Later we will integrate this into the main engine so new farm listings automatically get nearby weather history attached.

This project is intended as a personal/research tool. Be kind to the websites you scrape.

Contributions and new sources are welcome!