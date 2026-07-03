"""Shared interface every source-specific scraper implements.

Keeping this contract common means adding a new data source is just
"write a class with a crawl() method" — the checkpointing, the output
schema, and the CLI wiring in run.py stay the same.
"""
from __future__ import annotations

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

    def __init__(self, checkpoint_dir: str | Path = "data/checkpoints", max_pages: int = 40):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.checkpoint_dir / f"{self.name}_checkpoint.jsonl"
        self.max_pages = max_pages

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

    def clear_checkpoint(self) -> None:
        """Discard any saved progress and start the next crawl() from scratch."""
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()
        self.listings = []
        self.visited = set()

    @abstractmethod
    def crawl(self) -> list[Listing]:
        """Run the crawl and return all scraped listings."""
        raise NotImplementedError
