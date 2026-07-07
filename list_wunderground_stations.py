#!/usr/bin/env python3
"""
Script to list Weather Underground (Wunderground) Personal Weather Stations (PWS)
near a given lat/long within a radius.

Uses the (legacy but still functional for many keys) geolookup endpoint to find nearby PWS.

This is great for discovering hyperlocal stations for your farm data project.

It will:
- Query WU for stations near the point.
- Filter to those within the requested radius (in km).
- Print a nice table.
- Save a CSV: stations_near_<lat>_<lon>_r<radius>km.csv

Requirements:
- requests (in your requirements.txt)
- A Wunderground API key (set as env var WUNDERGROUND_API_KEY or pass --key)
  Note: Classic WU API keys for PWS contributors still work for geolookup and history.
  If yours is through the new Weather Company APIs, you may need to adjust the base URL.

Usage:
    export WUNDERGROUND_API_KEY=your_key
    python list_wunderground_stations.py --lat 44.0847 --lon -76.9594 --radius-km 2

Defaults to your example point with 2km radius.

For historical data from a specific station ID (after you pick one):
    The history endpoints are like:
    https://api.wunderground.com/api/YOUR_KEY/history_YYYYMMDD/q/pws:STATIONID.json
    or for PWS v2: https://api.weather.com/v2/pws/history/all?stationId=...&format=json&date=YYYYMMDD

This pairs well with download_weather_box.py for enriching your master_listings with WU PWS data.
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

# Classic WU endpoint (still works for many PWS keys)
WU_BASE = "https://api.wunderground.com/api"


def get_api_key(provided_key=None):
    if provided_key:
        return provided_key
    key = os.getenv("WUNDERGROUND_API_KEY") or os.getenv("WU_API_KEY")
    if not key:
        raise ValueError(
            "No API key found. Set WUNDERGROUND_API_KEY env var or use --key YOUR_KEY. "
            "Get/register at https://www.wunderground.com/ (PWS contributors get access)."
        )
    return key


def find_stations_near(lat, lon, radius_km=2.0, api_key=None):
    """
    Use WU geolookup to find nearby PWS stations.
    Returns list of dicts with id, name, lat, lon, distance_km, city, state, etc.
    Filters to those within radius_km.
    """
    api_key = get_api_key(api_key)
    url = f"{WU_BASE}/{api_key}/geolookup/q/{lat},{lon}.json"
    logger.info(f"Querying WU geolookup near {lat:.4f},{lon:.4f}...")

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"API call failed: {e}")
        if "Invalid API key" in str(e) or resp.status_code == 401:
            logger.error("Your WU key may be invalid, expired, or require the new Weather Company endpoints.")
            logger.error("Check https://www.wunderground.com/ or https://www.weathercompany.com/weather-data-apis/")
        return []

    nearby = data.get("location", {}).get("nearby_weather_stations", {}).get("pws", {}).get("station", [])
    if not nearby:
        logger.warning("No PWS stations returned by geolookup. The key may be limited or the area has few stations.")
        # Also check airport stations as fallback
        airports = data.get("location", {}).get("nearby_weather_stations", {}).get("airport", {}).get("station", [])
        if airports:
            logger.info(f"Found {len(airports)} nearby airport stations (official, not PWS).")
        return []

    stations = []
    for s in nearby:
        try:
            sid = s.get("id", "")
            name = s.get("neighborhood", s.get("city", "")) or "Unknown"
            slat = float(s.get("lat", 0))
            slon = float(s.get("lon", 0))
            dist_km = float(s.get("distance_km", 0))
            if dist_km <= radius_km:
                stations.append({
                    "station_id": sid,
                    "name": name,
                    "latitude": slat,
                    "longitude": slon,
                    "distance_km": round(dist_km, 3),
                    "city": s.get("city", ""),
                    "state": s.get("state", ""),
                    "country": s.get("country", ""),
                })
        except (ValueError, TypeError):
            continue

    stations.sort(key=lambda x: x["distance_km"])
    logger.info(f"Found {len(stations)} PWS stations within {radius_km} km.")
    return stations


def main():
    parser = argparse.ArgumentParser(
        description="List Wunderground PWS stations near a lat/long within a radius (in km)."
    )
    parser.add_argument("--lat", type=float, default=44.0847, help="Latitude (default: your example point)")
    parser.add_argument("--lon", type=float, default=-76.9594, help="Longitude (default: your example point)")
    parser.add_argument("--radius-km", type=float, default=2.0, help="Radius in kilometers (default 2 km)")
    parser.add_argument("--key", help="Wunderground API key (overrides env var)")
    parser.add_argument("--output-dir", default="data", help="Where to save the CSV (default: data/)")
    parser.add_argument("--max-stations", type=int, default=20, help="Max stations to list (default 20)")
    args = parser.parse_args()

    api_key = get_api_key(args.key)

    stations = find_stations_near(args.lat, args.lon, args.radius_km, api_key)

    if not stations:
        print("\nNo PWS stations found within radius.")
        print("Tips:")
        print("  - Try a larger --radius-km (e.g. 5 or 10 for rural areas)")
        print("  - Check the WU map: https://www.wunderground.com/wundermap")
        print("  - Your key may be PWS-contributor only or need the new api.weather.com endpoints.")
        print("  - Official airports are often listed separately in geolookup response.")
        return

    # Limit for display
    stations = stations[:args.max_stations]

    print(f"\nStations within {args.radius_km} km of {args.lat:.4f}, {args.lon:.4f}:")
    print(f"{'ID':<20} {'Name':<35} {'Dist km':>8} {'Lat':>10} {'Lon':>10}")
    print("-" * 90)
    for s in stations:
        print(f"{s['station_id']:<20} {s['name'][:34]:<35} {s['distance_km']:>8.3f} {s['latitude']:>10.4f} {s['longitude']:>10.4f}")

    # Save CSV
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_lat = f"{args.lat:.4f}".replace(".", "_").replace("-", "m")
    safe_lon = f"{args.lon:.4f}".replace(".", "_").replace("-", "m")
    csv_path = out_dir / f"stations_near_{safe_lat}_{safe_lon}_r{args.radius_km}km.csv"

    import pandas as pd
    df = pd.DataFrame(stations)
    df.to_csv(csv_path, index=False)
    print(f"\nSaved {len(stations)} stations to: {csv_path}")
    print(f"\nNext steps: Pick a station_id (e.g. the closest) and fetch its historical data with another call or the download script.")

    # Bonus: show example curl / python for one station's history (user can adapt)
    if stations:
        example_id = stations[0]["station_id"]
        print(f"\nExample: To fetch historical for the closest station ({example_id}) on a date:")
        print(f"  curl 'https://api.wunderground.com/api/{api_key}/history_20240701/q/pws:{example_id}.json' | jq")
        print("Or use the PWS v2 endpoint if your key requires it:")
        print(f"  https://api.weather.com/v2/pws/history/all?stationId={example_id}&format=json&units=m&date=20240701&apiKey={api_key}")


if __name__ == "__main__":
    main()
