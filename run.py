import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from sources.farmontario import FarmOntarioScraper
from sources.farmscom import FarmsComScraper

# Register each new source scraper here — everything else (checkpointing,
# CLI, output) works automatically once it's added to this mapping.
SCRAPERS = {
    "farmontario": FarmOntarioScraper,
    "farmscom": FarmsComScraper,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Canadian farm listings scraper")
    parser.add_argument("source", choices=SCRAPERS.keys())
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--out-dir", default="data")
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore checkpoint + clear page cache and start over (recommended for full re-pull)"
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable smart page HTML caching (forces fresh network requests for every detail page)"
    )
    parser.add_argument(
        "--consolidate", action="store_true",
        help="After scraping, also update a consolidated CSV (with acres + cost_per_acre derived)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    scraper = SCRAPERS[args.source](
        max_pages=args.max_pages,
        use_cache=not args.no_cache
    )
    if args.fresh:
        scraper.clear_checkpoint(clear_cache=True)

    listings = scraper.crawl()

    # Ensure derived fields (acres, cost_per_acre) are computed even if scraper missed the call
    for listing in listings:
        if hasattr(listing, "compute_derived_fields"):
            listing.compute_derived_fields()

    df = pd.DataFrame([listing.to_dict() for listing in listings])
    if df.empty:
        logging.info("No listings found.")
        return

    df = df.drop_duplicates(subset=["detail_url"])

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"{args.source}_listings_{timestamp}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8")
    logging.info("Saved %d listing(s) to %s", len(df), out_path)

    if getattr(args, "consolidate", False):
        try:
            from consolidate import consolidate
            consolidate(data_dir=str(out_dir), output_dir=str(out_dir), output_name="consolidated_farm_listings.csv")
            logging.info("Also updated consolidated_farm_listings.csv in %s", out_dir)
        except Exception as e:
            logging.warning("Consolidation step failed: %s", e)


if __name__ == "__main__":
    main()
