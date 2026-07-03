import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import json
import os
import argparse
from urllib.parse import urljoin
from datetime import datetime

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; FarmOntarioInternalScraper/1.0; +https://farmontario.com/)'
}

BASE_URL = "https://farmontario.com/"
START_URLS = [
    "https://farmontario.com/our-listings/",
    "https://farmontario.com/search-farms/",
]

visited = set()
listings = []

# --- Mid-pull caching / checkpointing (added to this copy only) ---
CHECKPOINT_FILE = "farmontario_checkpoint.jsonl"

def load_checkpoint():
    """Resume from a prior run.
    Loads previously saved listings (as dicts) and rebuilds the visited set from Detail_URLs.
    This is what allows 'caching mid-pull' — progress survives Ctrl-C / crashes / network blips.
    """
    global visited, listings
    if not os.path.exists(CHECKPOINT_FILE):
        return
    restored = 0
    with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                listings.append(rec)
                if rec.get("Detail_URL"):
                    visited.add(rec["Detail_URL"])
                restored += 1
            except Exception:
                pass
    if restored:
        print(f"♻️  Resumed {restored} listing(s) from mid-pull checkpoint: {CHECKPOINT_FILE}")


def append_checkpoint(listing_dict):
    """Persist a single listing *immediately* to disk (append-only JSONL).
    This is the core of mid-pull caching: called right after we successfully parse a detail page,
    before we continue to the next URL.
    """
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(listing_dict, ensure_ascii=False) + "\n")


def clear_checkpoint():
    """Discard any saved mid-pull progress (equivalent to V2's --fresh)."""
    if os.path.exists(CHECKPOINT_FILE):
        os.unlink(CHECKPOINT_FILE)
    global visited, listings
    visited = set()
    listings = []
# --- end checkpoint helpers ---


def clean_text(text):
    return re.sub(r'\s+', ' ', str(text).strip()) if text else ""

def scrape_detail_page(url):
    """Scrape individual listing with JSON location data"""
    if url in visited:
        return
    visited.add(url)
    
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        
        # Try to bypass ToS
        session.get(url, timeout=15)
        session.post(url, data={'accept': 'I Accept The Terms'}, timeout=15)
        
        r = session.get(url, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Find JSON with latitude/longitude
        lat = lng = None
        json_match = re.search(r'(\{.*?"latitude"\s*:\s*[\d.-]+.*?"longitude"\s*:\s*[\d.-]+.*?\})', str(soup), re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1) + "}")
                lat = data.get("latitude")
                lng = data.get("longitude")
                print(f"📍 {lat}, {lng} → {url}")
            except:
                pass
        
        # Basic fields
        title = clean_text(soup.find('h1'))
        price = clean_text(soup.find(string=re.compile(r'\$\s*[\d,]+')))
        
        listing_dict = {
            'Title': title,
            'Price': price,
            'Latitude': lat,
            'Longitude': lng,
            'Detail_URL': url,
            'Scraped_At': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        listings.append(listing_dict)
        append_checkpoint(listing_dict)   # <--- MID-PULL CACHING: written to disk right now
        time.sleep(1.5)
    except Exception as e:
        print(f"Detail error {url}: {e}")

def extract_listings_from_page(soup, page_url):
    """Extract links from index pages"""
    links = soup.find_all('a', href=re.compile(r'/listing/'))
    for link in links:
        detail_url = urljoin(BASE_URL, link.get('href', ''))
        if detail_url and detail_url not in visited:
            scrape_detail_page(detail_url)

def crawl(start_urls, max_pages=30):
    to_visit = list(start_urls)
    processed = 0
    while to_visit and processed < max_pages:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        processed += 1
        print(f"🌐 Crawling {processed}/{max_pages}: {url}")
        
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, 'html.parser')
            extract_listings_from_page(soup, url)
            
            # Discover more pages
            for link in soup.find_all('a', href=True):
                next_url = urljoin(BASE_URL, link['href'])
                if any(x in next_url for x in ['/listing/', '/our-listings', '/search-farms']) and next_url not in visited:
                    to_visit.append(next_url)
            time.sleep(2)
        except Exception as e:
            print(f"Error crawling {url}: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Farm Ontario Scraper (V1 style with mid-pull checkpoint caching)"
    )
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages to crawl")
    parser.add_argument(
        "--fresh", action="store_true",
        help="Ignore any existing checkpoint and start from scratch (like V2 --fresh)"
    )
    args = parser.parse_args()

    if args.fresh:
        clear_checkpoint()
        print("🧹 --fresh: checkpoint cleared. Starting over.")

    load_checkpoint()

    print("🚀 Farm Ontario Scraper Starting (mid-pull caching ENABLED)...")
    crawl(START_URLS, max_pages=args.max_pages)
    
    df = pd.DataFrame(listings)
    if not df.empty:
        df = df.drop_duplicates(subset=['Detail_URL'])
        filename = f"farm_ontario_listings_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(filename, index=False, encoding='utf-8')
        print(f"\n✅ DONE! Saved {len(df)} listings to {filename}")
        print("\nPreview:")
        print(df[['Title', 'Price', 'Latitude', 'Longitude']].head(10))

        # Clean up checkpoint on successful completion
        if os.path.exists(CHECKPOINT_FILE):
            os.unlink(CHECKPOINT_FILE)
            print("🧹 Checkpoint file removed after successful run.")
    else:
        print("No listings found.")

