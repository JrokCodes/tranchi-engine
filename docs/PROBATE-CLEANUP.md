# Probate listings — what we removed, and why (May–June 2026)

Plain-English record of the probate data-quality cleanup, so anyone (including Marc) can see
exactly what changed and trust the result. Pairs with the code: `app/scrapers/probate.py`
(INVARIANT header), `scripts/reresolve_probate.py`, `scripts/backfill_probate_decedent.py`,
and Babel `Clients/Marc/scraper-playbook/reference/JOIN-PRECISION.md`.

## What a probate listing claims
> "This person died, so their estate must sell this house." 

For it to be a real deal, the dead person has to **actually own that house**.

## What was wrong
To find a decedent's house, the old scraper searched the county property site by **last name**
and kept **every** result, scoring a match by *averaging* the name parts. Because the surname
always matched perfectly (HARRIS = HARRIS), that average cleared the bar even when the **first
name was completely different**. So one decedent got attached to *every same-surname owner in
the county*.

- Real example: case `2026EST304870` ("Darvis Harris") → **775 listings**, but Darvis owned
  ~68. The other ~707 were homes of **Stephen** Harris, **Na'Shanda** Harris, **Jeremiah** Harris,
  even a "Wolf" *foundation* for a different case — all different, living owners.
- A second failure mode: very common *full* names ("Daniel James Williams") still matched dozens
  of different "James Williams"-type owners.

These were never confirmed good deals — they were auto-generated guesses no one had checked.

## How we verify (the truth test)
We check each listing against the **county's official current-owner record** (Cuyahoga Fiscal
Office / MyPlace — the same system the tax office uses), read **live** via the county's data API.
The rule:

> If the county says a **different, living** person owns the house, the decedent doesn't — so the
> listing is removed.

Two layers, both required, now enforced in code (see `JOIN-PRECISION.md`):
1. **Full name** — the decedent's first **and** last name must match the county owner (scored on
   the weaker of the two; a shared last name alone fails).
2. **No pile-ups** — one death can't resolve to a crowd of different owners; if a name lands on
   more than a handful of distinct owners, the whole pile is rejected.
Plus the strongest signal when available: **address-anchor** — if the decedent's address (from the
court record) matches the property's address, that's a confirmed lead.

We also re-fetched the decedent's name directly from the **probate court** for ~440 legacy cases
whose name had been lost, so they could be judged instead of left in limbo.

## Proof it's correct
A live pass over **all retired rows that had a decedent name (3,074)** against the county's
current-owner API: **96.8% confirmed a different owner** (definitely correct to remove); the rest
were common-name pile-ups (also different people, removed by the no-pile-ups rule). **Zero genuine
over-removals** found. A nightly audit (`quality_audit.check_probate_join_sanity`) alerts if the
over-matching ever returns.

## Nothing is deleted
Removed rows are marked **`status='superseded'`** (not erased) — fully reversible. Rows we couldn't
verify were **hidden**, not removed, then resolved by the court re-fetch.

## The numbers
<!-- BEGIN COUNTS (final, 2026-06-01 after decedent re-fetch + re-judge) -->
Starting point: ~4,902 "active" probate listings — inflated by the surname over-match.

| Outcome | Count | Meaning |
|---|---|---|
| **Shown (verified)** | **615** | 305 address-confirmed (decedent's address == the parcel) + 316 owner-name-matched (decedent still on title — the estate hasn't transferred yet, which *is* the opportunity) |
| **Removed (superseded)** | **4,266** | County names a different/living owner, or a common-name pile-up. Reversible. |
| **Hidden / unresolved** | **0** | All 442 legacy cases were re-fetched from the court; none left in limbo |

Recovery step (Option 2): re-fetched the decedent name + address from the probate court for **442
cases** that had lost theirs — **442/442 recovered (100%)**. That promoted ~540 previously-hidden
rows into verified shown leads, taking shown-probate from ~75 → **615** and the whole active feed
from ~632 → **~1,099**. Audit (`quality_audit.check_probate_join_sanity`): **0 over-matched, 0
shown-without-decedent.**
<!-- END COUNTS -->

## The takeaway
The original 4,902 was never real — it was name-collision noise. After the fix we have a **smaller
but true** set: 615 verified probate leads, every one tied to the county's own ownership record.
That protects user (and Marc's) trust far more than an inflated count. To grow real volume, add
**more deal types and more markets** — not looser matching.
