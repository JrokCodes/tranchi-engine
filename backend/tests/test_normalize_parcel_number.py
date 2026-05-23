"""
Unit tests for normalize_parcel_number in db.py.

Cuyahoga County uses two parcel formats:
  - Compact 8-digit: '11019068' (Cleveland Open Data / code violations)
  - Display DDD-NN-NNN: '110-19-068' (MyPlace / Sheriff / Land Bank)

normalize_parcel_number must convert compact → display and be idempotent
on display format. These are the cross-source join correctness tests.
"""
import sys
from pathlib import Path

# Allow running from backend/ without installing the package
_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from app.scrapers.db import normalize_parcel_number


def test_compact_to_display() -> None:
    """8-digit compact format normalizes to DDD-NN-NNN display format."""
    assert normalize_parcel_number("11019068") == "110-19-068"


def test_display_is_idempotent() -> None:
    """Already-hyphenated DDD-NN-NNN is returned unchanged."""
    assert normalize_parcel_number("110-19-068") == "110-19-068"


def test_none_returns_none() -> None:
    """None input returns None."""
    assert normalize_parcel_number(None) is None


def test_empty_string_returns_none() -> None:
    """Empty string returns None."""
    assert normalize_parcel_number("") is None


def test_whitespace_stripped_compact() -> None:
    """Leading/trailing whitespace is stripped before normalization."""
    assert normalize_parcel_number("  11019068  ") == "110-19-068"


def test_whitespace_stripped_display() -> None:
    """Leading/trailing whitespace stripped from display format."""
    assert normalize_parcel_number("  110-19-068  ") == "110-19-068"


def test_another_known_parcel() -> None:
    """Second known Cuyahoga parcel: compact '02213042' → '022-13-042'."""
    assert normalize_parcel_number("02213042") == "022-13-042"


def test_another_known_parcel_display_idempotent() -> None:
    """Display format '022-13-042' is already normalized."""
    assert normalize_parcel_number("022-13-042") == "022-13-042"


def test_unparseable_returns_cleaned() -> None:
    """Input that can't be parsed to 8 digits is returned cleaned (no crash)."""
    result = normalize_parcel_number("UNKNOWN-PARCEL")
    assert result == "UNKNOWN-PARCEL"
