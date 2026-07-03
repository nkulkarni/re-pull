#!/usr/bin/env python3
"""
Consolidation engine for the re-pull scraping project.

Core idea: A single growing "master" data asset (CSV + Parquet) that you
append to over time. The engine understands the diff via detail_url.

Two main modes:
- consolidate()      → full rebuild from all per-source CSVs (rarely needed)
- update_master()    → the smart "append new stuff only" function used by the engine

The master always has:
- latitude / longitude columns (filled from source if available, else via Geocodio geocoding when coords are missing)
- acres (parsed from size)
- cost_per_acre (price_numeric / acres)
- geocode_provider (e.g. 'geocodio' or NaN if from source)

Usage from the engine:
    python run.py                 # runs all sources in append mode, updates master
    python run.py --fresh         # full refresh of all sources + master
"""

import argparse
import glob
import json
import logging
import os
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

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


def geocode_missing_coords(df: pd.DataFrame, api_key: str = None) -> pd.DataFrame:
    """Fill latitude/longitude using Geocodio for rows where they are missing from the source.

    Only geocodes when coords don't exist in the source data.
    Uses a simple JSON cache (data/geocode_cache.json) to avoid repeated API calls.
    Adds 'geocode_provider' column for provenance.
    """
    if df.empty:
        return df

    if api_key is None:
        api_key = os.getenv("GEOCODIO_API_KEY")
    if not api_key:
        logger.info("No GEOCODIO_API_KEY found in environment; skipping geocoding.")
        return df

    # Load cache
    cache_path = Path("data/geocode_cache.json")
    cache = {}
    if cache_path.exists():
        try:
            with cache_path.open() as f:
                cache = json.load(f)
        except Exception as e:
            logger.warning("Could not load geocode cache: %s", e)

    # Identify rows needing geocoding (source didn't provide coords)
    mask = (df.get("latitude").isna() | df.get("longitude").isna())
    to_geocode = df[mask].copy()

    if to_geocode.empty:
        logger.info("All rows already have coordinates from source.")
        return df

    logger.info("Geocoding %d rows with missing coordinates using Geocodio...", len(to_geocode))

    if "geocode_provider" not in df.columns:
        df["geocode_provider"] = pd.NA

    for idx in to_geocode.index:
        row = df.loc[idx]
        # Construct a good query. For legal descriptions, township focus helps.
        address = str(row.get("address", "")).strip()
        title = str(row.get("title", "")).strip()
        province = str(row.get("province", "Ontario")).strip()

        if address:
            query = f"{address}, {province}, Canada"
        else:
            query = f"{title}, {province}, Canada"

        cache_key = query.lower()

        if cache_key in cache:
            lat, lon = cache[cache_key][:2]
            df.at[idx, "latitude"] = lat
            df.at[idx, "longitude"] = lon
            df.at[idx, "geocode_provider"] = "geocodio (cached)"
            continue

        try:
            params = {"q": query, "api_key": api_key}
            resp = requests.get("https://api.geocod.io/v1.7/geocode", params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("results"):
                best = data["results"][0]
                lat = best["location"]["lat"]
                lon = best["location"]["lng"]
                # accuracy is a score 0-1 or similar in some responses
                accuracy = best.get("accuracy", best.get("confidence", None))

                df.at[idx, "latitude"] = lat
                df.at[idx, "longitude"] = lon
                df.at[idx, "geocode_provider"] = "geocodio"

                cache[cache_key] = (lat, lon, accuracy)
                logger.debug("Geocoded: %s -> (%s, %s)", query, lat, lon)
            else:
                logger.warning("No results for query: %s", query)
        except Exception as e:
            logger.warning("Geocoding failed for '%s': %s", query, e)

    # Save updated cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w") as f:
        json.dump(cache, f, indent=2)

    logger.info("Geocoding complete. Updated %d rows.", len(to_geocode))
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
    combined = geocode_missing_coords(combined)
    combined = deduplicate_listings(combined, keep="first")

    preferred = [
        "title", "price", "price_numeric", "acres", "cost_per_acre",
        "size", "address", "province", "latitude", "longitude",
        "detail_url", "source", "scraped_at", "geocode_provider", "source_file"
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
    new_listings = geocode_missing_coords(new_listings)

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
        "detail_url", "source", "scraped_at", "geocode_provider", "source_file"
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
