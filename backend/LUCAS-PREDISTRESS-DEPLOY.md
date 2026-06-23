# Lucas / Toledo — Full Build-Out: G2 Deploy Record

**Session:** 2026-06-22 → 06-23 (engine box `tranchi-engine`, Postgres `tranchi`).
**Outcome:** Lucas/Toledo went from the **thinnest market in the engine** (184 active listings, 0
pre-distress) to **mid-pack and fully built-out** in one session — **12,359 active listings**
(11,802 pre-distress + 557 buy-now), **6 new scrapers**, the **most pre-distress lead types of any
market**, and tax at **full parity** with the big markets.

Every source shipped through the same discipline: **build → rehearse on a 1:1 prod clone → dry-run
sanity-check → enable → deploy → verify → restore crons**. The 4 incumbent markets stayed
**byte-identical** and integrity counters (`null_market`, `orphan_signals`) **zero** at every step.

---

## Final state

### Pre-distress (`distress_stage='distress_signal'`) — 11,802 leads
| Lead type | Leads | Source |
|---|---|---|
| Tax delinquent (FULL roll) | 11,492 | Public AREIS COLLECTION (`lucas_areis_collection`) |
| Vacant + tax-delinquent | 205 | Auditor GIS `Vacant_Delinquent` (`lucas_areis_vacant_delinquent`) |
| Probate (deceased owner) | 77 | Toledo Legal News daily probate filings (`lucas_tln_probate`) |
| Foreclosure filing | 28 | Toledo Legal News Common Pleas complaints (`lucas_tln_foreclosures`) |

### Buy-now (`distress_stage='buy_now'`) — 557 listings
| Source | Listings |
|---|---|
| Lucas Land Bank (owned inventory) | 460 |
| Lucas Sheriff Sale (RealAuction) | 47 |
| Lucas Forfeited Land (tax-deed) | 31 |
| Toledo Legal News (sheriff cross-check) | 19 |

### Cross-market standing (final)
| Market | Pre-distress | Buy-now | Lead types | Total active |
|---|---|---|---|---|
| Wayne–Detroit | 43,721 | 2,945 | 1 | 46,666 |
| Shelby–Memphis | 21,102 | 4,864 | 2 | 25,966 |
| Cuyahoga | 14,791 | 1,012 | 2 | 15,803 |
| **Lucas–Toledo** | **11,802** | **557** | **4 (most)** | **12,359** |
| Summit–Akron | 6,576 | 681 | 2 | 7,257 |

---

## What shipped this session

**New scrapers** (`backend/app/scrapers/`):
- `lucas_areis_delinquent.py` — **the headline.** Downloads the Auditor's entire AREIS database
  (public, no-login ArcGIS Hub item `8e00e957…`), parses the COLLECTION tax-duplicate table,
  emits `tax_delinquent` signals. 29,452 delinquent county-wide → 11,492 RULE-#1 leads. Requires
  `mdbtools`.
- `lucas_probate.py` — TLN `/courts/probate/` daily estate filings → decedent name → AREIS owner
  unique-match → `probate` signals (badged `match_confidence='probable'`).
- `lucas_foreclosure_filings.py` — TLN Common Pleas complaints → `foreclosure_filing` signals
  (inline parcel + AREIS address→spine unique-match join).
- `lucas_vacant_delinquent.py` — Auditor GIS `Vacant_Delinquent` layer → `vacant_delinquent`
  signals (the curated vacant+delinquent hot subset).
- `lucas_forfeited_land.py` — Auditor GIS `Forfeited_Land_Sales` → tax-deed buy-now listings.
- `lucas_landbank.py` — Auditor CAMA GIS filtered to owner = Land Reutilization Corp → ~460
  land-bank-owned buy-now listings (the full owned inventory, not the ~12 broker-listed subset).

**Changed:** `lucas_legalnews.py` (stopped mis-filing foreclosure complaints as buy_now — moved to
the signal path); `run.py` + `market_config.py` (registration, `distress_lead_rules`, source_meta,
staleness, deal_sources).

