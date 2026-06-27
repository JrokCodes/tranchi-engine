"""
Unit tests for the Montgomery County / Dayton (OH) parcel normalizer branch and
zero-regression guarantee over the 5 pre-Dayton markets.

Two things are proven here:
  1. The new normalize_parcel_number() Montgomery branch collapses all three
     delimiter variants (spaced / hyphenated / no-delimiter) of the same PRINT_KEY
     to an identical canonical uppercase string.
  2. Every pre-Dayton market (Cuyahoga, Summit, Shelby, Wayne, Lucas) still
     normalizes to its existing expected output — byte-for-byte unchanged.

The canonical Montgomery form for the traced target parcel:
  'R72 11703 0016' == 'R72-11703-0016' == 'R72117030016'  → 'R72117030016'
(F-001: VANZANT DONNIE / 2064 RUSTIC RD / case 2025CV04878, dayton-parcels-field-map §proof)
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

# ── Montgomery County (OH / Dayton): delimiter-collapse ──────────────────────

TRACED_CANONICAL = "R72117030016"  # F-001 anchor parcel


def test_montgomery_spaced_form() -> None:
    """The AUDGIS_B1 / Spine PRINT_KEY spaced form collapses to canonical."""
    assert normalize_parcel_number("R72 11703 0016") == TRACED_CANONICAL


def test_montgomery_hyphenated_form() -> None:
    """A hyphen-delimited form (some DCR/source variants) collapses to canonical."""
    assert normalize_parcel_number("R72-11703-0016") == TRACED_CANONICAL


def test_montgomery_no_delimiter_form() -> None:
    """The no-delimiter compact form is already canonical (idempotent)."""
    assert normalize_parcel_number("R72117030016") == TRACED_CANONICAL


def test_montgomery_all_three_variants_equal() -> None:
    """All three delimiter variants of the same PRINT_KEY produce the same value."""
    spaced = normalize_parcel_number("R72 11703 0016")
    hyphenated = normalize_parcel_number("R72-11703-0016")
    no_delim = normalize_parcel_number("R72117030016")
    assert spaced == hyphenated == no_delim == TRACED_CANONICAL


def test_montgomery_uppercase_applied() -> None:
    """Lowercase input is uppercased to canonical form."""
    assert normalize_parcel_number("r72 11703 0016") == TRACED_CANONICAL
    assert normalize_parcel_number("r72-11703-0016") == TRACED_CANONICAL


def test_montgomery_second_parcel() -> None:
    """A second real parcel (different PRINT_KEY) normalizes correctly."""
    # R72-16003-0038 → R72160030038
    assert normalize_parcel_number("R72-16003-0038") == "R72160030038"
    assert normalize_parcel_number("R72 16003 0038") == "R72160030038"
    assert normalize_parcel_number("R72160030038") == "R72160030038"


def test_montgomery_twelve_digit_book() -> None:
    """~7% of live AUDGIS parcels have a 4-digit TAXBOOK → letter + 12 digits
    (e.g. 'R72410822 0018'). All delimiter variants must collapse to the same
    12-digit canonical, or those parcels silently miss the spine join."""
    canon = "R724108220018"
    assert normalize_parcel_number("R72410822 0018") == canon
    assert normalize_parcel_number("R72410822-0018") == canon
    assert normalize_parcel_number("R724108220018") == canon
    # second real 12-digit example
    assert normalize_parcel_number("J44204115 0005") == "J442041150005"


def test_montgomery_none_and_empty() -> None:
    assert normalize_parcel_number(None) is None
    assert normalize_parcel_number("") is None
    assert normalize_parcel_number("   ") is None


def test_montgomery_dispatch_via_market() -> None:
    """normalize_parcel_for_market('dayton') routes to normalize_parcel_number and
    produces the Montgomery canonical form — not a passthrough."""
    assert normalize_parcel_for_market("R72 11703 0016", "dayton") == TRACED_CANONICAL
    assert normalize_parcel_for_market("R72-11703-0016", "dayton") == TRACED_CANONICAL
    assert normalize_parcel_for_market("R72117030016", "dayton") == TRACED_CANONICAL


def test_dayton_uses_global_fn() -> None:
    """Dayton's parcel_normalize_fn is the global format-detector (not a separate fn).
    This confirms the letter-prefix is auto-detectable without market-dispatch."""
    assert "dayton" in MARKETS
    assert MARKETS["dayton"]["parcel_normalize_fn"] is normalize_parcel_number


# ── Logan County guard (shares the DCR feed — purely numeric, must NOT be caught) ──

def test_logan_numeric_not_caught_by_montgomery_branch() -> None:
    """Logan County parcels are purely numeric (e.g. '43-031-001') and must NOT
    be normalized as Montgomery. They fall through to other branches or passthrough."""
    # A Logan-style numeric (no leading letter) should NOT produce a 12-char
    # letter-prefixed canonical. It either hits another branch or passthrough.
    result = normalize_parcel_number("43-031-001")
    assert not (len(result) == 12 and result[0].isalpha()), (
        f"Logan numeric {result!r} was incorrectly caught by the Montgomery branch"
    )


# ── REGRESSION: pre-Dayton markets unchanged ─────────────────────────────────

def test_regression_cuyahoga_canonical_form() -> None:
    """Cuyahoga DDD-NN-NNN canonical form is returned unchanged."""
    assert normalize_parcel_number("110-19-068") == "110-19-068"
    assert normalize_parcel_number("022-13-042") == "022-13-042"


def test_regression_cuyahoga_compact_form() -> None:
    """Cuyahoga 8-digit compact form is expanded to DDD-NN-NNN."""
    assert normalize_parcel_number("11019068") == "110-19-068"


def test_regression_summit_seven_digit() -> None:
    """Summit 7-digit zero-padded form is returned unchanged."""
    assert normalize_parcel_number("6700526") == "6700526"
    assert normalize_parcel_number("0101379") == "0101379"


def test_regression_summit_dash_display() -> None:
    """Summit fiscal-office display dash is stripped to 7 digits."""
    assert normalize_parcel_number("67-08383") == "6708383"


def test_regression_shelby_spaced_numeric_map() -> None:
    """Shelby spaced numeric-MAP form normalizes to 14-char canonical."""
    assert normalize_parcel_number("072047  00016") == "07204700000160"


def test_regression_shelby_14char_canonical() -> None:
    """Shelby 14-char canonical form is returned unchanged (idempotent)."""
    assert normalize_parcel_number("07204700000160") == "07204700000160"


def test_regression_shelby_paid_compact() -> None:
    """Shelby PAID compact (10-11 digit numeric) normalizes to 14-char."""
    assert normalize_parcel_number("07204700016") == "07204700000160"


def test_regression_wayne_trailing_period() -> None:
    """Wayne Detroit ward parcel with trailing period is returned verbatim."""
    assert normalize_parcel_number("02000184.") == "02000184."


def test_regression_wayne_hyphen_range() -> None:
    """Wayne Detroit hyphen-range parcel is returned verbatim (uppercased)."""
    assert normalize_parcel_number("02000185-6") == "02000185-6"


def test_regression_lucas_dash_display() -> None:
    """Lucas RealAuction/Legal-News display form normalizes via normalize_parcel_lucas."""
    assert normalize_parcel_lucas("12-10314") == "1210314"
    assert normalize_parcel_for_market("12-10314", "lucas") == "1210314"


def test_regression_lucas_condo_s_suffix() -> None:
    """Lucas condo/split 8-digit+S parcel is handled by normalize_parcel_lucas."""
    assert normalize_parcel_lucas("04349092S") == "04349092S"
    assert normalize_parcel_for_market("04349092S", "lucas") == "04349092S"


def test_regression_global_fn_unchanged_for_live_markets() -> None:
    """normalize_parcel_for_market == normalize_parcel_number for all pre-Dayton markets
    that use the global format-detector (Cuyahoga, Shelby, Summit, Wayne).
    This mirrors the zero-behavior-change guarantee from test_normalize_parcel_lucas."""
    global_fn_markets = ("cuyahoga", "shelby", "summit", "wayne")
    samples = [
        "11019068", "110-19-068", "022-13-042",                    # Cuyahoga
        "07204700000160", "072047  00016", "D0217   00225",        # Shelby
        "07204700016",                                              # Shelby PAID
        "6700526", "0101379", "67-08383", "101379",                # Summit
        "02000184.", "02000185-6", "03001910.001",                  # Wayne
    ]
    for market in global_fn_markets:
        for s in samples:
            assert normalize_parcel_for_market(s, market) == normalize_parcel_number(s), (
                f"Market {market!r} changed output for {s!r}"
            )


def test_regression_dayton_does_not_change_other_market_outputs() -> None:
    """With dayton in MARKETS, all pre-Dayton parcel forms still normalize identically
    to what they produced before (dayton uses global fn → no side-effect on other markets)."""
    # Spot-check representative forms that might overlap with Montgomery's range.
    assert normalize_parcel_number("110-19-068") == "110-19-068"    # Cuyahoga: unaffected
    assert normalize_parcel_number("6700526") == "6700526"           # Summit 7-digit: unaffected
    assert normalize_parcel_number("D0217   00225") == "D0217000002250"  # Shelby alpha-MAP
    assert normalize_parcel_number("02000184.") == "02000184."       # Wayne: unaffected
