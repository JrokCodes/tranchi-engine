"""
Unit tests for the Lucas (Toledo, OH) parcel normalizer and the market-aware
dispatch added for the parcel-identity fix (F-008 / must-close #6).

Two things are proven here:
  1. normalize_parcel_lucas correctly canonicalizes Lucas PARIDs (7-digit zero-padded;
     RealAuction/Legal-News 'DD-DDDDD' dash form; the 96 '…S' condo/split parcels).
  2. normalize_parcel_for_market(x, market) is BYTE-FOR-BYTE identical to the global
     normalize_parcel_number for the 4 pre-Lucas markets — the zero-behavior-change
     guarantee that makes migration 021 safe to ship.

Plus the ambiguity proof that motivates the whole design: a bare '1210314' normalizes
to the SAME string under both Lucas and Summit, so only the `market` column can
disambiguate them — which is why parcels identity must be (parcel_number, market).
"""
import sys
from pathlib import Path

# Allow running from backend/ without installing the package
_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from app.market_config import MARKETS
from app.scrapers.db import (
    normalize_parcel_for_market,
    normalize_parcel_lucas,
    normalize_parcel_number,
)

LIVE_MARKETS = ("cuyahoga", "shelby", "summit", "wayne")


# ── Lucas canonical PARID (7-digit zero-padded) ──────────────────────────────

def test_lucas_seven_digit_idempotent() -> None:
    """An Auditor AREIS PARID is already 7-digit and returned unchanged."""
    assert normalize_parcel_lucas("1210314") == "1210314"
    assert normalize_parcel_lucas("0100001") == "0100001"


def test_lucas_dash_display_form() -> None:
    """RealAuction / Toledo Legal News 'DD-DDDDD' strips the dash to 7 digits."""
    assert normalize_parcel_lucas("12-10314") == "1210314"
    assert normalize_parcel_lucas("26-09998") == "2609998"


def test_lucas_dropped_leading_zero_zfilled() -> None:
    """Defensive: a source dropping a leading zero ('210314') zero-pads back to 7."""
    assert normalize_parcel_lucas("210314") == "0210314"


def test_lucas_whitespace_stripped() -> None:
    assert normalize_parcel_lucas("  12-10314  ") == "1210314"


def test_lucas_condo_s_suffix_preserved() -> None:
    """The 96 condo/split parcels are 8-digit base + 'S'; the qualifier is kept,
    any dash separators among the digits are stripped, and case is normalized.

    NOTE (F-009): the exact canonical byte form of '…S' parcels must still be
    confirmed against a real '…S' parcel traced across sources before go-live.
    """
    assert normalize_parcel_lucas("04349092S") == "04349092S"
    assert normalize_parcel_lucas("04349092s") == "04349092S"
    assert normalize_parcel_lucas("043-49-092S") == "04349092S"
    assert normalize_parcel_lucas("06330001S") == "06330001S"


def test_lucas_none_and_empty() -> None:
    assert normalize_parcel_lucas(None) is None
    assert normalize_parcel_lucas("") is None
    assert normalize_parcel_lucas("   ") is None


def test_lucas_eight_digit_no_s_left_alone() -> None:
    """An 8-digit numeric (ASSESSOR_NUM, the GIS id we do NOT key on) is returned as
    digits — NOT mis-routed to the Cuyahoga dashed form the global normalizer would
    produce ('043-49-092'). This keeps the Lucas namespace clean if such a value leaks in."""
    assert normalize_parcel_lucas("02188002") == "02188002"


# ── Market-aware dispatch: 4 live markets unchanged (zero-behavior-change) ────

def test_live_markets_use_global_fn_object() -> None:
    """Each pre-Lucas market's parcel_normalize_fn IS the global function — not a copy."""
    for m in LIVE_MARKETS:
        assert MARKETS[m]["parcel_normalize_fn"] is normalize_parcel_number


def test_dispatch_equivalence_live_markets() -> None:
    """normalize_parcel_for_market == normalize_parcel_number for every live market over a
    spread of real per-market forms. This is the byte-for-byte proof behind migration 021."""
    samples = [
        "11019068", "110-19-068", "022-13-042",          # Cuyahoga
        "07204700000160", "072047  00016", "D0217   00225", "07204700016",  # Shelby
        "6700526", "0101379", "67-08383", "101379",      # Summit
        "02000184.", "02000185-6", "03001910.001", "35024030846002",  # Wayne
    ]
    for m in LIVE_MARKETS:
        for s in samples:
            assert normalize_parcel_for_market(s, m) == normalize_parcel_number(s), (m, s)


def test_dispatch_lucas_routes_to_lucas_fn() -> None:
    """Once the lucas market is registered (Phase 6), dispatch must route to
    normalize_parcel_lucas. Before that, normalize_parcel_for_market falls back to the
    global fn for the unknown market (asserted in the fallback test below), so this
    activates automatically when MARKETS gains 'lucas'."""
    if "lucas" not in MARKETS:
        return  # Lucas not yet wired (pre-Phase 6); direct normalize_parcel_lucas tests cover it.
    assert MARKETS["lucas"]["parcel_normalize_fn"] is normalize_parcel_lucas
    assert normalize_parcel_for_market("12-10314", "lucas") == "1210314"
    assert normalize_parcel_for_market("04349092S", "lucas") == "04349092S"


def test_dispatch_unknown_market_falls_back_to_global() -> None:
    assert normalize_parcel_for_market("11019068", "nonexistent") == normalize_parcel_number("11019068")


# ── The ambiguity proof that motivates (parcel_number, market) identity ───────

def test_lucas_summit_string_collision() -> None:
    """A bare 7-digit PARID is form-identical under Lucas and Summit — the global
    format-detector cannot tell them apart, so only `market` disambiguates them.
    This is exactly why the composite key is required, not optional."""
    assert normalize_parcel_lucas("1210314") == normalize_parcel_number("1210314") == "1210314"


def test_global_fn_mangles_lucas_condo_form() -> None:
    """The global normalizer mis-handles the Lucas '…S' condo form (its 8 digits hit the
    Cuyahoga compact branch → '043-49-092'), proving auto-detection is dead for Lucas
    and a market-aware normalizer is mandatory — not a fast-follow."""
    assert normalize_parcel_number("04349092S") != "04349092S"
    assert normalize_parcel_lucas("04349092S") == "04349092S"
