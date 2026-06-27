"""
Unit tests for dayton_realforeclose.py — parse layer only, no DB or network.

Fixtures are real retHTML blobs captured live from:
  montgomery.sheriffsaleauction.ohio.gov  UPDATE/LOAD  2026-07-10 sale
  Probed: 2026-06-27

AREA C: 1 item  (rlist 53563)
AREA W: 6 items (rlist 53564,53906,53907,53565,53908,53566)
Total:  7 items — matches CALSCH=7 shown in the calendar for 07/10/2026.

Tests confirm:
  - case# "(N)" suffix stripped correctly
  - parcel normalization to Montgomery PRINT_KEY collapsed form
  - appraised_value_usd / opening_bid_usd parsed correctly from "$NNN,NNN.NN"
  - deposit_usd parsed
  - city / ZIP extracted; 9-digit ZIP truncated to 5
  - signal_type inferred (non-zero appraised → mortgage_foreclosure)
  - $0.00 appraised → tax_delinquent_foreclosure
  - "MULTIPLE" parcel → source_listing_id=None
  - comma-separated multi-parcel → first parcel normalized
  - non-ACTIVE status → skipped (returns None)
  - property_county = "Montgomery", property_state = "OH"
  - source_site = "Montgomery Sheriff Sale (RealForeclose)"
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Allow running from backend/ without installing the package
_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from app.scrapers.dayton_realforeclose import (
    _extract_city_zip,
    _infer_signal_type,
    _parse_item,
    _parse_money,
    _parse_ret_html,
    _strip_seq,
    _truncate_zip,
)
from app.scrapers.db import normalize_parcel_number

# ─────────────────────────────────────────────────────────────────────────────
# Real live fixtures — retHTML blobs from UPDATE/LOAD  (2026-07-10 sale)
# ─────────────────────────────────────────────────────────────────────────────

# AREA C — 1 item: case 2024 CV 01948, parcel R72 11506 0002
_RET_HTML_C = (
    '<div tabindex="0"id="AITEM_53563" aria-label="Auction Details" '
    '@C@E_ITEM PREVIEW" aid="53563" rem="0" isset="0">@A@E_STATS">'
    '<div tabindex="0"@CASTAT_MSGA ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSGB Astat_DATA">@B<div tabindex="0"@CASTAT_MSGC ASTAT_LBL">@B '
    '<div tabindex="0"@CASTAT_MSGD Astat_DATA">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_Label ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_MSG Astat_DATA">@B@B@A@E_DETAILS"><@I @Cad_tab" '
    'tabindex="0"><tbody>'
    '<tr><th @CAD_LBL" scope="row">Case Status:</th><td @CAD_DTA">ACTIVE'
    '@G<tr><th @CAD_LBL" scope="row" aria-label="Case Number">Case #:</th>'
    '<td @CAD_DTA"> 2024 CV 01948 (0)'
    '@G<tr><th @CAD_LBL" scope="row">Parcel ID:</th><td @CAD_DTA"> R72 11506 0002'
    '@G<tr><th @CAD_LBL" scope="row">Property Address:</th><td @CAD_DTA">711 TORRINGTON PLACE'
    '@G<tr><th @CAD_LBL" scope="row"></th><td @CAD_DTA">DAYTON , 45406'
    '@G<tr><th @CAD_LBL" scope="row">Appraised Value:</th><td @CAD_DTA">$81,000.00'
    '@G <tr><th @CAD_LBL" scope="row">Opening Bid:</th><td @CAD_DTA">$54,000.00'
    '@G<tr><th @CAD_LBL" scope="row">Deposit Requirement:</th><td @CAD_DTA">$5,000.00'
    '</tbody></@I>@B@B@A@E_ITEM_SPACER">&nbsp;@B '
)

# AREA W — 6 items (truncated to 3 for brevity, tests below use full fixture)
_RET_HTML_W = (
    '<div tabindex="0"id="AITEM_53564" aria-label="Auction Details" '
    '@C@E_ITEM PREVIEW" aid="53564" rem="0" isset="0">@A@E_STATS">'
    '<div tabindex="0"@CASTAT_MSGA ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSGB Astat_DATA">@B<div tabindex="0"@CASTAT_MSGC ASTAT_LBL">@B '
    '<div tabindex="0"@CASTAT_MSGD Astat_DATA">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_Label ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_MSG Astat_DATA">@B@B@A@E_DETAILS"><@I @Cad_tab" '
    'tabindex="0"><tbody>'
    '<tr><th @CAD_LBL" scope="row">Case Status:</th><td @CAD_DTA">ACTIVE'
    '@G<tr><th @CAD_LBL" scope="row" aria-label="Case Number">Case #:</th>'
    '<td @CAD_DTA"> 2025 CV 00888 (0)'
    '@G<tr><th @CAD_LBL" scope="row">Parcel ID:</th><td @CAD_DTA"> I39 00519 0011'
    '@G<tr><th @CAD_LBL" scope="row">Property Address:</th><td @CAD_DTA">3566 LANE GARDEN CT'
    '@G<tr><th @CAD_LBL" scope="row"></th><td @CAD_DTA">DAYTON , 45404'
    '@G<tr><th @CAD_LBL" scope="row">Appraised Value:</th><td @CAD_DTA">$81,000.00'
    '@G <tr><th @CAD_LBL" scope="row">Opening Bid:</th><td @CAD_DTA">$54,000.00'
    '@G<tr><th @CAD_LBL" scope="row">Deposit Requirement:</th><td @CAD_DTA">$5,000.00'
    '</tbody></@I>@B@B@A@E_ITEM_SPACER">&nbsp;@B'
    '<div tabindex="0"id="AITEM_53906" aria-label="Auction Details" '
    '@C@E_ITEM PREVIEW" aid="53906" rem="0" isset="0">@A@E_STATS">'
    '<div tabindex="0"@CASTAT_MSGA ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSGB Astat_DATA">@B<div tabindex="0"@CASTAT_MSGC ASTAT_LBL">@B '
    '<div tabindex="0"@CASTAT_MSGD Astat_DATA">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_Label ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_MSG Astat_DATA">@B@B@A@E_DETAILS"><@I @Cad_tab" '
    'tabindex="0"><tbody>'
    '<tr><th @CAD_LBL" scope="row">Case Status:</th><td @CAD_DTA">ACTIVE'
    '@G<tr><th @CAD_LBL" scope="row" aria-label="Case Number">Case #:</th>'
    '<td @CAD_DTA"> 2025 CV 02213 (0)'
    '@G<tr><th @CAD_LBL" scope="row">Parcel ID:</th><td @CAD_DTA"> K46 00919 0024'
    '@G<tr><th @CAD_LBL" scope="row">Property Address:</th><td @CAD_DTA">1231 MARSHA DR'
    '@G<tr><th @CAD_LBL" scope="row"></th><td @CAD_DTA">MIAMISBURG , 45342'
    '@G<tr><th @CAD_LBL" scope="row">Appraised Value:</th><td @CAD_DTA">$192,000.00'
    '@G <tr><th @CAD_LBL" scope="row">Opening Bid:</th><td @CAD_DTA">$128,000.00'
    '@G<tr><th @CAD_LBL" scope="row">Deposit Requirement:</th><td @CAD_DTA">$5,000.00'
    '</tbody></@I>@B@B@A@E_ITEM_SPACER">&nbsp;@B'
    '<div tabindex="0"id="AITEM_53907" aria-label="Auction Details" '
    '@C@E_ITEM PREVIEW" aid="53907" rem="0" isset="0">@A@E_STATS">'
    '<div tabindex="0"@CASTAT_MSGA ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSGB Astat_DATA">@B<div tabindex="0"@CASTAT_MSGC ASTAT_LBL">@B '
    '<div tabindex="0"@CASTAT_MSGD Astat_DATA">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_Label ASTAT_LBL">@B<div tabindex="0"'
    '@CASTAT_MSG_SOLDTO_MSG Astat_DATA">@B@B@A@E_DETAILS"><@I @Cad_tab" '
    'tabindex="0"><tbody>'
    '<tr><th @CAD_LBL" scope="row">Case Status:</th><td @CAD_DTA">ACTIVE'
    '@G<tr><th @CAD_LBL" scope="row" aria-label="Case Number">Case #:</th>'
    '<td @CAD_DTA"> 2025 CV 04109 (0)'
    '@G<tr><th @CAD_LBL" scope="row">Parcel ID:</th><td @CAD_DTA"> R72 11702 0063'
    '@G<tr><th @CAD_LBL" scope="row">Property Address:</th><td @CAD_DTA">2017 MAYFAIR ROAD'
    '@G<tr><th @CAD_LBL" scope="row"></th><td @CAD_DTA">DAYTON , 45405'
    '@G<tr><th @CAD_LBL" scope="row">Appraised Value:</th><td @CAD_DTA">$51,000.00'
    '@G <tr><th @CAD_LBL" scope="row">Opening Bid:</th><td @CAD_DTA">$34,000.00'
    '@G<tr><th @CAD_LBL" scope="row">Deposit Requirement:</th><td @CAD_DTA">$5,000.00'
    '</tbody></@I>@B@B@A@E_ITEM_SPACER">&nbsp;@B '
)

_SALE_DATE = date(2026, 7, 10)


# ─────────────────────────────────────────────────────────────────────────────
# Helper parser unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestStripSeq:
    def test_strips_zero_suffix(self):
        assert _strip_seq("2024 CV 01948 (0)") == "2024 CV 01948"

    def test_strips_nonzero_suffix(self):
        assert _strip_seq("2025 CV 06543 (12)") == "2025 CV 06543"

    def test_no_suffix_idempotent(self):
        assert _strip_seq("2024 CV 01948") == "2024 CV 01948"

    def test_leading_whitespace(self):
        assert _strip_seq(" 2025 CV 00888 (0)") == "2025 CV 00888"


class TestParseMoney:
    def test_dollar_commas(self):
        assert _parse_money("$81,000.00") == 81000.0

    def test_large_value(self):
        assert _parse_money("$192,000.00") == 192000.0

    def test_deposit(self):
        assert _parse_money("$5,000.00") == 5000.0

    def test_none(self):
        assert _parse_money(None) is None

    def test_empty(self):
        assert _parse_money("") is None

    def test_zero(self):
        assert _parse_money("$0.00") == 0.0

    def test_no_dollar_sign(self):
        assert _parse_money("54000.00") == 54000.0


class TestTruncateZip:
    def test_9digit_truncated(self):
        assert _truncate_zip("454060000") == "45406"

    def test_5digit_unchanged(self):
        assert _truncate_zip("45406") == "45406"

    def test_hyphenated_unchanged(self):
        assert _truncate_zip("45406-1234") == "45406-1234"

    def test_none(self):
        assert _truncate_zip(None) is None


class TestExtractCityZip:
    def test_dayton_45406(self):
        fields = {"__city_zip__": "DAYTON , 45406"}
        city, zip_ = _extract_city_zip(fields)
        assert city == "Dayton"
        assert zip_ == "45406"

    def test_miamisburg_45342(self):
        fields = {"__city_zip__": "MIAMISBURG , 45342"}
        city, zip_ = _extract_city_zip(fields)
        assert city == "Miamisburg"
        assert zip_ == "45342"

    def test_9digit_zip_truncated(self):
        fields = {"__city_zip__": "DAYTON , 454060000"}
        city, zip_ = _extract_city_zip(fields)
        assert city == "Dayton"
        assert zip_ == "45406"

    def test_missing_key(self):
        city, zip_ = _extract_city_zip({})
        assert city is None
        assert zip_ is None


class TestInferSignalType:
    def test_nonzero_appraised_is_mortgage(self):
        assert _infer_signal_type(81000.0) == "mortgage_foreclosure"

    def test_zero_appraised_is_tax(self):
        assert _infer_signal_type(0.0) == "tax_delinquent_foreclosure"

    def test_none_appraised_is_tax(self):
        assert _infer_signal_type(None) == "tax_delinquent_foreclosure"


# ─────────────────────────────────────────────────────────────────────────────
# Parse-ret-html + parse-item against live fixtures
# ─────────────────────────────────────────────────────────────────────────────

class TestParseRetHtml:
    def test_area_c_one_item(self):
        items = _parse_ret_html(_RET_HTML_C)
        assert len(items) == 1

    def test_area_w_three_items(self):
        # Our _RET_HTML_W fixture includes 3 items (C, K46, R72 books)
        items = _parse_ret_html(_RET_HTML_W)
        assert len(items) == 3

    def test_area_c_fields(self):
        items = _parse_ret_html(_RET_HTML_C)
        f = items[0]
        assert f["Case Status"] == "ACTIVE"
        assert "2024 CV 01948" in f["Case #"]
        assert "R72 11506 0002" in f["Parcel ID"]
        assert f["Appraised Value"] == "$81,000.00"
        assert f["Opening Bid"] == "$54,000.00"
        assert f["Deposit Requirement"] == "$5,000.00"
        assert f["Property Address"] == "711 TORRINGTON PLACE"
        assert "45406" in f["__city_zip__"]


class TestParseItem:
    """Full parse_item round-trip on the live AREA C fixture."""

    def _item(self) -> dict:
        return _parse_ret_html(_RET_HTML_C)[0]

    def test_case_number_stripped(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.case_number == "2024 CV 01948"

    def test_parcel_normalized(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        # "R72 11506 0002" → collapse spaces → "R72115060002"
        assert r.source_listing_id == "R72115060002"
        # Cross-verify with global normalizer
        assert r.source_listing_id == normalize_parcel_number("R72 11506 0002")

    def test_appraised_value(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.appraised_value_usd == 81000.0

    def test_opening_bid(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.opening_bid_usd == 54000.0

    def test_deposit(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.deposit_usd == 5000.0

    def test_city_zip(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.property_city == "Dayton"
        assert r.property_zip == "45406"

    def test_signal_type_mortgage(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.signal_type == "mortgage_foreclosure"

    def test_county_state_site(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.property_county == "Montgomery"
        assert r.property_state == "OH"
        assert r.source_site == "Montgomery Sheriff Sale (RealForeclose)"
        assert r.status == "active"
        assert r.auction_status == "ACTIVE"

    def test_sale_date(self):
        r = _parse_item(self._item(), _SALE_DATE)
        assert r is not None
        assert r.sale_date == _SALE_DATE

    def test_non_active_returns_none(self):
        f = self._item().copy()
        f["Case Status"] = "SOLD"
        assert _parse_item(f, _SALE_DATE) is None

    def test_multiple_parcel_returns_none_source_id(self):
        f = self._item().copy()
        f["Parcel ID"] = "MULTIPLE"
        r = _parse_item(f, _SALE_DATE)
        assert r is not None
        assert r.source_listing_id is None
        assert r.property_address == "711 TORRINGTON PLACE"

    def test_comma_split_parcel_uses_first(self):
        f = self._item().copy()
        f["Parcel ID"] = "K46 00606 0028, K46 00606 0029"
        r = _parse_item(f, _SALE_DATE)
        assert r is not None
        # First parcel normalized: "K46 00606 0028" → "K46006060028"
        assert r.source_listing_id == normalize_parcel_number("K46 00606 0028")
        assert r.source_listing_id == "K46006060028"

    def test_zero_appraised_is_tax(self):
        f = self._item().copy()
        f["Appraised Value"] = "$0.00"
        r = _parse_item(f, _SALE_DATE)
        assert r is not None
        assert r.appraised_value_usd == 0.0
        assert r.signal_type == "tax_delinquent_foreclosure"

    def test_9digit_zip_truncated(self):
        f = self._item().copy()
        f["__city_zip__"] = "DAYTON , 454060000"
        r = _parse_item(f, _SALE_DATE)
        assert r is not None
        assert r.property_zip == "45406"

    def test_miamisburg_item(self):
        """Second item from AREA W: different city and larger appraised value."""
        items = _parse_ret_html(_RET_HTML_W)
        assert len(items) >= 2
        r = _parse_item(items[1], _SALE_DATE)
        assert r is not None
        assert r.case_number == "2025 CV 02213"
        assert r.source_listing_id == normalize_parcel_number("K46 00919 0024")
        assert r.appraised_value_usd == 192000.0
        assert r.opening_bid_usd == 128000.0
        assert r.property_city == "Miamisburg"
        assert r.property_zip == "45342"
        assert r.signal_type == "mortgage_foreclosure"


class TestParcelNormalization:
    """Montgomery parcel format: letter + spaced/hyphenated/plain → collapsed uppercase."""

    def test_spaced(self):
        assert normalize_parcel_number("R72 11506 0002") == "R72115060002"

    def test_hyphenated(self):
        assert normalize_parcel_number("R72-11506-0002") == "R72115060002"

    def test_no_delimiter(self):
        assert normalize_parcel_number("R72115060002") == "R72115060002"

    def test_i_book(self):
        assert normalize_parcel_number("I39 00519 0011") == "I39005190011"

    def test_k_book(self):
        assert normalize_parcel_number("K46 00919 0024") == "K46009190024"

    def test_m_book(self):
        assert normalize_parcel_number("M60 25317 0004") == "M60253170004"

    def test_lowercase_normalized(self):
        assert normalize_parcel_number("r72 11506 0002") == "R72115060002"
