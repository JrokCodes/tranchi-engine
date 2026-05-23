"""
Abstract base class for all deal-sourcing scrapers.
Every scraper must implement fetch_and_parse() and declare site_name.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.scrapers.models import RawListing


class ListingScraper(ABC):
    site_name: str

    @abstractmethod
    async def fetch_and_parse(self) -> list[RawListing]:
        """Fetch HTML/JSON from the source and return parsed listings.

        Each implementation is responsible for:
        - Making HTTP requests with retry logic
        - Parsing HTML/JSON into RawListing objects
        - Returning a list of RawListing objects (NOT yet prefiltered)
        """
        ...
