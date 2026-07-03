"""Farms.com scraper for Ontario farm real estate.

Follows the V2 contract in base.py:
- Subclasses Scraper
- Implements crawl() that calls record() for each listing (mid-pull checkpoint + smart cache)
- Uses Playwright (via base helpers) for list pages (JS pagination) and detail pages (reliable extraction)
- Leverages inherited _get_cached_content / _save_to_cache for smart HTML caching of details
- Respects max_pages (as number of list pages), visited from checkpoint, etc.
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .base import Listing, Scraper

logger = logging.getLogger(__name__)


class FarmsComScraper(Scraper):
    name = "farmscom"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FarmsComInternalScraper/1.0; +https://www.farms.com/)"
    }
    request_delay = 2.0

    BASE_URL = "https://www.farms.com/farm-real-estate/farms-for-sale/ontario/"
    PRICE_RE = re.compile(r'\$\s*([\d,]+)')

    def __init__(self, checkpoint_dir: str | Path = "data/checkpoints", max_pages: int = 40, use_cache: bool = True):
        super().__init__(checkpoint_dir=checkpoint_dir, max_pages=max_pages, use_cache=use_cache)

        # Playwright management (used for both list pagination (JS-loaded) and details)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    @staticmethod
    def clean_text(text) -> str:
        """Strip HTML if we were passed a BS4 Tag/ResultSet and normalize whitespace."""
        if text is None:
            return ""
        if hasattr(text, "get_text"):
            text = text.get_text()
        return re.sub(r"\s+", " ", str(text).strip())

    def _ensure_playwright(self) -> None:
        """Launch browser once and reuse for list + detail pages."""
        if self.page is not None:
            return
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(
            user_agent=self.headers.get(
                "User-Agent",
                "Mozilla/5.0 (compatible; FarmsComInternalScraper/1.0; +https://www.farms.com/)"
            )
        )
        self.page = self.context.new_page()
        logger.info("Playwright browser launched for farms.com (reused for list + details)")

    def _close_playwright(self) -> None:
        """Best-effort cleanup."""
        try:
            if self.page:
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.page = self.context = self.browser = self.playwright = None

    def scrape_detail_page(self, url: str) -> Optional[Listing]:
        if url in self.visited:
            return None
        self.visited.add(url)

        content = self._get_cached_content(url)
        fetched_from_network = False

        if content is None:
            self._ensure_playwright()
            try:
                self.page.goto(url, timeout=45000, wait_until="domcontentloaded")
                self.page.wait_for_timeout(3500)  # let heavy JS/maps/ads settle (more tolerant than strict networkidle)
                content = self.page.content()
                self._save_to_cache(url, content)
                fetched_from_network = True
            except Exception as e:
                logger.warning("Detail error (Playwright) %s: %s", url, e)
                return None

        soup = BeautifulSoup(content, "html.parser")

        title = self.clean_text(soup.find("h1"))

        # Price
        price = ""
        price_numeric = None
        price_match = soup.find(string=self.PRICE_RE)
        if price_match:
            m = self.PRICE_RE.search(str(price_match))
            if m:
                num = m.group(1)
                price = "$" + num
                try:
                    price_numeric = float(num.replace(",", ""))
                except ValueError:
                    pass

        # Size / acres - strongly prefer "Total Acres:" label + number (most reliable)
        size = ""
        for label in soup.find_all(string=re.compile(r"Total Acres", re.I)):
            # Check the label itself, its parent, or next siblings for the number
            for candidate in [label, getattr(label, "parent", None)] + list(getattr(label, "parent", type('obj', (object,), {})).__dict__.get('next_siblings', []))[:3]:
                if candidate is None: continue
                txt = self.clean_text(str(candidate))
                m = re.search(r"Total Acres[:\s]*(\d[\d,]*)", txt, re.I)
                if m:
                    size = m.group(1) + " acres"
                    break
            if size: break
        if not size:
            # Fallback: first "XXX acres" that looks like a factual size (not teaser titles)
            for txt in soup.find_all(string=re.compile(r"\b(\d[\d,]*)\s*acres\b", re.I)):
                s = self.clean_text(str(txt))
                if re.search(r"^\d+ acres$", s, re.I) or (len(s) > 10 and "for sale" not in s.lower()):
                    m = re.search(r"(\d[\d,]*)\s*acres", s, re.I)
                    if m:
                        size = m.group(1) + " acres"
                        break

        # Address / province (title usually contains location, province is Ontario)
        address = title
        province = "Ontario"

        # Lat / Lng (not always present; some pages use static maps)
        lat = lng = None
        for script in soup.find_all("script"):
            s = str(script)
            latm = re.search(r'latitude["\']?\s*[:=]\s*["\']?([\d\.-]+)', s, re.I)
            lngm = re.search(r'longitude["\']?\s*[:=]\s*["\']?([\d\.-]+)', s, re.I)
            if latm and lngm:
                try:
                    lat = float(latm.group(1))
                    lng = float(lngm.group(1))
                    break
                except ValueError:
                    pass

        listing = Listing(
            title=title,
            price=price,
            price_numeric=price_numeric,
            address=address,
            province=province,
            size=size,
            latitude=lat,
            longitude=lng,
            detail_url=url,
            source=self.name,
        )
        listing.compute_derived_fields()  # sets acres + cost_per_acre
        self.record(listing)
        logger.info("Scraped %s (%s)", url, price or "no price")

        if fetched_from_network:
            time.sleep(self.request_delay)
        return listing

    def crawl(self) -> list[Listing]:
        """Visit up to max_pages list pages (using Playwright for JS pagination).
        Extract and scrape details as they are discovered.
        """
        try:
            self._ensure_playwright()

            for page_num in range(1, self.max_pages + 1):
                if page_num == 1:
                    list_url = self.BASE_URL
                else:
                    list_url = self.BASE_URL.rstrip("/") + f"/{page_num}"

                logger.info("Crawling list page %d/%d: %s", page_num, self.max_pages, list_url)

                try:
                    self.page.goto(list_url, timeout=45000, wait_until="domcontentloaded")
                    self.page.wait_for_timeout(2500)

                    content = self.page.content()
                    soup = BeautifulSoup(content, "html.parser")

                    # Collect unique detail links on this list page
                    detail_urls = []
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if "/farm-real-estate/farms-for-sale/ontario/" in href and href.endswith(".aspx"):
                            full = urljoin("https://www.farms.com", href)
                            if full not in detail_urls:
                                detail_urls.append(full)

                    logger.info("Found %d listing links on this page", len(detail_urls))

                    for durl in detail_urls:
                        if durl not in self.visited:
                            self.scrape_detail_page(durl)

                except Exception as e:
                    logger.warning("Error crawling list page %s: %s", list_url, e)

                time.sleep(self.request_delay)

            return self.listings
        finally:
            self._close_playwright()
