#!/usr/bin/env python3
"""
Consolidation engine for the re-pull scraping project.

Core idea: A single growing "master" data asset (CSV + Parquet) that you
append to over time. The engine understands the diff via detail_url.

Two main modes:
- consolidate()      → full rebuild from all per-source CSVs (rarely needed)
- update_master()    → the smart "append new stuff only" function used by the engine

The master always has:
- latitude / longitude columns (NaN if a source didn't provide them)
- acres (parsed from size)
- cost_per_acre (price_numeric / acres)

Usage from the engine:
    python run.py                 # runs all sources in append mode, updates master
    python run.py --fresh         # full refresh of all sources + master
"""

import argparse
import glob
import logging
from pathlib import Path
from typing import Iterable

import pandas as pd

from sources.base import parse_acres

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MASTER_FILENAME = "master_listings.csv"


def load_and_merge_sources(data_dir: str = "data") -> pd.DataFrame:
    """Load every *_listings_*.csv and concat them."""
    pattern = str(Path(data_dir) / "*_listings_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()

    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            df["source_file"] = Path(f).name
            dfs.append(df)
        except Exception as e:
            logger.warning("Failed to load %s: %s", f, e)
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


def enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee lat/long columns + compute acres + cost_per_acre."""
    if df.empty:
        return df

    for col in ["latitude", "longitude"]:
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "acres" not in df.columns:
        df["acres"] = pd.NA
    mask = df["acres"].isna() & df.get("size", pd.Series(dtype=object)).notna()
    df.loc[mask, "acres"] = df.loc[mask, "size"].apply(parse_acres)

    if "price_numeric" in df.columns and "acres" in df.columns:
        df["cost_per_acre"] = df.apply(
            lambda r: round(r["price_numeric"] / r["acres"], 2)
            if pd.notna(r.get("price_numeric")) and pd.notna(r.get("acres")) and r["acres"] > 0
            else pd.NA,
            axis=1,
        )
    return df


def deduplicate_listings(df: pd.DataFrame, keep: str = "first") -> pd.DataFrame:
    """Dedup by detail_url. 'first' keeps the earliest scraped_at (stable asset)."""
    if df.empty or "detail_url" not in df.columns:
        return df
    df = df.sort_values("scraped_at", ascending=(keep == "first"))
    return df.drop_duplicates(subset=["detail_url"], keep=keep)


def consolidate(
    data_dir: str = "data",
    output_dir: str | None = None,
    output_name: str = "consolidated_farm_listings.csv",
    also_parquet: bool = True,
) -> Path:
    """Full rebuild (use occasionally if you want a clean slate from all snapshots)."""
    combined = load_and_merge_sources(data_dir)
    if combined.empty:
        logger.warning("Nothing to consolidate.")
        return Path()

    combined = enrich_dataframe(combined)
    combined = deduplicate_listings(combined, keep="first")

    preferred = [
        "title", "price", "price_numeric", "acres", "cost_per_acre",
        "size", "address", "province", "latitude", "longitude",
        "detail_url", "source", "scraped_at", "source_file"
    ]
    cols = [c for c in preferred if c in combined.columns] + [c for c in combined.columns if c not in preferred]
    combined = combined[cols]

    out_dir = Path(output_dir) if output_dir else Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / output_name
    combined.to_csv(csv_path, index=False)
    logger.info("Full consolidated master written: %s (%d rows)", csv_path, len(combined))

    if also_parquet:
        try:
            combined.to_parquet(csv_path.with_suffix(".parquet"), index=False)
        except Exception as e:
            logger.warning("Parquet write failed (pip install pyarrow): %s", e)
    return csv_path


def update_master(
    new_listings: pd.DataFrame,
    master_path: Path,
    also_parquet: bool = True,
) -> Path:
    """The key function for a growing data asset.

    Takes whatever the scrapers just produced, merges it intelligently
    into the master, deduplicates, and writes back.
    """
    if new_listings.empty:
        logger.info("No new listings to add to master.")
        return master_path

    new_listings = enrich_dataframe(new_listings.copy())

    if master_path.exists():
        try:
            master = pd.read_csv(master_path)
            combined = pd.concat([master, new_listings], ignore_index=True)
        except Exception as e:
            logger.warning("Could not read existing master (%s), starting over for this run.", e)
            combined = new_listings
    else:
        combined = new_listings

    combined = deduplicate_listings(combined, keep="first")

    # Stable column order
    preferred = [
        "title", "price", "price_numeric", "acres", "cost_per_acre",
        "size", "address", "province", "latitude", "longitude",
        "detail_url", "source", "scraped_at", "source_file"
    ]
    cols = [c for c in preferred if c in combined.columns] + \
           [c for c in combined.columns if c not in preferred]
    combined = combined[cols]

    master_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(master_path, index=False)
    logger.info("Master asset updated: %s (%d total rows)", master_path, len(combined))

    if also_parquet:
        try:
            combined.to_parquet(master_path.with_suffix(".parquet"), index=False)
        except Exception as e:
            logger.debug("Parquet update skipped: %s", e)

    return master_path


def main():
    parser = argparse.ArgumentParser(description="Consolidate / update the master farm listings asset")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--output", default="consolidated_farm_listings.csv")
    parser.add_argument("--no-parquet", action="store_true")
    args = parser.parse_args()

    consolidate(
        data_dir=args.data_dir,
        output_dir=args.out_dir,
        output_name=args.output,
        also_parquet=not args.no_parquet,
    )


if __name__ == "__main__":
    main()
