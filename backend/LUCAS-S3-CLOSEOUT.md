# Lucas/Toledo S3 — engine-box build close-out

**Branch:** `lucas-toledo-s3`  ·  **Worktree:** `~/lucas-build`  ·  **Engine-box session:** 2026-06-21

Pre-Lucas Phase 0/1/6 (parcel-identity fix + spine + market wiring) was already on the
branch at session start. This session built the 4 deal scrapers + produced the G2 proof
package + rehearsed migration 021. Live prod DB and code were never touched.

> **Babel doc sync:** `~/Intelleq-Library-Of-Babel` was not cloned on the engine box —
> copy this file's content into Babel's
> `Clients/Marc/scraper-playbook/markets/lucas-toledo-build/` DECISION-LOG / PROGRESS /
> SESSION-HANDOFF the next time you're on a box that has the planning repo. The truth
> is the same; the engine branch just carries it locally so it ships with the code.

---

## 1. Scraper status (this session's deliverable)

| Phase | Module | Status | Verified live | Notes |
|---|---|---|---|---|
| 0 — parcel fix | `app/scrapers/db.py::normalize_parcel_lucas` + `_for_market` | shipped (was on branch) | 13/13 unit tests PASS | situs-builder fix below |
| 0 — situs fix | `app/scrapers/lucas_parcels.py::_build_situs` + `_extract_zip` | **FIXED this session** | live AREIS row | recon doc had TAXDIST="city"; live shows TAXDIST is the 2-digit tax-district code (01/12/33). PROPERTY_ADDRESS already carries `STREET, CITY OH ZIP` — title-case it as-is. ZIP now extracted from the address tail. |
| 1 — spine | `app/scrapers/lucas_parcels.py` | shipped | 192,691 L38 / 190,088 L84 rows confirmed; **proof row PARID 1210314 = STEPHENS CORNELIUS / 3941 VERMAAS AVE / LUC 510 / APRTOT $71,800** | F-009 ...S condo form locked: 96 rows, `<8digits>S` byte form (`04349092S` etc.); current normalizer is correct. |
| 2 — RealAuction | `app/scrapers/lucas_realauction.py` | **NEW** | 42 listings on the dry-run (20 mortgage + 22 tax); 100% of mortgage rows pass the 2/3-appraisal signature; **format-lock cross-source: PARID 1210314 appears as case TF2025-00090 on Thu 06/25/2026** (spine + RealAuction loop closed in one observation) | Same RealAuction platform as Summit; Wed→mortgage, Thu→tax; PARID DD-DDDDD → 7-digit; signal_type by weekday + signature, never plaintiff. |
| 3 — Legal News | `app/scrapers/lucas_legalnews.py` | **NEW** | 5 detail rows parsed across 2 index pages (sheriff sales + foreclosure FILINGS) including the spine-locked case `CI2025-00547` at PARID `1041297` (651 Mayfair Boulevard) | Uses TLN's pre-rendered HTML cards (first 51 articles inline) — the `/search/` JSON path is bot-tier-1 throttled. Detail fetches pace at 2.5s + 6s exponential backoff for the 429s that still happen. |
| 4 — Delinquent Tax | `app/scrapers/lucas_delinquent_tax.py` | **NEW (partial)** | 15 raw / **13 GATED** (residential LUC 5xx + balance ≥ $2000) across 9 weekly TFN articles | **The 5K Column lever is DEFERRED to G3** — `ohio.column.us` is a SPA with Cloudflare + JWT auth; `/api/v1/public/notices` returns the React shell, not JSON; neither Auditor nor Treasurer publishes a consolidated PDF (the legally required publication lives ON Column). Fallback shipped: the TLN consolidated **Tax Foreclosure Notices** article (Treasurer-filed cases, ~4/wk ≈ ~200/yr, downstream of the 19K pre-distress list but upstream of RealAuction-Thu). Same payload shape (delq_amount, luc, owner, taxbill_address, property_address, case_number); LUC is backfilled from the AREIS spine in one batch query. `distress_lead_types` row stays DISABLED in `_make_lucas_market` until Column is online or Marc accepts the smaller volume. |
| 5 — Probate | `app/scrapers/lucas_probate.py` | **DEFERRED stub** | n/a — vendor is Tyler `researchoh.tylerhost.net`, NOT CourtView; AWS-ELB returns 403 to plain Chrome UA; reverse-engineering the Tyler Odyssey Portal SPA + AWS WAF challenge is out of S3 scope | One-time WARN log on first invocation. Not listed in `MARKET_SCRAPERS['lucas']['deal_and_signal']`, so the every-3h run never invokes it (zero cost). G3 plan in the file docstring. |
| 6 — wiring | `app/market_config.py` + `app/scrapers/run.py` | shipped + new entries added | `_SCRAPERS` lists `lucas_parcels / lucas_realauction / lucas_legalnews / lucas_delinquent_tax`; `MARKET_SCRAPERS['lucas'].deal_and_signal` = those three deal/signal keys; `source_meta` carries AREIS + RealAuction + TLN + TLN-Lead; `staleness_policies` `full_rescan` on all three deal sources | `parcel_normalize_fn=normalize_parcel_lucas` set; `probate_transfer_rule=None` (left unset until probate lands). |

