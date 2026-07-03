# re-pull

Canadian farm listings scraper. Refactored for modularity, resumability, and easy extension to new data sources.

## Features

- **Modular architecture**: Each source lives in `sources/<name>.py` as a subclass of `Scraper`.
- **Crash-safe checkpointing**: Scraped listings are appended to a JSONL checkpoint immediately. Kill the process at any time and `--fresh` is the only way to start over; normal runs resume automatically.
- **Normalized output schema**: All sources produce the same `Listing` dataclass → consistent CSVs you can concatenate across sources.
- **CLI**: Simple `argparse` interface with source selection, page limits, and output directory control.
- **Polite scraping**: Built-in delays + session reuse.

## Project layout

```
.
├── run.py                 # CLI entrypoint
├── sources/
│   ├── __init__.py
│   ├── base.py            # Shared Listing dataclass + Scraper ABC + checkpoint logic
│   └── farmontario.py     # FarmOntarioScraper implementation
├── farmontario.py         # Original monolithic scraper (kept for reference)
├── requirements.txt
├── .gitignore             # Ignores data/, *.csv dumps, venvs, __pycache__, etc.
└── data/                  # Generated at runtime (gitignored)
    └── checkpoints/
```

## Installation

```bash
git clone <your-repo-url>
cd re-pull

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# REQUIRED for the updated V2 scraper (uses Playwright for reliable browser automation)
playwright install chromium
# (downloads ~150MB browser binaries; only needs to be done once per machine)
```

Python 3.10+ recommended (uses modern stdlib features).

## Playwright + Smart Caching (V2 update)

The core `FarmOntarioScraper` (V2) has been refactored to use the **Playwright + BeautifulSoup technique** from `test.py`:

- Uses a real browser (Chromium via Playwright) for detail pages.
- Automatically handles the "I Accept The Terms" ToS gate + waits for JS/network.
- Improved lat/lng JSON extraction (stricter regex + cleanup).
- Falls back gracefully.

**Smart caching** (new in base.py + integrated):
- Every successfully fetched detail page HTML is saved to `data/cache/<hash>.html`.
- On future runs, if the URL is in the checkpoint (visited), the scraper loads from cache instead of hitting the network/Playwright (huge speed win + resilience if site is flaky).
- Cache is per-URL, survives process restarts.
- `--fresh` now also clears the page cache.
- `--no-cache` disables it (forces live fetches).

Index page crawling (for discovering links) still uses fast `requests` + BS4. Detail pages use the full Playwright treatment.

## Usage

```bash
# Basic run (uses smart cache + checkpoint resume by default)
python run.py farmontario

# Small test run (recommended first)
python run.py farmontario --max-pages 5 --fresh

# Limit pages, custom output, force no cache
python run.py farmontario --max-pages 10 --out-dir data --no-cache

# Full re-pull from scratch (clears checkpoint + page cache)
python run.py farmontario --fresh
```

See all options:

```bash
python run.py --help
```

Output: a timestamped CSV in the chosen directory, e.g.

```
data/farmontario_listings_20260703_1422.csv
```

The CSV will now also include `price_numeric` (parsed from price) when available. More fields (address etc.) can be added by enhancing the BS4 parsing in `sources/farmontario.py`.

## Adding a new source

1. Create `sources/mysource.py`
2. Subclass `Scraper` from `.base` and implement `crawl(self) -> list[Listing]`.
3. Use `self.record(listing)` inside your scraper for automatic checkpointing + mid-pull safety.
4. (Optional but recommended) Use the inherited helpers for smart caching:
   `content = self._get_cached_content(url)`
   `self._save_to_cache(url, content)`
   (see `sources/farmontario.py` for example).
5. Register it in the `SCRAPERS` dict at the top of `run.py`.

Everything else (CLI, deduping, CSV writing, checkpoint resume, page caching) works automatically.

Example skeleton:

```python
from .base import Listing, Scraper

class MySourceScraper(Scraper):
    name = "mysource"

    def crawl(self):
        # your scraping logic here
        listing = Listing(title="...", source=self.name)
        self.record(listing)
        return self.listings
```

Then add to `run.py`:

```python
from sources.mysource import MySourceScraper

SCRAPERS = {
    "farmontario": FarmOntarioScraper,
    "mysource": MySourceScraper,
}
```

## Data & checkpoints

- All runtime artifacts go under `data/` (completely ignored by git).
- Checkpoints (resume state): `data/checkpoints/<source>_checkpoint.jsonl`
- Smart page cache (raw HTML for speed): `data/cache/<hash>.html`
- CSVs are written as `<source>_listings_YYYYMMDD_HHMM.csv`.

Never commit scraped data — this repo is for the **scraper code** only.

`--fresh` clears both checkpoint and cache. Use `--no-cache` to bypass the HTML cache for a run.

## License

TBD — add your preferred license when publishing.

## Notes

- The root `farmontario.py` is the original single-file version before the refactor.
- `farmontario_midcache.py` is a copy of the above (the original was never modified) that back-ports the mid-pull checkpointing technique from V2. It uses the same append-to-JSONL + restore-on-start approach but stays as a single standalone script. Use it with `python farmontario_midcache.py --fresh --max-pages 10`.
- **V2 is the recommended path**: `python run.py farmontario ...` (now with Playwright details + smart HTML caching).
- `test.py` is a standalone quick tester for 1-2 specific listing URLs using the same Playwright+BS4 technique (useful for debugging extraction on a problematic URL).
- Be kind to the sites you scrape (respect robots.txt, use reasonable delays, etc.).
- This is intended as an internal/research tool.

---

Contributions / new sources welcome once the GitHub repo is up!
