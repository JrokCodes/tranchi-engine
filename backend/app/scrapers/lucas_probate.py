"""
Lucas County (OH / Toledo) Probate — DEFERRED to G3, NOT BUILT IN S3.

VENDOR (confirmed engine-box recon 2026-06-21):
  Lucas County Probate Court does NOT use CourtView/Wicket (the Summit template).
  Their case-search live site is Tyler Technologies' Court Records Search on
  `researchoh.tylerhost.net` (linked from https://www.lucasprobate.org/case-access).

  > GET https://researchoh.tylerhost.net/CourtRecordsSearch/Home
  > HTTP/2 403  server: awselb/2.0

  A plain Chrome UA on the engine box gets 403 from the AWS-ELB front. A real
  Tyler session requires:
    - Akamai / AWS WAF challenge resolution (the front-door 403 is the WAF, not
      a missing cookie),
    - JavaScript cookie/JWT minting via the SPA bundle (researchoh is a
      Tyler.OdysseyPortal React app),
    - case-type + date-range search params unique to OH Probate (different
      grammar from the CV/Wicket query strings Summit/Shelby targets use).

  Mapping a CourtView (summit_probate.py) parse on top of researchoh would
  require a from-scratch reverse-engineering pass: NOT cheap, NOT in S3 scope.

DECISION (per the build brief — time-boxed, do LAST, defer-if-brutal):
  Probate is DEFERRED to G3. This file is a stub so the orchestrator doesn't
  carry a stale import. It registers a SignalScraper that returns [] with a
  one-time WARNING log and exposes the deferral rationale via this docstring.
  `MARKET_SCRAPERS['lucas']` does NOT list lucas_probate in `deal_and_signal`,
  so the every-3h run never invokes it (zero ops cost). When G3 builds the
  real fetcher, swap the implementation here and add the scraper key to
  market_config — no change to upstream wiring required.

DEAL-TYPE DIVERSITY FLAG (Marc decision required):
  With probate deferred, Lucas ships with FOUR deal types on this branch:
    1. mortgage_foreclosure         (lucas_realauction Wed)
    2. tax_delinquent_foreclosure   (lucas_realauction Thu)
    3. tax_delinquent (lead)        (lucas_delinquent_tax — TLN TFN fallback)
    4. foreclosure_filing (lead)    (lucas_legalnews /foreclosures/)
  Marc's ≥5 deal-type minimum is therefore NOT met without probate. Land bank
  is NO-GO for Lucas (no county/city land bank in operation), forfeited-land
  is deferred (auditor's once-a-year sale, not a recurring deal stream), so
  PROBATE IS THE SWING 5TH. Either G3 ships Tyler researchoh probate, or Marc
  signs off on the 4-type Lucas market explicitly. Surface this in the close-
  out DECISION-LOG.

WHAT G3 WILL NEED:
  - Reverse the Tyler Odyssey Portal SPA bundle to find the search/case API
    (likely `/api/v1/searches`, JWT-cookie protected).
  - Resolve the AWS-ELB / WAF 403 (Playwright + a residential proxy, or a
    curl_cffi-style Chrome impersonation that beats the JA3 fingerprint).
  - Fill `MARKETS['lucas']['probate_transfer_rule']` once the case-number
    format is locked (e.g. `\b\d{4}EST\d{6}\b` if Lucas follows the OH
    convention; confirm against the live data).
"""
from __future__ import annotations

import logging

from app.scrapers.base import SignalScraper
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Probate Court"
SIGNAL_SOURCE = "lucas_probate"


class LucasProbateScraper(SignalScraper):
    """Stub — deferred to G3. Returns no signals; logs the deferral once."""

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    _warned: bool = False

    async def fetch_signals(self) -> list[RawSignal]:
        if not LucasProbateScraper._warned:
            logger.warning(
                "LucasProbate: DEFERRED — Tyler researchoh.tylerhost.net (NOT CourtView). "
                "S3 build does not include probate; see file docstring for the G3 plan."
            )
            LucasProbateScraper._warned = True
        return []
