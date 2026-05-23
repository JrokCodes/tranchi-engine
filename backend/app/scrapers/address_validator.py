"""
Shared address sanity check for enrichment pipelines.

Rejects obvious non-property strings BEFORE hitting any external API.
Ported from Gotham verbatim — logic is generic, not state-specific.
"""
from __future__ import annotations

import re

_OFFICE_SUITE = re.compile(r"\b(suite|ste\.?)\s*\d", re.IGNORECASE)
_LEGAL_NOTICE = re.compile(r"notice of sale|notice of foreclosure", re.IGNORECASE)
_CASE_NUMBER = re.compile(r"^\s*\d{2}-CV-|^\s*\d{2}-C-\d|case no\.?\s*\d", re.IGNORECASE)
_MULTI_PROPERTY = re.compile(r"\b(arta|et al)\b|i/r/t/a", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"^\s*\d+\s*[,]")
_HAS_STREET_PREFIX = re.compile(r"^\s*\d+\s+\S")


def is_valid_property_address(address: str | None, city: str | None = "") -> tuple[bool, str]:
    """Return (is_valid, reason).

    Returns (False, <reason>) for obvious non-property strings so the caller
    can mark the listing as un-enrichable and stop retrying it.
    """
    if not address or not address.strip():
        return False, "empty_address"

    addr = address.strip()

    if _CASE_NUMBER.search(addr):
        return False, "case_number_prefix"

    if _LEGAL_NOTICE.search(addr):
        return False, "legal_notice_text"

    if _MULTI_PROPERTY.search(addr):
        return False, "multi_property_notice"

    if _OFFICE_SUITE.search(addr):
        return False, "office_suite"

    if _BARE_NUMBER.match(addr):
        return False, "malformed_address"

    if not _HAS_STREET_PREFIX.match(addr):
        return False, "no_street_number"

    if len(addr) > 150:
        return False, "address_too_long"

    return True, "ok"
