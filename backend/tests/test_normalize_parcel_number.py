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


# ───────────────────────────────────────────────────────────────────────────
# Summit County (OH / Akron) — 7-digit zero-padded numeric canonical form.
# These prove the new branch works AND that it does not collide with the
# Cuyahoga (8-digit) or Shelby (>= 10-digit / alpha / spaced) detection above.
# Format-lock anchors traced live across the GIS spine + RealAuction + Akron
# Legal News + delinquent-tax tape + land bank (2026-06-13).
# ───────────────────────────────────────────────────────────────────────────

def test_summit_seven_digit_idempotent() -> None:
    """A 7-digit Summit parcel is returned unchanged (GIS spine / land bank / DELQ)."""
    assert normalize_parcel_number("7000697") == "7000697"
    assert normalize_parcel_number("6700526") == "6700526"
    assert normalize_parcel_number("2400024") == "2400024"


def test_summit_leading_zero_preserved() -> None:
    """Leading zeros are load-bearing — never int-cast away (DELQ tape parcel)."""
    assert normalize_parcel_number("0101379") == "0101379"
    assert normalize_parcel_number("0211528") == "0211528"


def test_summit_dash_display_form() -> None:
    """Fiscal-office display dash ('67-08383') and Akron Legal News ('70-00697') strip to 7 digits."""
    assert normalize_parcel_number("67-08383") == "6708383"
    assert normalize_parcel_number("70-00697") == "7000697"
    assert normalize_parcel_number("02-11528") == "0211528"


def test_summit_dropped_leading_zero_zfilled() -> None:
    """Defensive: a source dropping a leading zero ('101379') zero-pads back to 7."""
    assert normalize_parcel_number("101379") == "0101379"


def test_summit_does_not_break_cuyahoga_or_shelby() -> None:
    """The Summit branch must not capture Cuyahoga (8-digit) or Shelby (>=10 / alpha / spaced)."""
    # Cuyahoga stays Cuyahoga
    assert normalize_parcel_number("11019068") == "110-19-068"
    assert normalize_parcel_number("110-19-068") == "110-19-068"
    # Shelby stays Shelby
    assert normalize_parcel_number("07204700000160") == "07204700000160"
    assert normalize_parcel_number("072047  00016") == "07204700000160"
    assert normalize_parcel_number("D0217   00225") == "D0217000002250"
    assert normalize_parcel_number("07204700016") == "07204700000160"