---

## 2. Deal-type diversity (Marc decision required)

With probate deferred and no Lucas land bank, **Lucas ships with FOUR active deal types
on `lucas-toledo-s3`:**

1. `mortgage_foreclosure` — `lucas_realauction` Wednesday
2. `tax_delinquent_foreclosure` — `lucas_realauction` Thursday
3. `tax_delinquent` (lead) — `lucas_delinquent_tax` (TLN TFN partial; Column 19K deferred)
4. `foreclosure_filing` (lead) — `lucas_legalnews` `/foreclosures/` complaints

Marc's ≥5 deal-type minimum is **NOT** met. Probate is the swing 5th. Decision modes:

- **Marc signs off on 4-type Lucas:** ship as is.
- **Marc holds for 5:** G3 builds Tyler `researchoh` probate scrape; Lucas waits.
- **Hybrid:** ship 4-type now, gate Lucas behind a feature flag until probate lands.

---

## 3. G2 evidence package — `/tmp/lucas-g2/`

All scripts run from this branch's worktree against READ-ONLY targets (live prod or
a throwaway clone). Live tranchi data, code, and crons were never touched.

### 3a. Read-only on live prod (zero-behavior-change)

```
parcel_identity_proof_live.json    # parcel_identity_proof.py --out (live)
verify_listings.txt                # verify_listings --market $m --stratified 3 × 4 markets
```

- **Normalize-equivalence: 4/4 markets OK** (cuyahoga 24K / shelby 26K / summit 7K /
  wayne 46K parcel_number strings; dispatched normalize == global normalize byte-for-byte).
- **65 / 72 VALID** in stratified samples (cuyahoga 18/18, shelby 15/15, summit 17/21,
  wayne 15/18). 7 REVIEW (address-detail flags, normal — not failures), 0 STALE.
- **Pre-existing data finding:** `orphan_signals_composite = 1` on live prod —
  `parcel_number='0111111', market='cuyahoga', signal_type='code_violation',
  source='cleveland_open_data', observed_at='2020-01-02'`. Clearly a stale test fixture
  from initial development. **Operator must reconcile before applying 021 to prod**
  (delete that one signal row, OR insert a matching parcel stub). The migration's
  Step-2 preflight will RAISE on this row.

### 3b. Migration 021 rehearsal — throwaway clone

```
clone.log                         # pg_dump tranchi | psql tranchi_rehearsal
rehearsal_before.json             # parcel_identity_proof against clone (with 1 orphan)
rehearsal_before_clean.json       # after deleting the orphan — PASS
021_apply.log                     # full psql \timing trace of the migration txn
_apply_021.sql                    # the exact SQL applied (mig 021 + alembic bump in 1 txn)
rehearsal_after.json              # parcel_identity_proof --compare → IDENTICAL
```

**Clone:** `CREATE DATABASE tranchi_rehearsal` + `pg_dump tranchi --no-owner | psql
tranchi_rehearsal` — 91 sec, 811 MB on disk, all rows present (1.05M parcels / 107K
listings / 351K signals). Dropped on session exit; only `tranchi` remains.

**Migration timing (per-step, inside one transaction):**

| Step | Operation | Time |
|---|---|---|
| 1 | NULL-market guard (DO block) | 7.5 ms |
| 2 | composite-orphan guard (DO block, scan of 351K signals) | **2.62 s** |
| 3a | `ALTER tranchi.parcels SET NOT NULL` market | 0.63 s |
| 3b | `ALTER tranchi.signals SET NOT NULL` market | 0.24 s |
| 4 | DROP single-column FK | 1.2 ms |
| 5a | DROP single-column PK | 2.6 ms |
| 5b | **ADD PRIMARY KEY (parcel_number, market)** — index build over 1.05M rows | **4.18 s** |
| 6 | ADD composite FK + validate (351K signal rows) | **1.73 s** |
| | UPDATE public.alembic_version = '021' | 1.5 ms |
| | **TOTAL inside txn** | **~9.4 s** |
| | Wall-clock incl. psql RTT | 9.67 s |

**ACCESS EXCLUSIVE lock window on tranchi.parcels:** roughly steps 5b → 6 = **~6 s**.
**ACCESS EXCLUSIVE lock window on tranchi.signals:** during step 6 FK add = **~1.7 s**.
On live prod, schedule the flip off-cron (no full-run scraper should be writing
parcels/signals) so concurrent connections don't pile up behind the lock.