**Migrations** (DB now at head **025**): 022 (tax + foreclosure_filing leads), 023 (probate lead),
024 (vacant_delinquent lead), 025 (repoint `tax_delinquent` lead → full AREIS COLLECTION source).
All lead rows shipped **disabled**, flipped `enabled=true` only after the surface_distress dry-run
(the Summit/Wayne G1 discipline).

**Commits** (branch `lucas-toledo-s3`, local on the box — push from a write-access workstation; the
box deploy key is read-only): `7f17e91` (filing+tax) · `ec37fee` (probate) · `b2b09e0` (vacant) ·
`5e17b8a` (full AREIS tax) · `a2c632e` (forfeited land) · `80a23cb` (land bank) · plus this doc.

---

## Source decisions (the research that mattered)

**Tax — the full roll, finally.** The full ~19K delinquent universe looked locked (Column
Cloudflare-blocked; PublicNoticesOhio annual + ASP.NET; File-Downloads login-gated). The unlock:
the Auditor publishes its **entire AREIS database as a *public, no-login* ArcGIS Hub item** — the
same data behind the login. Delinquency was validated as **`TaxDue > (annual taxes)`** (accumulated
prior-year arrears) against known parcels (0701121 owes $12,953 on $15/yr → flagged; paid parcels →
not). Repointed the tax lead to it: **13 → 11,492 leads**, parity with Cuyahoga (12,550) / Shelby
(15,341).

**Probate — public source, court portals dead/walled.** The county's eAccess/CourtView portal is
**100% packet-loss unreachable**; re:SearchOH is **registration-gated AND carries only the General
Division** (not probate). The public source is **Toledo Legal News daily probate filing lists**
(`/courts/probate/`). Joined decedent name → AREIS owner.

**Land bank — owned inventory, not the broker subset.** The public Framer portal lists only ~12
broker-marketed properties; the **full owned inventory (~460 parcels)** is queryable from the
Auditor CAMA GIS by owner name — robust, and comparable to Summit's 489.

---

## Honest caveats (do not oversell)

- **Probate is a name-only join** (notices carry no address). Unique-match, person-only, badged
  `match_confidence='probable'` (never "confirmed") — a coincidental living namesake is the one
  mis-join risk. Coverage is the TLN public-notice subset (~7 recent days/run); the cron walks
  forward and grows it.
- **AREIS tax data version is 2026-06-08** (refreshes recurringly). v1 **downloads the 99MB DB each
  cron run**; the `modified`-poll + wide-freshness optimization is a documented follow-up.
- **`vacant_delinquent` (205) overlaps the full tax roll** — intentional (the "hotter" vacant
  subset; the engine's signal-stacking treats co-occurring signals as HOT).
- **Lead `property_city` is blank** on spine-sourced leads (Lucas AREIS situs format vs the shared
  city-extraction regex) — full address incl. city is in `property_address`. Cosmetic.
- **Buy-now volume ceiling is structural.** Lucas (557) trails Shelby (4,864) / Wayne (2,945)
  purely because those counties hold *thousands* of land-bank parcels; Toledo's land bank is ~460.
  Not a build gap.

---

## Fast-follows (scoped, not done)

1. **AREIS download optimization** — poll the item `modified` field, re-download only on change,
   widen the `tax_delinquent` freshness window (the wayne_blight DELTA pattern). Cuts 99MB×8/day.
2. **Fuller probate** — a registered eFileOH/re:SearchOH account + authenticated scraper would beat
   the TLN public-notice subset. Business/ToS decision (account ownership + Tyler terms).
3. **Land-bank for-sale tagging** — overlay the Framer portal's price/condition/"listed" flags onto
   the owned-inventory backbone.

## Left for the operator
- **Push the 7 commits** (`7f17e91` → `80a23cb` + this doc) from a write-access workstation for
  Jayden's GitHub review. Everything is already live + verified on prod regardless.
- Drop the throwaway/audit artifacts per the usual G2 sign-off.
