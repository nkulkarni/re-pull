#!/usr/bin/env python3
"""
Consolidate listings from all sources into a single clean table.

Features:
- Always includes latitude / longitude columns (float, NaN if missing)
- Parses numeric 'acres' from the free-text 'size' column
- Computes 'cost_per_acre' = price_numeric / acres when both are available
- Deduplicates across sources (by detail_url, keeping latest scraped_at)
- Outputs a single CSV (and optionally Parquet) for easy analysis

Usage:
    python consolidate.py
    python consolidate.py --out-dir data --output consolidated_farm_listings.csv
"""

import argparse
import glob
import logging
from pathlib import Path

import pandas as pd

from sources.base import parse_acres  # reuse the robust parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def consolidate(
    data_dir: str = "data",
    output_dir: str | None = None,
    output_name: str = "consolidated_farm_listings.csv",
    also_parquet: bool = True,
) -> Path:
    """Find all source CSVs, merge, enrich with derived columns, dedup, and save."""

    pattern = str(Path(data_dir) / "*_listings_*.csv")
    files = sorted(glob.glob(pattern))

    if not files:
        logger.warning("No *_listings_*.csv files found in %s", data_dir)
        return Path()

    logger.info("Found %d source files: %s", len(files), [Path(f).name for f in files])

    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            df["source_file"] = Path(f).name
            dfs.append(df)
            logger.info("Loaded %s (%d rows)", Path(f).name, len(df))
        except Exception as e:
            logger.error("Failed to load %s: %s", f, e)

    if not dfs:
        return Path()

    combined = pd.concat(dfs, ignore_index=True)

    # Ensure core columns exist (lat/long always present, even if all empty)
    for col in ["latitude", "longitude"]:
        if col not in combined.columns:
            combined[col] = pd.NA
        combined[col] = pd.to_numeric(combined[col], errors="coerce")

    # Re-parse acres from size (in case individual scrapers were incomplete)
    if "acres" not in combined.columns:
        combined["acres"] = pd.NA
    # Only fill missing acres
    mask = combined["acres"].isna() & combined.get("size", "").notna()
    combined.loc[mask, "acres"] = combined.loc[mask, "size"].apply(parse_acres)

    # Compute / recompute cost_per_acre
    if "price_numeric" in combined.columns and "acres" in combined.columns:
        combined["cost_per_acre"] = combined.apply(
            lambda row: round(row["price_numeric"] / row["acres"], 2)
            if pd.notna(row.get("price_numeric"))
            and pd.notna(row.get("acres"))
            and row.get("acres") > 0
            else pd.NA,
            axis=1,
        )

    # Deduplicate: keep the most recently scraped version of each listing
    if "detail_url" in combined.columns:
        combined = combined.sort_values("scraped_at", ascending=False)
        combined = combined.drop_duplicates(subset=["detail_url"], keep="first")
        logger.info("After dedup by detail_url: %d rows", len(combined))

    # Order columns nicely (lat/long early, derived fields together)
    preferred_order = [
        "title", "price", "price_numeric", "acres", "cost_per_acre",
        "size", "address", "province",
        "latitude", "longitude",
        "detail_url", "source", "scraped_at", "source_file"
    ]
    existing = [c for c in preferred_order if c in combined.columns]
    others = [c for c in combined.columns if c not in existing]
    combined = combined[existing + others]

    # Output
    out_dir = Path(output_dir) if output_dir else Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / output_name
    combined.to_csv(csv_path, index=False)
    logger.info("Wrote %s (%d rows)", csv_path, len(combined))

    if also_parquet:
        try:
            parquet_path = csv_path.with_suffix(".parquet")
            combined.to_parquet(parquet_path, index=False)
            logger.info("Wrote %s (for fast analytics)", parquet_path)
        except Exception as e:
            logger.warning("Parquet export failed (install pyarrow?): %s", e)

    return csv_path


def main():
    parser = argparse.ArgumentParser(description="Consolidate farm listings across sources")
    parser.add_argument("--data-dir", default="data", help="Directory containing *_listings_*.csv files")
    parser.add_argument("--out-dir", default=None, help="Output directory (defaults to data-dir)")
    parser.add_argument("--output", default="consolidated_farm_listings.csv", help="Output CSV filename")
    parser.add_argument("--no-parquet", action="store_true", help="Skip Parquet output")
    args = parser.parse_args()

    consolidate(
        data_dir=args.data_dir,
        output_dir=args.out_dir,
        output_name=args.output,
        also_parquet=not args.no_parquet,
    )


if __name__ == "__main__":
    main()
