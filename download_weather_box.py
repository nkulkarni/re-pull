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
   - area_weather_{hourly or daily}_YYYY-MM.csv : the main weather data for the box
   - station_<id>_{hourly or daily}_data.csv : per-station historical data (if available)

The script has **built-in smart chunking** so you (or your non-technical users) don't have to worry about API limits or choosing chunk sizes.
It will automatically break huge date ranges (even 50+ years of hourly data) into safe small pieces.
It is also **resumable**: if you stop it or it hits a temporary problem (or rate limit), just run the exact same command again and it will skip chunks it already successfully downloaded and continue.
The script prints big friendly warnings for huge requests. It also writes a simple download_summary.txt in the output folder.

Requirements:
- requests (already in project requirements.txt)
- A Visual Crossing API key (sign up at https://www.visualcrossing.com/)
- Set environment variable: VISUALCROSSING_API_KEY=your_key_here
- Add --plan paid if you have a paid metered subscription (allows much larger/faster pulls and more stations per query).

Usage examples:
    python download_weather_box.py
    # Small safe test (recommended first for non-technical users)
    python download_weather_box.py --start 2024-01-01 --end 2024-01-31
    # Custom box + paid metered plan (recommended for serious historical pulls)
    python download_weather_box.py \
        --plan paid \
        --lat-min 42.0 --lat-max 44.5 --lon-min -82.5 --lon-max -79.0 \
        --start 2020-01-01 --end 2025-12-31

    # For a very small box (e.g. 500m radius) where no stations are inside:
    # Allow stations up to 2000 METERS (2 km) outside the box
    python download_weather_box.py \
        --lat-min 44.0802 --lat-max 44.0892 \
        --lon-min -76.9657 --lon-max -76.9532 \
        --max-station-distance-from-box 2000 \
        --start 2024-07-01 --end 2024-07-07

The script uses metric units by default. Change --unit-group if needed.
It defaults to hourly (what you asked for). Use --resolution daily for much smaller/faster downloads.

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
import sys
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


def estimate_records_and_cost(days, num_stations, resolution, is_paid):
    """Rough cost estimate for non-technical users."""
    hours = days * 24
    area_recs = hours
    per_station_recs = hours * num_stations if num_stations else hours * 5  # rough guess
    total_recs = area_recs + per_station_recs
    cost_per_record = 0.0001  # metered rate
    total_cost = total_recs * cost_per_record
    daily_free = 1000
    free_recs = daily_free * days
    billable_recs = max(0, total_recs - free_recs)
    billable_cost = billable_recs * cost_per_record
    return {
        'hours': hours,
        'area': area_recs,
        'per_station': per_station_recs,
        'total': total_recs,
        'est_total_cost': total_cost,
        'est_billable_cost': billable_cost
    }


def filter_stations_by_box(all_stations, lat_min, lat_max, lon_min, lon_max, max_dist_outside):
    """Return stations inside box or within max_dist_outside meters of it."""
    def station_is_acceptable(s):
        slat = s["latitude"]
        slon = s["longitude"]
        if lat_min <= slat <= lat_max and lon_min <= slon <= lon_max:
            return True, 0.0
        closest_lat = max(lat_min, min(slat, lat_max))
        closest_lon = max(lon_min, min(slon, lon_max))
        dist = haversine_distance(slat, slon, closest_lat, closest_lon)
        return (dist <= max_dist_outside), dist

    acceptable = []
    for s in all_stations:
        ok, dist = station_is_acceptable(s)
        s = s.copy()
        s["distance_to_box_m"] = round(dist) if dist is not None else 999999
        if ok:
            acceptable.append(s)
    return acceptable

def discover_stations_in_box(api_key, lat_min, lat_max, lon_min, lon_max,
                             max_distance=None, max_stations=None, sample_date="2024-07-01",
                             station_search_radius=50000):
    """
    Discover weather stations around the box by querying the center point with
    large max* settings. Returns the list of stations found in the search radius.
    Filtering by box + buffer happens in main() so we can prompt interactively.
    """
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2
    location = f"{center_lat},{center_lon}"

    if max_distance is None:
        max_distance = station_search_radius or 150000   # 150 km
    if max_stations is None:
        max_stations = 30

    url = f"{API_BASE}/{location}/{sample_date}/{sample_date}"
    params = {
        "key": api_key,
        "include": "days,obs,stations",
        "maxDistance": max_distance,      # meters
        "maxStations": max_stations,
        "unitGroup": "metric",
        "contentType": "json",
        "options": "collectStationContributions",
    }

    logger.info(f"Discovering stations near center {center_lat:.4f},{center_lon:.4f} "
                f"(search radius={max_distance}m, maxStations={max_stations})")
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
            "contribution": sinfo.get("contribution", 100),
        })

    return all_stations

