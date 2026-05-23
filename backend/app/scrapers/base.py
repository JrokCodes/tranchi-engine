"""
Abstract base classes for all deal-sourcing scrapers.

ListingScraper — for sources that produce tranchi.listings rows
    (sheriff sales, land bank, probate). Every implementation must declare
    site_name and implement fetch_and_parse().

SignalScraper — for sources that tag parcels rather than create listings
    (code violations, fiscal officer distress flags). Every implementation
    must declare site_name and implement fetch_signals(). Signal rows land
    in tranchi.signals; they are NOT listings.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.scrapers.models import RawListing, RawSignal


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


class SignalScraper(ABC):
    """Base class for scrapers that write to tranchi.signals instead of tranchi.listings.

    Code violations, fiscal officer distress flags, and similar per-parcel signals
    inherit from this class. The orchestrator (run.py) calls fetch_signals() and
    routes output to upsert_signals() rather than upsert_listings().
    """
    site_name: str

    @abstractmethod
    async def fetch_signals(self) -> list[RawSignal]:
        """Fetch data from the source and return parsed signal rows.

        Each implementation is responsible for:
        - Making HTTP/REST requests with retry logic
        - Parsing response data into RawSignal objects
        - Returning a list of RawSignal objects (no prefilter for signals)
        """
        ...
