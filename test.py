from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import pandas as pd
import time
import re
import json
from datetime import datetime

listings = []

def scrape_detail_page(url):
    print(f"\n🔍 Testing: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (compatible; FarmOntarioInternalScraper/1.0)"})
        
        try:
            page.goto(url, timeout=30000)
            
            try:
                page.click("text=I Accept The Terms", timeout=10000)
                print("✅ Clicked Accept Terms")
                page.wait_for_load_state("networkidle", timeout=10000)
            except:
                print("No ToS button needed")
            
            page.wait_for_timeout(4000)
            
            content = page.content()
            
            # Improved JSON extraction - look for the specific object
            json_match = re.search(r'\{[^}]*"latitude"\s*:\s*[\d\.-]+[^}]*"longitude"\s*:\s*[\d\.-]+[^}]*\}', 
                                   content, re.DOTALL)
            
            lat = lng = None
            if json_match:
                try:
                    data_str = json_match.group(0)
                    # Clean common issues
                    data_str = re.sub(r',\s*}', '}', data_str)
                    data = json.loads(data_str)
                    lat = data.get("latitude")
                    lng = data.get("longitude")
                    print(f"📍 Latitude: {lat}, Longitude: {lng}")
                except Exception as e:
                    print(f"JSON parse error: {e}")
            
            title = BeautifulSoup(content, 'html.parser').find('h1')
            title_text = title.get_text(strip=True) if title else ""
            price_match = re.search(r'\$\s*[\d,]+', content)
            price_text = price_match.group(0) if price_match else ""
            
            listings.append({
                'Title': title_text,
                'Price': price_text,
                'Latitude': lat,
                'Longitude': lng,
                'Detail_URL': url,
                'Scraped_At': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            print("✅ Successfully scraped!")
            
        except Exception as e:
            print(f"Error: {e}")
        finally:
            browser.close()

if __name__ == "__main__":
    print("🚀 Fixed Playwright FarmOntario Scraper\n")
    
    test_urls = [
        "https://farmontario.com/listing/pt-lt-9-range-1-road-brant-brantford-twp-ontario-n3t-5l4-29371700/",
        "https://farmontario.com/listing/515490-2nd-line-amaranth-ontario-l9v-1l6-29983276/"
    ]
    
    for url in test_urls:
        scrape_detail_page(url)
        time.sleep(3)
    
    df = pd.DataFrame(listings)
    if not df.empty:
        filename = f"test_farm_listings_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        df.to_csv(filename, index=False, encoding='utf-8')
        print(f"\n✅ Saved to {filename}")
        print(df)
    else:
        print("No data extracted.")
