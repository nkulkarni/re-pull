#!/usr/bin/env python3
"""
Simple script to list available weather stations near a lat/long point within a given radius.

Uses the Visual Crossing Timeline API to discover stations (by querying a small date range
at the point with a generous search radius, then filtering by actual distance).

This is useful before downloading full historical data with download_weather_box.py,
especially for small boxes where no stations may be strictly inside.

Outputs:
- A nice printed table of stations (id, name, lat, lon, distance in meters, elevation if available).
- A CSV file: stations_near_<lat>_<lon>.csv (or custom via --output)

Usage (non-technical friendly):
    export VISUALCROSSING_API_KEY=your_key
    python list_weather_stations.py --lat 44.0847 --lon -76.9594 --radius-meters 2000

Defaults to the example point you provided (around 500m–2km radius).

Options:
    --lat, --lon          (required unless using defaults)
    --radius-meters       Default 2000 (2 km). Increase for rural areas.
    --max-stations        How many to ask the API for (default 50 on paid, 30 on free).
    --plan paid|free      Affects default max-stations (default paid).
    --output-dir          Where to save the CSV (default: data/ or current dir).
    --sample-date         Date to use for discovery query (default recent).

The script is resumable-safe and prints clear messages.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"


def get_api_key():
    key = os.getenv("VISUALCROSSING_API_KEY") or os.getenv("WEATHER_API_KEY")
    if not key:
        raise ValueError(
            "Please set the VISUALCROSSING_API_KEY environment variable "
            "(get a free/paid key at https://www.visualcrossing.com/)."
        )
    return key


def haversine_distance(lat1, lon1, lat2, lon2):
    """Approximate great-circle distance in meters."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


def list_stations_near_point(
    api_key,
    lat,
    lon,
    radius_meters=2000,
    max_stations=50,
    sample_date="2024-07-01",
    output_dir=None,
):
    """
    Query Visual Crossing at (lat, lon) for a tiny date range, requesting many stations.
    Filter the returned stations to those within radius_meters of the point.
    Return list of dicts and optionally save CSV.
    """
    location = f"{lat},{lon}"
    url = f"{API_BASE}/{location}/{sample_date}/{sample_date}"

    params = {
        "key": api_key,
        "include": "days,obs,stations",
        "maxDistance": max(radius_meters * 2, 50000),  # generous search to find candidates
        "maxStations": max_stations,
        "unitGroup": "metric",
        "contentType": "json",
        "options": "collectStationContributions",
    }

    logger.info(f"Querying stations near {lat:.6f},{lon:.6f} (search radius ~{params['maxDistance']}m, asking for up to {max_stations} stations)...")
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    stations_dict = data.get("stations", {})
    candidates = []
    for sid, sinfo in stations_dict.items():
        slat = sinfo.get("latitude")
        slon = sinfo.get("longitude")
        if slat is None or slon is None:
            continue
        dist = haversine_distance(lat, lon, slat, slon)
        candidates.append({
            "station_id": sid,
            "name": sinfo.get("name", ""),
            "latitude": slat,
            "longitude": slon,
            "distance_m": round(dist),
            "elevation": sinfo.get("elevation"),
            "contribution_pct": sinfo.get("contribution", 100),
        })

    # Filter to those inside the requested radius
    stations_in_radius = [s for s in candidates if s["distance_m"] <= radius_meters]
    stations_in_radius.sort(key=lambda s: s["distance_m"])

    logger.info(f"Found {len(candidates)} candidate stations in search area; {len(stations_in_radius)} within {radius_meters}m of the point.")

    if not stations_in_radius:
        logger.warning("No stations within the requested radius. Try increasing --radius-meters (or --max-station-distance-from-box when using the download script).")
        # Still return the closest few for info
        closest = sorted(candidates, key=lambda s: s["distance_m"])[:5]
        if closest:
            logger.info("Closest stations (for reference):")
            for s in closest:
                logger.info(f"  {s['station_id']} | {s['name']} | {s['distance_m']}m away | {s['latitude']:.4f},{s['longitude']:.4f}")
        return []

    # Print nice table
    print("\nStations within radius:")
    print(f"{'ID':<20} {'Name':<40} {'Dist (m)':>10} {'Lat':>10} {'Lon':>10}")
    print("-" * 95)
    for s in stations_in_radius:
        print(f"{s['station_id']:<20} {s['name'][:39]:<40} {s['distance_m']:>10} {s['latitude']:>10.4f} {s['longitude']:>10.4f}")

    # Save CSV
    if output_dir is None:
        output_dir = Path("data")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Safe filename
    safe_lat = f"{lat:.4f}".replace(".", "_").replace("-", "m")
    safe_lon = f"{lon:.4f}".replace(".", "_").replace("-", "m")
    csv_path = output_dir / f"stations_near_{safe_lat}_{safe_lon}_r{radius_meters}m.csv"

    import pandas as pd
    df = pd.DataFrame(stations_in_radius)
    df.to_csv(csv_path, index=False)
    print(f"\nSaved {len(stations_in_radius)} stations to: {csv_path}")
    print(f"(You can use these station IDs with the download script if desired, or just inspect them.)")

    return stations_in_radius


def main():
    parser = argparse.ArgumentParser(
        description="List weather stations near a lat/long point within a radius (using Visual Crossing)."
    )
    parser.add_argument("--lat", type=float, default=44.0847, help="Latitude (default: your example point)")
    parser.add_argument("--lon", type=float, default=-76.9594, help="Longitude (default: your example point)")
    parser.add_argument("--radius-meters", type=int, default=2000,
                        help="Radius in meters around the point (default 2000 = 2 km). "
                             "For 500 m around a point, try 500 here, but expect to use a larger value if 0 stations.")
    parser.add_argument("--max-stations", type=int, default=50,
                        help="How many stations to ask the API to consider (higher on paid plans).")
    parser.add_argument("--plan", default="paid", choices=["free", "paid"],
                        help="Affects default max-stations. Use 'paid' (default) for your metered subscription.")
    parser.add_argument("--output-dir", default="data",
                        help="Directory for the output CSV (default: data/ to match project style).")
    parser.add_argument("--sample-date", default="2024-07-01",
                        help="A recent date to use for the discovery query (doesn't affect results).")
    args = parser.parse_args()

    api_key = get_api_key()

    # Adjust defaults for free vs paid (more stations on paid is safer/better)
    max_st = args.max_stations
    if args.plan == "paid" and args.max_stations == 50:  # user didn't override
        max_st = 100

    print(f"Looking for stations within {args.radius_meters}m (~{args.radius_meters/1000:.1f} km) of {args.lat:.6f}, {args.lon:.6f}")
    print(f"Using plan={args.plan} (max_stations={max_st})")

    stations = list_stations_near_point(
        api_key=api_key,
        lat=args.lat,
        lon=args.lon,
        radius_meters=args.radius_meters,
        max_stations=max_st,
        sample_date=args.sample_date,
        output_dir=Path(args.output_dir),
    )

    if stations:
        print(f"\nDone. {len(stations)} stations within range.")
        print("You can now use a similar box + --max-station-distance-from-box in download_weather_box.py if desired.")
    else:
        print("\nNo stations found within radius. Try a larger --radius-meters or different point.")


if __name__ == "__main__":
    main()
