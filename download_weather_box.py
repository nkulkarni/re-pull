#!/usr/bin/env python3
"""
Demo script: Download historical weather station data for a lat/long bounding box
using the Visual Crossing Weather API.

This script:
1. Discovers weather stations within or near your bounding box by querying a central
   point with generous maxStations and maxDistance settings.
2. Filters the stations to those strictly inside the box.
3. Downloads the "area" historical weather data (blended from stations in/near the box)
   for the requested date range, chunked (e.g. by month for hourly data) to respect API limits.
4. Attempts to download raw historical observations for each individual station in the box
   (using the station ID as location where supported by the API).
5. Saves everything as CSV files for easy analysis:
   - stations_in_box.csv : list of stations with coords, names, distances
   - area_weather_hourly_YYYY-MM.csv : the main weather data for the box (hourly)
   - station_<id>_hourly_data.csv : per-station historical data (if available)

" All available historical" is handled by chunking (e.g. monthly for hourly). 
Full 50+ years of *hourly* data will generate enormous files and will almost certainly hit your plan's row limits or rate limits.
Start with a small date range (e.g. last 1-3 years or even a few months) and scale up carefully. Hourly data is 24x larger than daily.

Requirements:
- requests (already in project requirements.txt)
- A Visual Crossing API key (sign up at https://www.visualcrossing.com/ for free tier)
- Set environment variable: VISUALCROSSING_API_KEY=your_key_here

Usage examples:
    python download_weather_box.py
    python download_weather_box.py --start 2023-01-01 --end 2023-03-31   # small test range for hourly
    python download_weather_box.py --lat-min 42.0 --lat-max 44.5 --lon-min -82.5 --lon-max -79.0

The script uses metric units by default. Change unitGroup if needed.
Hourly data is requested by default (include=hours,obs,stations).

Note on "weather station data":
Visual Crossing primarily returns high-quality interpolated/blended data from the nearest
stations (obs + remote sources). The response includes which stations contributed.
We also try to pull per-station observations where the station ID can be used as a location.
Hourly data is requested (this matches your request for hour-level, not daily).
This gives you the closest thing to "all station data in the box".
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
# For multiple locations if needed later: "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timelinemulti"

def get_api_key():
    key = os.getenv("VISUALCROSSING_API_KEY") or os.getenv("WEATHER_API_KEY")
    if not key:
        raise ValueError(
            "Please set the VISUALCROSSING_API_KEY environment variable with your API key "
            "from https://www.visualcrossing.com/"
        )
    return key

def haversine_distance(lat1, lon1, lat2, lon2):
    """Simple distance in meters (approximate)."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000  # Earth radius in meters
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c

def discover_stations_in_box(api_key, lat_min, lat_max, lon_min, lon_max,
                             max_distance=150000, max_stations=30, sample_date="2024-07-01"):
    """
    Discover weather stations around the box by querying the center point with
    large max* settings. Then filter to stations inside the box.
    """
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2
    location = f"{center_lat},{center_lon}"

    url = f"{API_BASE}/{location}/{sample_date}/{sample_date}"
    params = {
        "key": api_key,
        "include": "days,obs,stations",
        "maxDistance": max_distance,      # meters
        "maxStations": max_stations,
        "unitGroup": "metric",
        "contentType": "json",
        "options": "collectStationContributions",  # helps get contribution info
    }

    logger.info(f"Discovering stations near center {center_lat:.4f},{center_lon:.4f} "
                f"(maxDistance={max_distance}m, maxStations={max_stations})")
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    stations_dict = data.get("stations", {})
    all_stations = []
    for sid, sinfo in stations_dict.items():
        slat = sinfo.get("latitude")
        slon = sinfo.get("longitude")
        if slat is None or slon is None:
            continue
        distance = haversine_distance(center_lat, center_lon, slat, slon)
        all_stations.append({
            "station_id": sid,
            "name": sinfo.get("name", ""),
            "latitude": slat,
            "longitude": slon,
            "distance_m": round(distance),
            "elevation": sinfo.get("elevation"),
            "contribution": sinfo.get("contribution", 100),  # percentage in some responses
        })

    # Filter to inside the box
    in_box = [
        s for s in all_stations
        if lat_min <= s["latitude"] <= lat_max and lon_min <= s["longitude"] <= lon_max
    ]

    logger.info(f"Discovered {len(all_stations)} stations in search radius; "
                f"{len(in_box)} strictly inside the box.")
    return in_box

