import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import json
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
        
        listings.append({
            'Title': title,
            'Price': price,
            'Latitude': lat,
            'Longitude': lng,
            'Detail_URL': url,
            'Scraped_At': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
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
    print("🚀 Farm Ontario Scraper Starting...")
    crawl(START_URLS, max_pages=40)  # Adjust as needed
    
    df = pd.DataFrame(listings)
    if not df.empty:
        df = df.drop_duplicates(subset=['Detail_URL'])
        filename = f"farm_ontario_listings_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(filename, index=False, encoding='utf-8')
        print(f"\n✅ DONE! Saved {len(df)} listings to {filename}")
        print("\nPreview:")
        print(df[['Title', 'Price', 'Latitude', 'Longitude']].head(10))
    else:
        print("No listings found.")