**After-snapshot diff vs before-snapshot:**

- `parcels_by_market` / `signals_by_market` / `active_listings_by_market` /
  `enriched_listings_by_market` / `active_listings_by_market_source` — **IDENTICAL**.
- `null_market_parcels = 0`, `null_market_signals = 0`, `orphan_signals_composite = 0`.
- Normalize-equivalence: 4/4 OK.
- PK def: `PRIMARY KEY (parcel_number, market)`.
- FK def: `FOREIGN KEY (parcel_number, market) REFERENCES tranchi.parcels(parcel_number,
  market) ON DELETE CASCADE`.

### 3c. End-to-end Lucas-write validation on rehearsed DB

```
DATABASE_URL=…tranchi_rehearsal python -m app.scrapers.run --site lucas_realauction
   Lucas Sheriff Sale (RealAuction)     42      42      42      0      0    42     0

DATABASE_URL=…tranchi_rehearsal python -m app.scrapers.run --site lucas_delinquent_tax
   Lucas Tax Delinquent (Lead)          15      15      15      0      0     0     0
```

- 42 listings + 15 signals written under the composite PK, all auto-tagged
  `market='lucas'` via `market_for_scraper`. 0 errors.
- Spine-join is 0 / 42 because we did not run the 192K AREIS sweep; that runs on
  its own weekly cron (`--site lucas_parcels`). The `_ensure_parcels_for_listings`
  post-pass also stubs missing parents inside the full 3h run.

---

## 4. Pre-flight items for the human applying 021 on live prod

1. **Reconcile the orphan** (see 3a). One-liner against prod:
   `DELETE FROM tranchi.signals WHERE parcel_number='0111111' AND market='cuyahoga'
     AND source='cleveland_open_data';`
2. Schedule a ~30 s maintenance window off-cron.
3. Apply the migration in **one transaction** with the alembic bump (template:
   `/tmp/lucas-g2/_apply_021.sql` — note `public.alembic_version`, NOT `tranchi.`).
4. Run `python scripts/parcel_identity_proof.py --compare /tmp/before.json` after
   the txn — must print `PASS — byte-for-byte clean`.
5. Schedule the weekly `lucas_parcels` spine cron (`--site lucas_parcels`).
6. Enable the `lucas_realauction` / `lucas_legalnews` / `lucas_delinquent_tax`
   sources in the 3h cron (they're already in `_SCRAPERS` and
   `MARKET_SCRAPERS['lucas']['deal_and_signal']` — just verify the cron runner picks
   them up).
7. **DO NOT** enable a `tax_delinquent` distress_lead_types row for Lucas until the
   Column 19K integration lands and is buy-now-verified.

---

## 5. Reachability + anti-bot notes (engine-box specific)

| Host | Path | Status from engine-box | Notes |
|---|---|---|---|
| `lcaudgis.co.lucas.oh.us` | AREIS MapServer | 200 OK, open | Anti-bot is IP-allowlist; engine-box IP works. |
| `lucas.sheriffsaleauction.ohio.gov` | RealAuction | 200 OK | Same Summit-style 3-step cookie session; full Chrome UA required (or 403). |
| `www.toledolegalnews.com` | TownNews flex | 200 OK on HTML index; **429 on `/search/` JSON** | `x-tncms-bot-tier: 1` rate limiter. Use the inline 51-card HTML index; pace detail at 2.5 s. |
| `ohio.column.us` | React SPA | 200 OK on shell; **`/api/*` returns SPA, not data** | Cloudflare + JWT-auth. Defer to Playwright + WAF resolution in G3. |
| `researchoh.tylerhost.net` | Tyler Odyssey Portal | **403** (awselb) | AWS-ELB WAF. Probate stays deferred. |
| `www.lucasprobate.org` | Wix marketing | 200 OK | Points to Tyler researchoh for case search. |

---

## 6. Files touched on this branch (this session)

```
backend/app/scrapers/lucas_parcels.py            (_build_situs / _extract_zip / fields)
backend/app/scrapers/lucas_realauction.py        (new — RealAuction Wed/Thu split)
backend/app/scrapers/lucas_legalnews.py          (new — TLN HTML cards, sheriff + filings)
backend/app/scrapers/lucas_delinquent_tax.py     (new — TLN TFN fallback; Column deferred)
backend/app/scrapers/lucas_probate.py            (new stub — vendor recon, G3 plan)
backend/app/scrapers/run.py                      (import + register 3 new scraper keys)
backend/app/market_config.py                     (deal_and_signal[], source_meta, staleness)
backend/LUCAS-S3-CLOSEOUT.md                     (this file)
```

Existing tests still pass (13/13 in `tests/test_normalize_parcel_lucas.py`). The
spine-builder edits are byte-for-byte safe for the 4 pre-Lucas markets — proven by
the live `parcel_identity_proof` normalize-equivalence check (above).
