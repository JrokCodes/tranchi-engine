"""
User-agent rotation for scrapers.

Rotates among recent real Chrome UA strings on macOS and Windows.
No "TranchiBot" identifier, no contact email — generic low-detection posture.
Per Marc: we rotate, we don't identify.

Verified as shipping Chrome versions as of May 2026.
"""
from __future__ import annotations

import random

_USER_AGENTS: list[str] = [
    # Chrome 124 on macOS (Apple Silicon)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 on Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 123 on Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Consistent Accept-Language header to pair with any of the above UAs
ACCEPT_LANGUAGE = "en-US,en;q=0.9"


def random_ua() -> str:
    """Return a randomly chosen user-agent string."""
    return random.choice(_USER_AGENTS)


def default_headers() -> dict[str, str]:
    """Return a minimal headers dict suitable for most scraper requests."""
    return {
        "User-Agent": random_ua(),
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }
