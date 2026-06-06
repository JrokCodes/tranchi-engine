"""One-click verification deep-links for a listing — built deterministically from the
address + parcel + market. NO LLM, no scraping. Shared by the read API (dashboard verify
buttons) and the /tranchi-verify skill so both use identical link logic.

Verified 2026-06-06 (real browser):
  - Zillow  /homes/{q}_rb/  auto-redirects to the exact /homedetails/ property page. ✓
  - Redfin  has NO deterministic deep-link: ?q= lands on the homepage, and the
    autocomplete API that returns the real /STATE/City/Addr/home/{id} URL is 403 from all
    datacenter IPs (WebFetch AND EC2). So we bounce through a Google site:redfin.com search
    — the property page is reliably the first result, one extra click. ✓
  - Trustee parcel page (TN) needs the NATIVE spaced parcel id (e.g. '013019  00015'),
    NOT our 14-char canonical — confirmed returns owner+tax for any Shelby category. ✓
  - MyPlace (OH) takes the canonical parcel.
"""
from __future__ import annotations

import urllib.parse


def _zillow(addr: str, city: str | None, state: str | None, zip_: str | None) -> str:
    q = " ".join(p for p in (addr, city, state, zip_) if p)
    return "https://www.zillow.com/homes/" + urllib.parse.quote(q) + "_rb/"


def _redfin(addr: str, city: str | None, state: str | None, zip_: str | None) -> str:
    q = ", ".join(p for p in (addr, city, state, zip_) if p)
    return "https://www.google.com/search?q=" + urllib.parse.quote(f"site:redfin.com {q}")


def _registry(state: str | None, native_parcel_id: str | None,
              canonical_parcel: str | None) -> tuple[str | None, str]:
    """(url, label) for the market's county parcel registry. Market-aware."""
    if state == "TN":
        if native_parcel_id:
            return ("https://apps2.shelbycountytrustee.com/Parcel?parcel="
                    + urllib.parse.quote(native_parcel_id, safe=""), "Shelby Trustee")
        # No native id yet (stub parcel) → Trustee address-search SPA.
        return "https://apps2.shelbycountytrustee.com/PropertyClient", "Shelby Trustee"
    if state == "OH":
        if canonical_parcel:
            return ("https://myplace.cuyahogacounty.gov/?parcel="
                    + urllib.parse.quote(canonical_parcel), "Cuyahoga MyPlace")
        return "https://myplace.cuyahogacounty.gov/", "Cuyahoga MyPlace"
    return None, "County Registry"


def build_verify_links(*, address: str | None, city: str | None, state: str | None,
                       zip_: str | None, native_parcel_id: str | None,
                       canonical_parcel: str | None, source_url: str | None) -> dict:
    """Return the per-listing verify-link bundle the dashboard renders as buttons."""
    reg_url, reg_label = _registry(state, native_parcel_id, canonical_parcel)
    return {
        "zillow": _zillow(address, city, state, zip_) if address else None,
        "redfin": _redfin(address, city, state, zip_) if address else None,
        "registry": reg_url,
        "registry_label": reg_label,
        "source": source_url,
    }