def fetch_timeline_chunk(api_key, location, start_date, end_date, include="hours,obs,stations",
                         unit_group="metric", extra_params=None, max_retries=5):
    """Fetch one chunk of historical data, with automatic retries for rate limits / transient errors.
    This makes large historical pulls much more reliable for non-technical users.
    """
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

    for attempt in range(1, max_retries + 1):
        logger.debug(f"Fetching {start_date} to {end_date} for {location} (attempt {attempt}/{max_retries})")
        try:
            resp = requests.get(url, params=params, timeout=60)
            if resp.status_code == 429:
                wait = 30 * attempt
                logger.warning(f"Rate limit hit (429). Waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 10 * attempt
                logger.warning(f"Server error {resp.status_code}. Waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries:
                logger.error(f"Failed to fetch {start_date}-{end_date} after {max_retries} attempts: {e}")
                raise
            wait = 5 * attempt
            logger.warning(f"Network error: {e}. Retrying in {wait}s (attempt {attempt})...")
            time.sleep(wait)
    return {}

def download_historical_chunks(api_key, location, start_date, end_date, chunk_days=None,
                               include="hours,obs,stations", unit_group="metric",
                               output_dir: Path = None, resolution_label="hourly",
                               is_paid=False):
    """
    Smart, automatic chunking for large time periods.
    Non-technical users should not have to think about chunk sizes.

    - Chooses safe chunk size automatically based on resolution + whether you have a paid plan.
    - Paid users get significantly larger chunks (faster, fewer calls).
    - Breaks the full requested range into small safe API calls.
    - **Resumable**: Skips any monthly output file that already exists and has data (great if run is interrupted).
    - Good logging so users see progress.
    - Returns the collected records + last stations info.
    """
    if chunk_days is None:
        if is_paid:
            chunk_days = 90 if "hours" in include else 1825   # ~5 years for daily on paid
        else:
            chunk_days = 14 if "hours" in include else 365

    all_records = []
    last_stations = {}

    current = datetime.fromisoformat(start_date)
    end = datetime.fromisoformat(end_date)
    total_days = (end - current).days + 1
    approx_chunks = max(1, (total_days + chunk_days - 1) // chunk_days)

    logger.info(f"Downloading {resolution_label} data from {start_date} to {end_date} for {location}")
    logger.info(f"Plan: {'paid' if is_paid else 'free'} → using chunks of ~{chunk_days} days → approx {approx_chunks} API calls.")

    if "hours" in include and total_days > 365:
        if is_paid:
            logger.warning("You requested many years of HOURLY data on a paid plan. This will still create large files.")
            logger.warning("The script will chunk automatically and can resume. Monitor your Visual Crossing usage dashboard.")
        else:
            logger.warning("!!! You requested many years of HOURLY data on the FREE plan. This will likely hit limits.")
            logger.warning("    Strongly consider using --resolution daily or a much shorter date range.")

    chunk_num = 0
    sleep_time = 0.3 if is_paid else 0.8   # paid users can go a bit faster

    while current < end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        date1 = current.strftime("%Y-%m-%d")
        date2 = chunk_end.strftime("%Y-%m-%d")
        chunk_num += 1

        # --- RESUMABILITY: check for existing monthly file(s) ---
        if output_dir:
            year_months = set()
            d = current
            while d <= chunk_end:
                year_months.add(d.strftime("%Y-%m"))
                d += timedelta(days=1)
            existing = any((output_dir / f"area_weather_{resolution_label}_{ym}.csv").exists() for ym in year_months)
            if existing:
                logger.info(f"[{chunk_num}/{approx_chunks}] Skipping {date1}–{date2} (output files already exist)")
                current = chunk_end + timedelta(days=1)
                continue

        try:
            data = fetch_timeline_chunk(
                api_key, location, date1, date2, include=include, unit_group=unit_group
            )
            days = data.get("days", [])
            chunk_count = 0
            for day in days:
                hours = day.get("hours", [])
                for hour in hours:
                    hour["day"] = day.get("datetime")
                    all_records.append(hour)
                    chunk_count += 1
            if "stations" in data:
                last_stations = data["stations"]
            logger.info(f"[{chunk_num}/{approx_chunks}] Got {chunk_count} {resolution_label} records for {date1}–{date2}")
        except Exception as e:
            logger.error(f"[{chunk_num}/{approx_chunks}] FAILED chunk {date1}–{date2}: {e}")
            logger.error("    (Will continue with next chunk. You can re-run later to resume.)")

        current = chunk_end + timedelta(days=1)
        time.sleep(sleep_time)

    return all_records, last_stations

def main():
    parser = argparse.ArgumentParser(description="Download historical weather station data for a lat/lon box using Visual Crossing.")
    parser.add_argument("--lat-min", type=float, default=42.0, help="Minimum latitude of box")
    parser.add_argument("--lat-max", type=float, default=44.5, help="Maximum latitude of box")
    parser.add_argument("--lon-min", type=float, default=-82.5, help="Minimum longitude of box (negative for west)")
    parser.add_argument("--lon-max", type=float, default=-79.0, help="Maximum longitude of box")
    parser.add_argument("--start", default="2020-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--chunk-days", type=int, default=None,
                        help="ADVANCED: Override automatic chunk size in days. Leave empty for smart defaults (recommended for non-technical users).")
    parser.add_argument("--output-dir", default="weather_data", help="Directory to save CSVs")
    parser.add_argument("--unit-group", default="metric", choices=["us", "uk", "metric", "base"])
    parser.add_argument("--resolution", default="hourly", choices=["hourly", "daily"],
                        help="hourly (default - what you want, but creates big files) or daily (much smaller and faster, good for testing large date ranges first)")
    parser.add_argument("--plan", default="paid", choices=["free", "paid"],
                        help="Your Visual Crossing plan. Default is now 'paid' since you upgraded. Use 'free' only if you're on the free tier.")
    parser.add_argument("--max-stations", type=int, default=None,
                        help="ADVANCED: Max number of weather stations to consider (Visual Crossing default is 3). Paid plans allow higher values.")
    parser.add_argument("--max-distance", type=int, default=None,
                        help="ADVANCED: Max distance in meters to search for stations (default ~80km). Paid plans allow higher values (e.g. 200000 for 200km).")
    parser.add_argument("--station-search-radius", type=int, default=50000,
                        help="How far from the CENTER of your box to ask the weather service to look for stations (in METERS, default 50,000 = 50 km). "
                             "This is the initial search radius. Then the --max-station-distance-from-box filter is applied. "
                             "Increase this (e.g. 100000 for 100 km) in very rural areas.")
    parser.add_argument("--max-station-distance-from-box", type=int, default=0,
                        help="How far OUTSIDE your exact box, in METERS, to accept weather stations. "
                             "0 = strictly inside box only (default). "
                             "For small boxes (e.g. 500 m radius), set this >0. "
                             "THE NUMBER IS ALWAYS IN METERS (not km). "
                             "2000 = 2000 meters = 2 km. "
                             "Example: --max-station-distance-from-box 2000   # allow stations up to 2 km outside the box")
    args = parser.parse_args()

    api_key = get_api_key()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print("WEATHER DOWNLOAD FOR A BOX (Visual Crossing)")
    print(f"{'='*60}")
    print(f"Box:          lat {args.lat_min} to {args.lat_max}, lon {args.lon_min} to {args.lon_max}")
    print(f"Date range:   {args.start} to {args.end}")
    print(f"Resolution:   {args.resolution}")
    print(f"Plan:         {args.plan}")
    print(f"Output folder: {output_dir}")
    print(f"{'='*60}\n")

    # Make resolution label available for later prints and filenames even if some blocks are skipped
    res_label = "hourly" if args.resolution == "hourly" else "daily"

    # Compute days early for estimates and interactive
    try:
        start_dt = datetime.fromisoformat(args.start)
        end_dt = datetime.fromisoformat(args.end)
        days = (end_dt - start_dt).days + 1
    except Exception:
        days = 0
        start_dt = end_dt = None

    # Cost estimate using the helper (preliminary, assumes ~5 stations)
    if days > 0:
        try:
            est = estimate_records_and_cost(days, 5, args.resolution, args.plan == "paid")
            print("Preliminary cost estimate (before exact # of stations):")
            print(f"  ~{est['hours']:,} hours → ~{est['total']:,} records (area + ~5 stations)")
            print(f"  Rough total cost: ${est['est_total_cost']:.2f}")
            print(f"  After daily free allowance (~{1000*days:,} records): ~${est['est_billable_cost']:.2f}")
            print(f"  (Will refine after discovering actual stations in the box.)\n")
        except Exception:
            pass

    # Friendly warning for non-technical users doing big pulls
    try:
        start_dt = datetime.fromisoformat(args.start)
        end_dt = datetime.fromisoformat(args.end)
        days = (end_dt - start_dt).days + 1
        if args.resolution == "hourly" and days > 365:
            print("!!! BIG WARNING !!!")
            print(f"You asked for {days} days of HOURLY data.")
            print("This can create VERY LARGE files (hundreds of MB or GB).")
            if args.plan == "paid":
                print("You are on a paid plan, so you can pull more aggressively.")
            print("The script will chunk it automatically and can resume if stopped.")
            print("If this is your first time, consider a shorter range first (e.g. 1-3 months).")
            print("Waiting 5 seconds before starting... (press Ctrl-C to cancel)")
            time.sleep(5)
    except Exception:
        pass

    # 1. Discover stations in/near the box
    center_lat = (args.lat_min + args.lat_max) / 2
    center_lon = (args.lon_min + args.lon_max) / 2
    # For paid plans we can safely ask for more stations / farther distance
    if args.plan == "paid":
        default_max_stations = 50
        default_max_distance = 200000
    else:
        default_max_stations = 30
        default_max_distance = 150000

    effective_search_radius = args.station_search_radius or default_max_distance
    candidates = discover_stations_in_box(
        api_key, args.lat_min, args.lat_max, args.lon_min, args.lon_max,
        max_distance=effective_search_radius,
        max_stations=args.max_stations or default_max_stations
    )

    # Filter with current value (may be 0)
    stations = filter_stations_by_box(
        candidates, args.lat_min, args.lat_max, args.lon_min, args.lon_max,
        args.max_station_distance_from_box
    )

    if stations:
        stations_df = pd.DataFrame(stations)
        stations_path = output_dir / "stations_in_box.csv"
        stations_df.to_csv(stations_path, index=False)
        print(f"Saved {len(stations)} stations to {stations_path}")
        print(stations_df[["station_id", "name", "latitude", "longitude", "distance_m"]].head())

        # Better cost estimate now that we know number of stations
        try:
            start_dt = datetime.fromisoformat(args.start)
            end_dt = datetime.fromisoformat(args.end)
            days = (end_dt - start_dt).days + 1
            num_st = len(stations)
            est = estimate_records_and_cost(days, num_st, args.resolution, args.plan == "paid")
            print(f"\nUpdated cost estimate (with {num_st} stations in box):")
            print(f"  Area query: ~{est['area']:,} records")
            print(f"  Per-station queries: ~{est['station_recs']:,} records")
            print(f"  Total estimated: ~{est['total']:,} records")
            print(f"  Estimated cost at $0.0001/record: ${est['est_total_cost']:.2f}")
            print(f"  After daily free allowance: ~${est['est_billable_cost']:.2f}")
            print(f"  (Upper bound; many per-station queries may return partial or cached data. Check your Visual Crossing usage dashboard.)\n")
        except Exception:
            pass
    else:
        print("No stations found inside your box (or within the --max-station-distance-from-box you allowed).")
        print("For a tiny box like 500 m, you will almost always need to allow some stations from outside.")
        print("The number for --max-station-distance-from-box is in METERS.")
        print("   Example: --max-station-distance-from-box 2000   means allow stations up to 2000 meters (2 km) outside the box.")
        print("You can also increase --station-search-radius (default searches 50 km around the center of the box).")

        if candidates and sys.stdin.isatty():
            # Interactive prompt for non-technical users
            closest = min(candidates, key=lambda s: haversine_distance(
                (args.lat_min + args.lat_max)/2, (args.lon_min + args.lon_max)/2,
                s["latitude"], s["longitude"]
            ))
            closest_dist = haversine_distance(
                (args.lat_min + args.lat_max)/2, (args.lon_min + args.lon_max)/2,
                closest["latitude"], closest["longitude"]
            )
            print(f"\nClosest station found in search is about {int(closest_dist)} meters from the center of your box.")
            try:
                answer = input("Would you like to include stations outside the box? Enter max meters outside (e.g. 2000) or press Enter for 2000m, or 'n' to skip: ").strip()
                if answer.lower() in ('', 'y', 'yes'):
                    new_dist = 2000
                elif answer.lower() in ('n', 'no'):
                    new_dist = 0
                else:
                    new_dist = int(answer)
                if new_dist > 0:
                    stations = filter_stations_by_box(candidates, args.lat_min, args.lat_max, args.lon_min, args.lon_max, new_dist)
                    if stations:
                        args.max_station_distance_from_box = new_dist  # for logging
                        stations_df = pd.DataFrame(stations)
                        stations_path = output_dir / "stations_in_box.csv"
                        stations_df.to_csv(stations_path, index=False)
                        print(f"Using stations up to {new_dist}m outside the box. Saved {len(stations)} stations to {stations_path}")
                        print(stations_df[["station_id", "name", "latitude", "longitude", "distance_m"]].head())
                        # Re-do cost estimate
                        try:
                            days = (end_dt - start_dt).days + 1
                            num_st = len(stations)
                            est = estimate_records_and_cost(days, num_st, args.resolution, args.plan == "paid")
                            print(f"\nUpdated cost estimate (with {num_st} stations): ~${est['est_total_cost']:.2f} total, ~${est['est_billable_cost']:.2f} billable after free allowance.\n")
                        except:
                            pass
            except (EOFError, ValueError, KeyboardInterrupt):
                print("Skipping outside stations.")
        stations = stations if 'stations' in locals() else []

    # 2. Download the main "area" historical data (blended from stations near the box)
    # Determine resolution settings (user-friendly: default hourly, but support daily)
    if args.resolution == "hourly":
        include = "hours,obs,stations"
        res_label = "hourly"
    else:
        include = "days,obs,stations"
        res_label = "daily"

    print(f"Resolution: {res_label} (you can change with --resolution daily)")

    location = f"{center_lat},{center_lon}"
    print(f"\nDownloading area weather data for representative location {location} ...")
    records, last_stations = download_historical_chunks(
        api_key, location, args.start, args.end,
        chunk_days=args.chunk_days,
        include=include,
        unit_group=args.unit_group,
        output_dir=output_dir,
        resolution_label=res_label,
        is_paid=(args.plan == "paid")
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
            ym_path = output_dir / f"area_weather_{res_label}_{ym}.csv"
            group.drop(columns=["year_month"]).to_csv(ym_path, index=False)
            print(f"  Saved {len(group)} {res_label} rows for {ym} -> {ym_path}")

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
                station_include = "hours,obs" if args.resolution == "hourly" else "days,obs"
                station_days, _ = download_historical_chunks(
                    api_key, sid, args.start, args.end,
                    chunk_days=args.chunk_days,
                    include=station_include,
                    unit_group=args.unit_group,
                    output_dir=output_dir,
                    resolution_label=args.resolution,
                    is_paid=(args.plan == "paid")
                )
                if station_days:
                    sdf = pd.DataFrame(station_days)
                    sdf["station_id"] = sid
                    sdf["station_name"] = station.get("name", "")
                    sdf["station_lat"] = station["latitude"]
                    sdf["station_lon"] = station["longitude"]
                    sfile = output_dir / f"station_{sid}_{res_label}_data.csv"
                    sdf.to_csv(sfile, index=False)
                    print(f"  Saved {len(sdf)} {res_label} rows for station {sid} -> {sfile}")
                else:
                    print(f"  No data for station {sid} (may not support direct station queries or no data in range)")
            except Exception as e:
                print(f"  Could not fetch direct data for station {sid}: {e}")
                # This is common; many stations are only accessible via the blended query.

    print("\n=== Demo complete ===")
    print(f"Check the '{output_dir}' directory for CSVs.")
    print(f"The 'area_weather_{res_label}_*.csv' files contain the main historical *{res_label}* records based on stations in/near your box.")
    print("The per-station files (if any) contain more direct station observations.")
    print("The script is designed to be re-runnable: it will automatically skip chunks whose output files already exist.")
    print("If you had errors or interruptions, just run the exact same command again — it will continue where it left off.")
    if args.plan == "paid":
        print("Paid plan detected — using larger chunks for faster downloads.")

    # Simple summary file for non-technical users
    try:
        summary_path = output_dir / "download_summary.txt"
        with summary_path.open("w") as f:
            f.write(f"Download completed: {datetime.now().isoformat()}\n")
            f.write(f"Box: lat {args.lat_min}-{args.lat_max}, lon {args.lon_min}-{args.lon_max}\n")
            f.write(f"Requested: {args.start} to {args.end} ({args.resolution})\n")
            f.write(f"Plan: {args.plan}\n")
            f.write(f"Total records downloaded this run: {len(records) if 'records' in locals() else 'N/A'}\n")
            f.write("Files created are safe to open in Excel or Google Sheets.\n")
        print(f"Summary written to {summary_path}")
    except Exception:
        pass

if __name__ == "__main__":
    main()