def fetch_timeline_chunk(api_key, location, start_date, end_date, include="days,obs,stations",
                         unit_group="metric", extra_params=None):
    """Fetch one chunk of historical data."""
    url = f"{API_BASE}/{location}/{start_date}/{end_date}"
    params = {
        "key": api_key,
        "include": include,
        "unitGroup": unit_group,
        "contentType": "json",
        "maxDistance": 150000,
        "maxStations": 30,
    }
    if extra_params:
        params.update(extra_params)

    logger.debug(f"Fetching {start_date} to {end_date} for {location}")
    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code == 429:
        logger.warning("Rate limit hit. Sleeping 60s...")
        time.sleep(60)
        return fetch_timeline_chunk(api_key, location, start_date, end_date, include, unit_group, extra_params)
    resp.raise_for_status()
    return resp.json()

def download_historical_chunks(api_key, location, start_date, end_date, chunk_days=30,
                               include="hours,obs,stations", unit_group="metric"):
    """
    Download historical data in time chunks to avoid row limits.
    Defaults to hourly data (include=hours,obs,stations) and smaller chunks.
    Collects hourly records from the nested 'hours' arrays inside each 'day'.
    Returns list of hourly records + the stations dict from the last successful response.
    """
    all_records = []  # will hold hourly records
    last_stations = {}

    current = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)

    while current < end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        date1 = current.strftime("%Y-%m-%d")
        date2 = chunk_end.strftime("%Y-%m-%d")

        try:
            data = fetch_timeline_chunk(
                api_key, location, date1, date2, include=include, unit_group=unit_group
            )
            days = data.get("days", [])
            chunk_count = 0
            for day in days:
                hours = day.get("hours", [])
                for hour in hours:
                    # Optionally attach the day's date or other metadata
                    hour["day"] = day.get("datetime")
                    all_records.append(hour)
                    chunk_count += 1
            if "stations" in data:
                last_stations = data["stations"]
            logger.info(f"  Got {chunk_count} hourly records for {date1}–{date2}")
        except Exception as e:
            logger.error(f"  Failed chunk {date1}–{date2}: {e}")
            # Continue with next chunk

        current = chunk_end + timedelta(days=1)
        # Be nice to the API (important for hourly which returns more data)
        time.sleep(1)

    return all_records, last_stations

