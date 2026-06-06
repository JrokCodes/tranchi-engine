# Pre-Distress Surfacing — Before / After (for Marc)

**TL;DR:** We turned ~22,000 distress *signals* we were already collecting into filterable
**Pre-Distress** leads, without touching the clean Buy Now feed. You can flip any type off in one
line if you don't want it. Verified live 2026-06-06.

## Before vs After

| | Before | After |
|---|---|---|
| **Buy Now** (default view) | 4,191 active | **4,191 active — unchanged** |
| **Pre-Distress** (new toggle) | hidden | **22,081 active leads** |
| **Total Memphis inventory** | ~4,191 | **~26,272** |

The default dashboard view is **identical to before** — Buy Now only. The 22k pre-distress leads sit
behind the **Pre-Distress** toggle (top-left), so they never clutter the buyable feed; users opt in.

## What the 22k are
| Type | Count | Why it's a valid off-market lead |
|---|---|---|
| **Tax Delinquent (Lawsuit)** | 15,999 | Owner is being sued for unpaid taxes (open lien >12mo). Hard financial distress, pre-auction. The "$20k owed on a $100k house" lead. |
| **Eviction (Tired Landlord)** | 6,082 | Landlord clearing a problem tenant → motivated seller. Slightly weaker off-market than tax-distress. |

Every lead is a real Shelby parcel pulled from the county registry — **owner name + address come
straight from the authoritative record** (no guessing, no fuzzy name-matching).

## How we keep them valid (same standard as Buy Now)
- **Off-market:** a lead is auto-retired the moment its parcel **sells/transfers** (the same guard
  that keeps Buy Now ~off-market). Plus the per-listing **Zillow/Redfin** buttons for the
  is-it-actively-listed eyeball check.
- **Fresh:** re-derived every 3h from the live distress lists; a parcel that drops off (taxes paid /
  redeemed) is retired automatically.
- **No double-counting:** if a parcel is *already* a Buy Now deal, it stays a Buy Now deal (the
  distress just makes it HOT) — it is not duplicated as a lead.

## The kill switch (your control)
Each type is independently toggleable. If you decide a type isn't worth showing:
```sql
-- turn a distress type OFF (next 3h cycle retires all its leads; reversible)
UPDATE tranchi.distress_lead_types SET enabled = false WHERE signal_type = 'eviction';
-- back ON
UPDATE tranchi.distress_lead_types SET enabled = true  WHERE signal_type = 'eviction';
```
(We'll do this for you — just say which types to keep after you've reviewed `SOURCE-CATALOG.md`.)

## What we need from you
Review the sources in `SOURCE-CATALOG.md` (the Verify buttons make it one click each), then tell us:
1. Which **Buy Now** sources look good.
2. Which **Pre-Distress** types to keep (both? tax-delinquent only? neither?).
3. Whether the off-market quality holds on the samples you check.
