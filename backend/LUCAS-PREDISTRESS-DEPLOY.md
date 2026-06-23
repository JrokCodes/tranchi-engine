# Lucas / Toledo — Pre-Distress Side: G2 Deploy Record

**Deployed to prod:** 2026-06-23 ~00:27 UTC (engine box `tranchi-engine`, Postgres `tranchi`).
**Outcome:** Lucas/Toledo gains its full pre-distress side — **0 → 116 leads** across **all three
designed signal types**, matching the other markets (Summit's pre-distress is the same triplet:
foreclosure_filing + tax_delinquent + probate).

| Signal type | Leads live | Signal source |
|---|---|---|
| Foreclosure filing | 26 | Toledo Legal News Common Pleas complaints (`lucas_tln_foreclosures`) |
| Probate (deceased owner) | 77 | Toledo Legal News daily probate filings → AREIS owner join (`lucas_tln_probate`) |
| Tax delinquent | 13 | TLN Treasurer cases, RULE #1 gated (`lucas_tln_tfn`) |
| **Total pre-distress (`distress_stage='distress_signal'`)** | **116** | |

---

## What shipped

**New scrapers** (`backend/app/scrapers/`):
- `lucas_foreclosure_filings.py` — SignalScraper. Parses TLN `/legal_notices/foreclosures/` Common
  Pleas complaints → `foreclosure_filing` signals. Inline parcel + AREIS address→spine unique-match
  join for parcel-less notices. Excludes TF (Treasurer tax) cases.
- `lucas_probate.py` — SignalScraper. Parses TLN `/courts/probate/` daily filing lists, keeps recent
  ESTATE openings, joins decedent name → Lucas AREIS owner (unique-match, person-only), emits
  `probate` signals badged `match_confidence='probable'` (confidence 0.6).

**Changed:**
- `lucas_legalnews.py` — stopped emitting foreclosure complaints as `buy_now` listings (they moved to
  the signal path). It now owns only the sheriff-sale roster. 50 mis-classified buy_now filings retired.
- `market_config.py` — `MARKET_SCRAPERS['lucas']` += the two scrapers; `distress_lead_rules` added
  `probate` (spine, gate=None); `source_meta` tax/filing/probate leads → category `lead`.
- `run.py` — registered + constructed both scrapers (pool for the spine/owner joins).

**Migrations:**
- `022_lucas_distress_leads.py` — seed `tax_delinquent` + `foreclosure_filing` lead rows (disabled).
- `023_lucas_probate_lead.py` — seed `probate` lead row (disabled).
- Rows shipped **disabled**, flipped `enabled=true` only after the surface_distress dry-run sanity-check
  (the Summit/Wayne G1 discipline). DB is now at alembic head **023**.

**Commits** (branch `lucas-toledo-s3`, local on the box — push from a write-access workstation so it
lands on GitHub for review; the box deploy key is read-only):
- `7f17e91` — foreclosure_filing scraper + tax/filing lead enablement
- `ec37fee` — probate ESTATE scraper

---

## How it was deployed (and verified)

Every leg followed the auction-side discipline: **build → rehearse on a 1:1 prod clone → dry-run
sanity-check → enable → deploy → verify → unpause crons**.

**Rehearsal (throwaway clone `tranchi_rehearsal`, full pipeline through the real `run.py` +
`surface_distress`):**
- foreclosure_filing 26–28 leads, tax 13 leads (RULE #1: 13/13 residential LUC 5xx), probate 77 leads.
- **4 incumbent markets byte-identical** to baseline (cuyahoga 1034/14791, shelby 4866/21102,
  summit 673/6576, wayne 2943/43721).
- Integrity counters zero throughout (`null_market_parcels=0`, `null_market_signals=0`,
  `orphan_signals=0`).

**Prod verification (post-deploy):** identical shape — 116 Lucas leads, incumbents untouched
(`surface_distress` inserted=0 for all non-Lucas sources), integrity zero, API healthy (200), crons
restored byte-identical to pre-deploy.

---

## The probate source decision (exhaustively established)

Lucas probate could **not** be scraped from its court portal:
- **eAccess / CourtView** (`eaccess.lucas-co-probate-ct.org`) — **dead**: 100% ICMP packet loss, 5/5
  HTTPS attempts never established TCP, from the engine box AND a residential IP. Decommissioned.
- **re:SearchOH** (`researchoh.tylerhost.net`) — **registration-gated** (renders a "Sign in with your
  eFileOH Account" wall, zero search inputs, only `/api/auth/claims`) **and carries only the General
  Division** (probate is a separate court, not published there as of the 2026-01-06 rollout).

**The public source that works:** Toledo Legal News publishes the court's **daily probate filing
lists** at `/courts/probate/` (HTTP 200, no login) — the same public-notice pattern as the tax and
filing legs. `lucas_probate` parses the `Estate` entries
(`YYYY-EST-NNNNNN ESTATE OF <Decedent>. <actions>`), filters to recent-year openings, and recovers
the parcel by joining the decedent name to the AREIS owner record.

---

## Honest caveats (do not oversell)

- **Probate join is name-only.** TLN probate notices carry no address, so the parcel comes from a
  decedent-name → AREIS owner match. Unique-match only, person-type owners only, ambiguous/no-match
  dropped. ~22% of recent decedents matched (only property *owners* become leads — correct). Every
  lead is badged `match_confidence='probable'` (confidence 0.6) — never "confirmed"; a coincidental
  living namesake is the one real mis-join risk, which the badge surfaces honestly. The read layer
  must show the probable/unverified badge.
- **Coverage grows via cron.** TLN rate-limits aggressively; tonight's runs covered ~7 recent days of
  filings. The every-3h cron now runs all three scrapers and walks forward, so lead counts grow over
  the coming days without intervention. (Idempotent — natural-key dedup + per-parcel lead dedup.)
- **Lead `property_city` is blank** on spine-sourced leads (Lucas AREIS situs format `"City Oh ZIP"`
  doesn't match `surface_distress`'s shared city-extraction regex). The full address incl. city is in
  `property_address`; zip resolves. Cosmetic; not touched pre-deploy to avoid changing shared code.

---

## Fast-follows (reachable, scoped — not done tonight)

1. **Tax-expansion (deepen the 13 tax leads).** The full ~19K Column delinquent list stays blocked
   (Cloudflare/JWT). But `icare.co.lucas.oh.us` (iasWorld per-parcel tax — same vendor as a scraper we
   already have) and `publicnoticesohio.com` (the annual full-universe list, ASP.NET) are both
   reachable. Build a county-wide tax-delinquent signal off one of these.
2. **Probate via the authenticated court index.** A fuller probate feed than TLN's public-notice
   subset would require a registered eFileOH/re:SearchOH account + an authenticated scraper. That's a
   business/ToS decision (account ownership + Tyler terms) for Jayden — not a unilateral build.

---

## What's left for the operator

- **Push** `7f17e91` + `ec37fee` (and the earlier frontend commit `e1f3aba`) from a write-access
  workstation so Jayden can review on GitHub. Everything is already live + verified on prod regardless.
- Optional: drop the audit/throwaway artifacts per the usual G2 sign-off.
