import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from sources.farmontario import FarmOntarioScraper
from sources.farmscom import FarmsComScraper

# === REGISTER NEW SOURCES HERE ===
# The "engine" will run all of them by default.
SCRAPERS = {
    "farmontario": FarmOntarioScraper,
    "farmscom": FarmsComScraper,
}


def update_master_from_df(df: pd.DataFrame, out_dir: Path) -> None:
    """Append-only update of the stable master data asset."""
    if df.empty:
        return
    try:
        from consolidate import update_master
        master_path = out_dir / "master_listings.csv"
        update_master(df, master_path)
    except Exception as e:
        logging.warning("Failed to update master asset: %s", e)


def run_source(name: str, max_pages: int, use_cache: bool, fresh: bool, out_dir: Path) -> pd.DataFrame:
    """Run one source and return the DataFrame of what was just scraped (new stuff only)."""
    if name not in SCRAPERS:
        raise ValueError(f"Unknown source: {name}")

    ScraperCls = SCRAPERS[name]
    scraper = ScraperCls(max_pages=max_pages, use_cache=use_cache)

    if fresh:
        scraper.clear_checkpoint(clear_cache=True)
        logging.info("Fresh mode for %s - checkpoints and cache cleared", name)
    else:
        logging.info("Append mode for %s - using existing checkpoints (only new listings)", name)

    listings = scraper.crawl()

    for listing in listings:
        if hasattr(listing, "compute_derived_fields"):
            listing.compute_derived_fields()

    df = pd.DataFrame([l.to_dict() for l in listings])
    if df.empty:
        logging.info("No new listings for %s", name)
        return df

    df = df.drop_duplicates(subset=["detail_url"])

    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    snapshot = out_dir / f"{name}_listings_{ts}.csv"
    df.to_csv(snapshot, index=False, encoding="utf-8")
    logging.info("Saved snapshot for %s: %s (%d rows)", name, snapshot.name, len(df))

    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="re-pull scraping engine - one command to grow your stable farm data asset"
    )
    parser.add_argument(
        "sources",
        nargs="*",
        help="Sources to scrape (e.g. farmontario farmscom). Omit or pass 'all' to run every registered source.",
    )
    parser.add_argument("--max-pages", type=int, default=40, help="Max list pages per source")
    parser.add_argument("--out-dir", default="data", help="Where to write snapshots and the master")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Full refresh: clear checkpoints/caches for the selected sources (otherwise append-only)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable HTML page caching (slower, forces live fetches)",
    )
    parser.add_argument(
        "--no-master",
        action="store_true",
        help="Do not update the master_listings.csv asset after this run",
    )
    parser.add_argument(
        "--master-only",
        action="store_true",
        help="Skip scraping and only rebuild the master from existing snapshots",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(args.out_dir)

    if args.master_only:
        from consolidate import consolidate
        consolidate(data_dir=str(out_dir), output_dir=str(out_dir), output_name="master_listings.csv")
        return

    sources_to_run: list[str]
    raw = args.sources or ["all"]
    if "all" in raw:
        sources_to_run = list(SCRAPERS.keys())
    else:
        sources_to_run = [s for s in raw if s in SCRAPERS]

    logging.info("=== re-pull engine starting ===")
    logging.info("Sources: %s | mode: %s", sources_to_run, "FRESH" if args.fresh else "APPEND")

    all_new_dfs: list[pd.DataFrame] = []

    for src in sources_to_run:
        try:
            df = run_source(
                src,
                max_pages=args.max_pages,
                use_cache=not args.no_cache,
                fresh=args.fresh,
                out_dir=out_dir,
            )
            if not df.empty:
                all_new_dfs.append(df)
        except Exception as e:
            logging.exception("Source %s failed: %s", src, e)

    if not args.no_master and all_new_dfs:
        combined_new = pd.concat(all_new_dfs, ignore_index=True)
        update_master_from_df(combined_new, out_dir)

    logging.info("=== re-pull engine finished ===")


if __name__ == "__main__":
    main()
