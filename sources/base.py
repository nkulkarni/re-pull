"""Shared interface every source-specific scraper implements.

Keeping this contract common means adding a new data source is just
"write a class with a crawl() method" — the checkpointing, the output
schema, and the CLI wiring in run.py stay the same.
"""
from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


@dataclass
class Listing:
    """Common schema all sources normalize into, so outputs can be concatenated."""

    title: str = ""
    price: str = ""
    price_numeric: Optional[float] = None
    address: str = ""
    province: str = ""
    size: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    detail_url: str = ""
    source: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict:
        return asdict(self)


class Scraper(ABC):
    """Base class for a single data source. Subclasses implement crawl()."""

    name: str = "base"
    headers: dict = {}
    request_delay: float = 2.0

    def __init__(self, checkpoint_dir: str | Path = "data/checkpoints", max_pages: int = 40, use_cache: bool = True):
        """Initialize scraper with optional mid-pull checkpointing and smart page caching.

        use_cache: if True (default), raw page HTML for visited URLs is cached to disk
                   under data/cache/ (alongside checkpoints). Subsequent runs (even without
                   --fresh) will use cached content for details, making re-runs much faster
                   and more resilient to transient network/ToS issues.
        """
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.checkpoint_dir / f"{self.name}_checkpoint.jsonl"
        self.max_pages = max_pages
        self.use_cache = use_cache
        self.cache_dir = self.checkpoint_dir.parent / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update(self.headers)

        self.visited: set[str] = set()
        self.listings: list[Listing] = []
        self._load_checkpoint()

    def _load_checkpoint(self) -> None:
        """Resume from a prior run: anything already checkpointed is skipped."""
        if not self.checkpoint_path.exists():
            return
        with open(self.checkpoint_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                self.listings.append(Listing(**record))
                if record.get("detail_url"):
                    self.visited.add(record["detail_url"])
        if self.listings:
            logger.info(
                "Resumed %d listing(s) from checkpoint %s", len(self.listings), self.checkpoint_path
            )

    def record(self, listing: Listing) -> None:
        """Store a scraped listing and persist it immediately.

        Appending per-listing (rather than writing only at the end) is what
        makes this crash-safe: kill the process at any point and a rerun
        picks up from the checkpoint instead of re-scraping from scratch.
        """
        self.listings.append(listing)
        with open(self.checkpoint_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(listing.to_dict(), ensure_ascii=False) + "\n")

    def clear_checkpoint(self, clear_cache: bool = False) -> None:
        """Discard any saved progress and start the next crawl() from scratch.

        If clear_cache=True, also wipe the page HTML cache (useful with --fresh).
        """
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
        self.listings = []
        self.visited = set()
        if clear_cache:
            self.clear_cache()

    def clear_cache(self) -> None:
        """Clear all cached page HTML (smart cache)."""
        if self.cache_dir.exists():
            for f in self.cache_dir.glob("*.html"):
                try:
                    f.unlink()
                except Exception:
                    pass
            logger.info("Cleared page cache directory: %s", self.cache_dir)

    def _cache_key(self, url: str) -> str:
        """Stable short filename-safe key for a URL."""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    def _get_cached_content(self, url: str) -> Optional[str]:
        """Return cached raw HTML for URL if use_cache and present."""
        if not self.use_cache:
            return None
        key = self._cache_key(url)
        path = self.cache_dir / f"{key}.html"
        if path.exists():
            logger.debug("Cache HIT for %s", url)
            return path.read_text(encoding="utf-8")
        return None

    def _save_to_cache(self, url: str, content: str) -> None:
        """Persist raw HTML for this URL (smart caching for speed + resilience)."""
        if not self.use_cache:
            return
        key = self._cache_key(url)
        path = self.cache_dir / f"{key}.html"
        path.write_text(content, encoding="utf-8")
        logger.debug("Cached content for %s", url)

    @abstractmethod
    def crawl(self) -> list[Listing]:
        """Run the crawl and return all scraped listings."""
        raise NotImplementedError
