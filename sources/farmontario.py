import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .base import Listing, Scraper

logger = logging.getLogger(__name__)

# Playwright is optional at import time but required at runtime for the updated scraper.
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None  # Will raise a clear error when used.


class FarmOntarioScraper(Scraper):
    name = "farmontario"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FarmOntarioInternalScraper/1.0; +https://farmontario.com/)"
    }
    request_delay = 2.0

    BASE_URL = "https://farmontario.com/"
    START_URLS = [
        "https://farmontario.com/our-listings/",
        "https://farmontario.com/search-farms/",
    ]
    LISTING_PATH_HINTS = ("/listing/", "/our-listings", "/search-farms")
    # Improved regex (from test.py technique) + fallback
    LATLNG_RE = re.compile(r'(\{.*?"latitude"\s*:\s*[\d.-]+.*?"longitude"\s*:\s*[\d.-]+.*?\})', re.DOTALL)
    PRICE_RE = re.compile(r"\$\s*[\d,]+")

    def __init__(self, checkpoint_dir: str | Path = "data/checkpoints", max_pages: int = 40, use_cache: bool = True):
        # Pass through new smart-caching param + old ones to base
        super().__init__(checkpoint_dir=checkpoint_dir, max_pages=max_pages, use_cache=use_cache)

        # Playwright browser management (reused across detail pages for efficiency)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    @staticmethod
    def clean_text(text) -> str:
        return re.sub(r"\s+", " ", str(text).strip()) if text else ""

    def _extract_coords(self, content: str) -> tuple[Optional[float], Optional[float]]:
        """Improved lat/lng extraction (technique from test.py + fallback).

        Accepts raw HTML content string (from Playwright .content() or requests.text).
        Tries a stricter pattern first, then cleans common JSON truncation, falls back to old regex.
        """
        # Primary improved pattern (stricter, less greedy)
        match = re.search(
            r'\{[^}]*"latitude"\s*:\s*[\d\.-]+[^}]*"longitude"\s*:\s*[\d\.-]+[^}]*\}',
            content, re.DOTALL
        )
        if not match:
            # Fallback to original broader pattern
            match = self.LATLNG_RE.search(content)
        if not match:
            return None, None
        try:
            data_str = match.group(0)
            # Clean common truncation / trailing comma issues
            data_str = re.sub(r',\s*}', '}', data_str)
            data_str = re.sub(r',\s*\]', ']', data_str)
            data = json.loads(data_str)
            return data.get("latitude"), data.get("longitude")
        except Exception as e:
            logger.debug("Could not parse embedded location JSON: %s", e)
            return None, None

    def _ensure_playwright(self) -> None:
        """Launch a single browser/context/page and reuse it for all detail scrapes.

        Much more efficient than launching per URL (Playwright overhead is high).
        """
        if self.page is not None:
            return
        if sync_playwright is None:
            raise RuntimeError(
                "playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.context = self.browser.new_context(
            user_agent=self.headers.get(
                "User-Agent",
                "Mozilla/5.0 (compatible; FarmOntarioInternalScraper/1.0; +https://farmontario.com/)"
            )
        )
        self.page = self.context.new_page()
        logger.info("Playwright browser launched (reused for detail pages)")

    def _close_playwright(self) -> None:
        """Cleanly shut down browser (call in finally block of crawl)."""
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
            pass  # best effort cleanup
        self.page = self.context = self.browser = self.playwright = None

    def scrape_detail_page(self, url: str) -> Optional[Listing]:
        if url in self.visited:
            return None
        self.visited.add(url)

        # Smart cache first (uses base.py helpers)
        content = self._get_cached_content(url)
        fetched_from_network = False

        if content is None:
            # Use Playwright technique (from test.py) for reliable ToS handling + JS content
            self._ensure_playwright()
            try:
                self.page.goto(url, timeout=30000)

                try:
                    self.page.click("text=I Accept The Terms", timeout=10000)
                    logger.info("✅ Clicked 'I Accept The Terms'")
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    logger.debug("No ToS button or already accepted for %s", url)

                self.page.wait_for_timeout(3000)  # give maps/JS a moment (tunable)

                content = self.page.content()
                self._save_to_cache(url, content)
                fetched_from_network = True
            except Exception as e:
                logger.warning("Detail page error (Playwright) %s: %s", url, e)
                return None

        # Parse with BS4 + improved extraction (works for both cached and fresh content)
        soup = BeautifulSoup(content, "html.parser")

        lat, lng = self._extract_coords(content)  # pass raw content for best regex match
        price_text = self.clean_text(soup.find(string=self.PRICE_RE))

        # Try to derive price_numeric (enhancement over basic V2)
        price_numeric = None
        if price_text:
            m = re.search(r"[\d,]+", price_text)
            if m:
                try:
                    price_numeric = float(m.group().replace(",", ""))
                except ValueError:
                    pass

        listing = Listing(
            title=self.clean_text(soup.find("h1")),
            price=price_text,
            price_numeric=price_numeric,
            latitude=lat,
            longitude=lng,
            detail_url=url,
            source=self.name,
        )
        self.record(listing)
        logger.info("Scraped %s (%s)", url, listing.price or "no price")

        if fetched_from_network:
            time.sleep(self.request_delay)

        return listing

    def _extract_listing_links(self, soup: BeautifulSoup) -> None:
        for link in soup.find_all("a", href=re.compile(r"/listing/")):
            detail_url = urljoin(self.BASE_URL, link.get("href", ""))
            if detail_url and detail_url not in self.visited:
                self.scrape_detail_page(detail_url)

    def crawl(self) -> list[Listing]:
        to_visit = list(self.START_URLS)
        processed = 0

        try:
            while to_visit and processed < self.max_pages:
                url = to_visit.pop(0)
                if url in self.visited:
                    continue
                self.visited.add(url)
                processed += 1
                logger.info("Crawling %d/%d: %s", processed, self.max_pages, url)

                try:
                    r = self.session.get(url, timeout=15)
                    if r.status_code != 200:
                        continue
                    soup = BeautifulSoup(r.text, "html.parser")
                    self._extract_listing_links(soup)

                    for link in soup.find_all("a", href=True):
                        next_url = urljoin(self.BASE_URL, link["href"])
                        if any(hint in next_url for hint in self.LISTING_PATH_HINTS) and next_url not in self.visited:
                            to_visit.append(next_url)
                    time.sleep(self.request_delay)
                except requests.RequestException as e:
                    logger.warning("Error crawling %s: %s", url, e)

            return self.listings
        finally:
            # Always clean up the browser even on errors / KeyboardInterrupt
            self._close_playwright()
