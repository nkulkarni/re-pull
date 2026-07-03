import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .base import Listing, Scraper

logger = logging.getLogger(__name__)


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
    LATLNG_RE = re.compile(r'(\{.*?"latitude"\s*:\s*[\d.-]+.*?"longitude"\s*:\s*[\d.-]+.*?\})', re.DOTALL)
    PRICE_RE = re.compile(r"\$\s*[\d,]+")

    @staticmethod
    def clean_text(text) -> str:
        return re.sub(r"\s+", " ", str(text).strip()) if text else ""

    def _extract_coords(self, soup: BeautifulSoup) -> tuple[Optional[float], Optional[float]]:
        """Pull lat/lng out of the JSON blob the map widget is rendered from."""
        match = self.LATLNG_RE.search(str(soup))
        if not match:
            return None, None
        try:
            data = json.loads(match.group(1) + "}")
            return data.get("latitude"), data.get("longitude")
        except json.JSONDecodeError:
            logger.debug("Could not parse embedded location JSON")
            return None, None

    def scrape_detail_page(self, url: str) -> Optional[Listing]:
        if url in self.visited:
            return None
        self.visited.add(url)

        try:
            r = self.session.get(url, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")

            lat, lng = self._extract_coords(soup)
            listing = Listing(
                title=self.clean_text(soup.find("h1")),
                price=self.clean_text(soup.find(string=self.PRICE_RE)),
                latitude=lat,
                longitude=lng,
                detail_url=url,
                source=self.name,
            )
            self.record(listing)
            logger.info("Scraped %s (%s)", url, listing.price or "no price")
            time.sleep(self.request_delay)
            return listing
        except requests.RequestException as e:
            logger.warning("Detail error %s: %s", url, e)
            return None

    def _extract_listing_links(self, soup: BeautifulSoup) -> None:
        for link in soup.find_all("a", href=re.compile(r"/listing/")):
            detail_url = urljoin(self.BASE_URL, link.get("href", ""))
            if detail_url and detail_url not in self.visited:
                self.scrape_detail_page(detail_url)

    def crawl(self) -> list[Listing]:
        to_visit = list(self.START_URLS)
        processed = 0

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
