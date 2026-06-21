# Session — Lucas/Toledo: build the 4 deal scrapers + G2 evidence (ON THE ENGINE BOX)

I'm the operator/dev on the engine box (the Lucas deal sources are anti-bot-walled, only
reachable here). Phase 0 (parcel fix), Phase 1 (spine), Phase 6 (wiring) are built + 27/27
tested on branch `lucas-toledo-s3`. This session = build the four DEAL scrapers (Phases 2-5) +
produce G2 evidence. I'm in the `~/lucas-build` worktree (NOT the live checkout). Jayden said
"do everything, just cook" — I cook the build; live DB + deploy are gated.

## HARD SAFETY RULES — read twice
1. I'm in `~/lucas-build` (separate worktree). Do NOT touch `/home/ubuntu/tranchi-engine` (the
   crons run from there; switching it to this branch would run composite-key/Lucas code on the
   un-migrated DB and corrupt it).
2. Do NOT write Lucas data to the live prod tranchi DB. It lacks migration 021, so any Lucas
   write collides with Summit's namespace (proven 8,529 collisions) and corrupts prod. Scrapers
   run DRY-RUN vs live sources only.
3. Real end-to-end DB tests go against a THROWAWAY restored copy of prod with 021 applied
   (= the migration rehearsal).
4. Migration 021 on live prod = a human, AFTER Jayden's G2. Not this session.
5. Read every line. Anti-bot: pace, real browser UA, real sessions, backoff.

## RULE #1 (Jayden)
Every listing must be a valid investor lead — RESIDENTIAL/investable (land-use code), GENUINELY
distressed, ACTIONABLE/off-market. Price filter LOOSE, deal-validity gate TIGHT. Hardest on the
delinquent slice -> gate to residential + real balance; report the GATED count.

## Where things live
- Engine code + this branch: `~/lucas-build` (worktree of `tranchi-engine` on `lucas-toledo-s3`).
- Phase-0 artifacts already on the branch: `alembic/versions/021_parcels_market_pk.py`,
  `app/scrapers/db.py` (`normalize_parcel_lucas` + `normalize_parcel_for_market`),
  `app/market_config.py` (`_make_lucas_market` + `parcel_normalize_fn` per market),
  `app/scrapers/lucas_parcels.py` (spine), `scripts/parcel_identity_proof.py`,
  `tests/test_normalize_parcel_lucas.py`. `MARKET_SCRAPERS['lucas'].deal_and_signal` is `[]` —
  add each scraper as it lands.
- Planning docs (DECISION-LOG / PROGRESS / SESSION-HANDOFF / PARCEL-IDENTITY-FIX-PLAN / recon)
  are in a SEPARATE repo: `~/Intelleq-Library-Of-Babel` ->
  `Clients/Marc/scraper-playbook/markets/lucas-toledo-build/`. Clone/pull it; the close-out doc
  updates commit there, not in the engine repo.

## First-checks (before building)
- Dry-run `python -m app.scrapers.lucas_parcels` vs live AREIS -> confirm field names
  (PARID / OWNER / PROPERTY_ADDRESS / LUC on L38; APRTOT on L84). The field-map is
  recon-documented, NOT live-probed — fix `_OUT_FIELDS` / `_attrs_to_parcel` if live names differ.
- Trace one `…S` condo parcel across AREIS + a deal source -> lock its canonical byte form
  (F-009 self-join risk; `normalize_parcel_lucas` currently keeps `<8digits>S`).

## Build the 4 deal scrapers (mirror the Summit modules)
Each: fetch -> parse -> classify -> validity-gate (VALID-WHEN / KILL / GUARD) -> dedup by PARID
-> dry-run output. Add each to `MARKET_SCRAPERS['lucas'].deal_and_signal` + `run.py` `_SCRAPERS`
+ its `source_meta`/`staleness_policies` in `_make_lucas_market` as it lands.

2. `lucas_realauction.py` — Wed mortgage + Thu tax; classify by WEEKDAY + per-item SIGNATURE,
   never plaintiff (tax = non-appraised / taxes+costs / 10%-of-bid deposit; mortgage =
   2/3-appraisal). Reuse the Summit retHTML 3-step cookie session. Cross-check each row vs the
   spine (PARID) before active. Site `lucas.sheriffsaleauction.ohio.gov`; DD-DDDDD -> 7-pad PARID;
   non-PARID rows -> `source_listing_id=None`.
3. `lucas_legalnews.py` — Toledo Legal News sheriff sales + foreclosure filings; parcel
   `DD-DDDDD` = PARID; use the working CMS path
   (`legal_notices/foreclosure_sherrif_sales_lucas/`), pace the 429 legacy path; derive
   signal_type by sale weekday (don't hardcode); dedup vs RealAuction by PARID/case; add the
   foreclosure-FILING page as the pre-distress signal.
4. `lucas_delinquent_tax.py` — THE 5K LEVER. Recon Column's mechanics first
   (`ohio.column.us` — API / HTML / SPA / `__NEXT_DATA__` / anti-bot). Build if tractable; if
   brutal, fall back to the annual statutory delinquent-list PDF (annual refresh is fine) — do
   NOT drop to ~2k. Gate to investable residential + real balance. Emit payload keys
   `delq_amount` + `luc` (the `_make_lucas_market` gate already references them). Ship the
   `distress_lead_types` row DISABLED until buy-now verified. Report the GATED count.
5. `lucas_probate.py` — TIME-BOXED, do LAST. Confirm the vendor (CourtView? ->
   `summit_probate.py` is the template); residential session for the anti-bot wall; build ONLY if
   cheap-CourtView-opens, else DEFER with a strong log. Fill `probate_transfer_rule` if it lands.

## G2 evidence
- Read-only on LIVE prod (safe): `python scripts/parcel_identity_proof.py --out /tmp/before.json`
  + `for m in cuyahoga shelby summit wayne; do python scripts/verify_listings.py --market $m
  --stratified 3; done` -> the zero-behavior-change package.
- Migration 021 rehearsal on the THROWAWAY restored copy: `parcel_identity_proof.py --out
  /tmp/before.json` BEFORE applying 021 -> apply 021 (one txn + bump alembic_version) ->
  `parcel_identity_proof.py --compare /tmp/before.json` (asserts per-market counts identical +
  0 NULL/orphan + normalize-equivalence; exit 0 = clean). Confirm clean run + capture the
  lock/timing window. Then run the Lucas scrapers against that rehearsed DB for end-to-end write
  validation.

## Close out
- Update DECISION-LOG / PROGRESS / SESSION-HANDOFF (in `~/Intelleq-Library-Of-Babel`): scraper
  status, GATED delinquent count, probate verdict, migration-rehearsal result + lock window, G2
  evidence location, and the DEAL-TYPE DIVERSITY flag (land bank NO-GO + forfeited deferred +
  probate maybe-deferred -> Lucas may be under Marc's >=5 deal types -> Marc decision; on this
  branch Lucas has mortgage_foreclosure + tax_delinquent_foreclosure + tax_delinquent(lead) +
  foreclosure_filing(lead) = 4, with probate the swing 5th).
- Commit to `lucas-toledo-s3` (NOT main); push the branch. (Engine docs in the engine repo;
  planning docs in the Babel repo.)
- Hand to Jayden: G2 package ready -> his sign-off -> human applies 021 -> merge to main ->
  deploy -> Lucas live -> G3.

## Cleanup when done
From `/home/ubuntu/tranchi-engine`: `git worktree remove ~/lucas-build` so it doesn't linger.
