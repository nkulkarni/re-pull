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
```

Python 3.10+ recommended (uses modern stdlib features).

## Usage

```bash
# Basic run (default: 40 pages, output to ./data)
python run.py farmontario

# Limit pages and control output dir
python run.py farmontario --max-pages 10 --out-dir data

# Start fresh (ignore existing checkpoint)
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

## Adding a new source

1. Create `sources/mysource.py`
2. Subclass `Scraper` from `.base` and implement `crawl(self) -> list[Listing]`.
3. Use `self.record(listing)` inside your scraper for automatic checkpointing.
4. Register it in the `SCRAPERS` dict at the top of `run.py`.

Everything else (CLI, deduping, CSV writing, checkpoint resume) works automatically.

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
- Checkpoints live at `data/checkpoints/<source>_checkpoint.jsonl`.
- CSVs are written as `<source>_listings_YYYYMMDD_HHMM.csv`.

Never commit scraped data — this repo is for the **scraper code** only.

## License

TBD — add your preferred license when publishing.

## Notes

- The root `farmontario.py` is the original single-file version before the refactor.
- `farmontario_midcache.py` is a copy of the above (the original was never modified) that back-ports the mid-pull checkpointing technique from V2. It uses the same append-to-JSONL + restore-on-start approach but stays as a single standalone script. Use it with `python farmontario_midcache.py --fresh --max-pages 10`.
- Be kind to the sites you scrape (respect robots.txt, use reasonable delays, etc.).
- This is intended as an internal/research tool.

---

Contributions / new sources welcome once the GitHub repo is up!
