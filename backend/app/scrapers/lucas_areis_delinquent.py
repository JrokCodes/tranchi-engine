"""
Lucas County (OH / Toledo) FULL certified-delinquent tax roll — writes to tranchi.signals.

THE FULL COUNTY-WIDE DELINQUENT UNIVERSE — the source the partial TLN/vacant legs were a
stand-in for. The Lucas Auditor publishes its entire AREIS database as a PUBLIC, no-login
ArcGIS Hub item (item 8e00e957fcc04a81aac77e8bfc17b2dc) — the SAME data the login-gated
File-Downloads serves, so the login is moot. A ~99MB .zip containing AREIS_Info.mdb (MS
Access). The COLLECTION table is the tax duplicate: per-parcel charges, collections, prior
balances, special assessments. Updated recurringly (item 'modified' field; ~nightly-to-weekly).

This is the PRIMARY lucas tax_delinquent signal source (distress_lead_types row repointed to
SIGNAL_SOURCE by migration 025). lucas_delinquent_tax (TLN filed cases) + lucas_vacant_delinquent
(GIS vacant subset) remain as corroborating/HOT-stacking signals.

DELINQUENCY DEFINITION (validated 2026-06-23 against the Auditor's own GIS Vacant_Delinquent set
+ known parcels): a parcel is delinquent when **TaxDue > (Taxes1 + Taxes2)** AND TaxDue > 0 — it
owes MORE than one full year of current taxes, which can only be accumulated PRIOR-year arrears
(taxes, special assessments, penalties). TaxDue>0 alone is NOT delinquency (mid-cycle current
billing makes ~94% of parcels show TaxDue>0); the >annual test isolates true arrears.
  Validation: 0701121 (owes $12,953 on $15/yr) -> delinquent; paid parcels (TaxDue=0) -> not.
  County-wide: 29,452 delinquent; 11,744 residential (LUC 5xx) with TaxDue >= $2000 (RULE #1).

INVARIANTS:
  1. COLLECTION.Parcel is the Lucas PARID -> normalize_parcel_for_market(raw, 'lucas').
  2. payload uses delq_amount (=TaxDue) + luc (=Landuse) so the shared lucas tax RULE #1 gate
     (>= $2000 AND residential LUC 5xx) fires unchanged at surface time.
  3. observed_at = the item's 'modified' date (stable per data version) -> idempotent; re-runs
     refresh last_seen_at so leads stay live without minting duplicate rows within a version.
  4. mdbtools (mdb-export) is required on the box (apt-get install mdbtools).

PERF / FOLLOW-UP: v1 downloads + parses each run. Optimization: poll the item 'modified' field,
re-download only on change, and widen the tax_delinquent freshness window so a daily refresh
keeps leads live (the wayne_blight DELTA pattern). Left as a follow-up; correctness first.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.scrapers.base import SignalScraper
from app.scrapers.db import normalize_parcel_for_market
from app.scrapers.models import RawSignal

logger = logging.getLogger(__name__)

SITE_NAME = "Lucas Tax Delinquent (AREIS)"     # signal scrape_runs identity (distinct from the lead)
SIGNAL_SOURCE = "lucas_areis_collection"
_MARKET = "lucas"

_ITEM = "8e00e957fcc04a81aac77e8bfc17b2dc"
_META_URL = f"https://www.arcgis.com/sharing/rest/content/items/{_ITEM}?f=json"
_DATA_URL = f"https://www.arcgis.com/sharing/rest/content/items/{_ITEM}/data"
_DL_TIMEOUT = 300.0
_MDB_TIMEOUT = 300


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


class LucasAreisDelinquentScraper(SignalScraper):
    """Full Lucas certified-delinquent roll from the public AREIS COLLECTION table.

    No DB pool needed. Output flows through the signal path in run.py
    (fetch_signals -> upsert_signals; the parcel FK-stubs into tranchi.parcels).
    """

    site_name = SITE_NAME
    signal_source = SIGNAL_SOURCE

    async def fetch_signals(self) -> list[RawSignal]:
        observed_at = datetime.now(timezone.utc)
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                meta = (await c.get(_META_URL)).json()
            m = meta.get("modified")
            if m:
                observed_at = datetime.fromtimestamp(m / 1000, tz=timezone.utc)
            if meta.get("access") != "public":
                logger.warning("LucasAreisDelinquent: item access=%r (expected public)", meta.get("access"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("LucasAreisDelinquent: meta fetch failed (%s) — using now() as observed_at", exc)

        try:
            return await asyncio.to_thread(self._download_and_parse, observed_at)
        except Exception as exc:  # noqa: BLE001
            logger.error("LucasAreisDelinquent: download/parse failed: %s", exc)
            return []

    def _download_and_parse(self, observed_at: datetime) -> list[RawSignal]:
        with tempfile.TemporaryDirectory(prefix="areis_") as td:
            zip_path = Path(td) / "areis.zip"
            # ── download the ~99MB public zip ──────────────────────────────────
            with httpx.stream("GET", _DATA_URL, timeout=_DL_TIMEOUT, follow_redirects=True) as r:
                r.raise_for_status()
                with open(zip_path, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
            # ── extract AREIS_Info.mdb ─────────────────────────────────────────
            with zipfile.ZipFile(zip_path) as z:
                mdb_name = next((n for n in z.namelist() if n.lower().endswith((".mdb", ".accdb"))), None)
                if not mdb_name:
                    logger.error("LucasAreisDelinquent: no .mdb in zip (%s)", z.namelist()[:5])
                    return []
                z.extract(mdb_name, td)
            mdb_path = Path(td) / mdb_name
            # ── mdb-export COLLECTION ──────────────────────────────────────────
            proc = subprocess.run(
                ["mdb-export", str(mdb_path), "COLLECTION"],
                capture_output=True, text=True, timeout=_MDB_TIMEOUT,
            )
            if proc.returncode != 0:
                logger.error("LucasAreisDelinquent: mdb-export failed (rc=%s): %s",
                             proc.returncode, (proc.stderr or "")[:200])
                return []

            reader = csv.DictReader(io.StringIO(proc.stdout))
            cols = reader.fieldnames or []

            def col(name: str) -> str | None:
                for c in cols:
                    if c.lower() == name.lower():
                        return c
                return None

            PC, T1, T2, TD, LU, CL = (col("parcel"), col("taxes1"), col("taxes2"),
                                      col("taxdue"), col("landuse"), col("class"))
            if not (PC and TD and T1 and T2):
                logger.error("LucasAreisDelinquent: COLLECTION missing expected columns (have %s)", cols[:10])
                return []

            signals: list[RawSignal] = []
            seen: set[str] = set()
            rows = delinquent = 0
            for row in reader:
                rows += 1
                td_ = _f(row.get(TD))
                annual = _f(row.get(T1)) + _f(row.get(T2))
                # DELINQUENCY: owes more than a full year of current taxes (accumulated arrears).
                if not (td_ > 0 and td_ > annual):
                    continue
                delinquent += 1
                raw = (row.get(PC) or "").strip()
                parcel = normalize_parcel_for_market(raw, _MARKET)
                if not parcel or parcel in seen:
                    continue
                seen.add(parcel)
                signals.append(RawSignal(
                    parcel_number=parcel,
                    signal_type="tax_delinquent",
                    source=SIGNAL_SOURCE,
                    observed_at=observed_at,
                    confidence=1.0,
                    payload={
                        "delq_amount": f"{td_:.2f}",
                        "luc": (row.get(LU) or "").strip(),
                        "class": (row.get(CL) or "").strip() if CL else "",
                        "annual_taxes": f"{annual:.2f}",
                        "source": "areis_collection",
                    },
                ))

            logger.info(
                "LucasAreisDelinquent: %d delinquent of %d parcels -> %d signals (data modified %s)",
                delinquent, rows, len(signals), observed_at.date(),
            )
            return signals


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)

    async def _dry_run() -> None:
        print("\n=== Lucas AREIS full delinquent roll dry-run ===\n")
        signals = await LucasAreisDelinquentScraper().fetch_signals()
        print(f"\nTotal delinquent signals: {len(signals)}")
        # RULE #1 preview
        def gated(s):
            try:
                return float(s.payload["delq_amount"]) >= 2000 and s.payload["luc"].startswith("5")
            except (ValueError, KeyError):
                return False
        print(f"RULE #1 (>=2000 + residential 5xx): {sum(1 for s in signals if gated(s))}")
        for s in signals[:5]:
            print(json.dumps({"parcel": s.parcel_number, "payload": s.payload}, indent=2))

    asyncio.run(_dry_run())