def main():
    parser = argparse.ArgumentParser(description="Download historical weather station data for a lat/lon box using Visual Crossing.")
    parser.add_argument("--lat-min", type=float, default=42.0, help="Minimum latitude of box")
    parser.add_argument("--lat-max", type=float, default=44.5, help="Maximum latitude of box")
    parser.add_argument("--lon-min", type=float, default=-82.5, help="Minimum longitude of box (negative for west)")
    parser.add_argument("--lon-max", type=float, default=-79.0, help="Maximum longitude of box")
    parser.add_argument("--start", default="2020-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--chunk-days", type=int, default=14, help="Days per API chunk. For hourly data use small values like 7-30 to avoid row limits (default 14 for hourly)")
    parser.add_argument("--output-dir", default="weather_data", help="Directory to save CSVs")
    parser.add_argument("--unit-group", default="metric", choices=["us", "uk", "metric", "base"])
    args = parser.parse_args()

    api_key = get_api_key()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Bounding box: lat [{args.lat_min}, {args.lat_max}], lon [{args.lon_min}, {args.lon_max}]")
    print(f"Date range: {args.start} to {args.end}")
    print(f"Output dir: {output_dir}")

    # 1. Discover stations in/near the box
    center_lat = (args.lat_min + args.lat_max) / 2
    center_lon = (args.lon_min + args.lon_max) / 2
    stations = discover_stations_in_box(
        api_key, args.lat_min, args.lat_max, args.lon_min, args.lon_max
    )

    if stations:
        stations_df = pd.DataFrame(stations)
        stations_path = output_dir / "stations_in_box.csv"
        stations_df.to_csv(stations_path, index=False)
        print(f"Saved {len(stations)} stations to {stations_path}")
        print(stations_df[["station_id", "name", "latitude", "longitude", "distance_m"]].head())
    else:
        print("No stations found in box. Try increasing the search radius or adjusting the box.")
        stations = []

    # 2. Download the main "area" historical data (blended from stations near the box)
    location = f"{center_lat},{center_lon}"
    print(f"\nDownloading area weather data for representative location {location} ...")
    records, last_stations = download_historical_chunks(
        api_key, location, args.start, args.end,
        chunk_days=args.chunk_days,
        include="hours,obs,stations",
        unit_group=args.unit_group
    )

    if records:
        area_df = pd.DataFrame(records)
        # Add some metadata
        area_df["query_center_lat"] = center_lat
        area_df["query_center_lon"] = center_lon
        area_df["box_lat_min"] = args.lat_min
        area_df["box_lat_max"] = args.lat_max
        area_df["box_lon_min"] = args.lon_min
        area_df["box_lon_max"] = args.lon_max

        # Save per month for manageability with hourly data (very large files)
        area_df["year_month"] = pd.to_datetime(area_df["datetime"]).dt.strftime("%Y-%m")
        for ym, group in area_df.groupby("year_month"):
            ym_path = output_dir / f"area_weather_hourly_{ym}.csv"
            group.drop(columns=["year_month"]).to_csv(ym_path, index=False)
            print(f"  Saved {len(group)} hourly rows for {ym} -> {ym_path}")

        # Also save the stations that were used in the queries
        if last_stations:
            used_df = pd.DataFrame([{"station_id": k, **v} for k, v in last_stations.items()])
            used_path = output_dir / "stations_used_in_area_queries.csv"
            used_df.to_csv(used_path, index=False)
            print(f"  Saved stations used in area queries -> {used_path}")
    else:
        print("No area weather data retrieved.")

    # 3. (Optional but powerful) Try to download per-station historical data for stations inside the box
    if stations:
        print(f"\nAttempting to download per-station historical data for {len(stations)} stations in box...")
        for station in stations:
            sid = station["station_id"]
            try:
                # Many station IDs (especially ICAO or the numeric ones) work as location values
                station_days, _ = download_historical_chunks(
                    api_key, sid, args.start, args.end,
                    chunk_days=args.chunk_days,
                    include="hours,obs",
                    unit_group=args.unit_group
                )
                if station_days:
                    sdf = pd.DataFrame(station_days)
                    sdf["station_id"] = sid
                    sdf["station_name"] = station.get("name", "")
                    sdf["station_lat"] = station["latitude"]
                    sdf["station_lon"] = station["longitude"]
                    sfile = output_dir / f"station_{sid}_hourly_data.csv"
                    sdf.to_csv(sfile, index=False)
                    print(f"  Saved {len(sdf)} hourly rows for station {sid} -> {sfile}")
                else:
                    print(f"  No data for station {sid} (may not support direct station queries or no data in range)")
            except Exception as e:
                print(f"  Could not fetch direct data for station {sid}: {e}")
                # This is common; many stations are only accessible via the blended query.

    print("\n=== Demo complete ===")
    print(f"Check the '{output_dir}' directory for CSVs.")
    print("The 'area_weather_hourly_*.csv' files contain the main historical *hourly* records based on stations in/near your box.")
    print("The per-station files (if any) contain more direct station hourly observations.")

if __name__ == "__main__":
    main()