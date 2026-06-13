"""
Database upsert logic for scraped listings.

For each RawListing:
  - Upserts into tranchi.listings with conflict resolution on
    (source_site, case_number) when available, or
    (source_site, property_address, sale_date) as fallback.
  - On conflict: updates last_seen_at, deposit_usd, status.
  - Logs scrape run to tranchi.scrape_runs with the full Sources-dashboard
    stat shape (found/passed/active/filtered/dupes/delisted/expired/new_today).

Returns ScrapeResult with counts.

INVARIANT: normalized_address is computed by normalize_address(canonical_address(raw))
and must never be stored as NULL for a row with a non-NULL property_address.
The dedup query in _dedup_cross_source_listings partitions on normalized_address;
NULL rows are silently skipped and never deduplicated.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from app.scrapers.models import RawListing, ScrapeResult
from app.scrapers._time import today_et

logger = logging.getLogger(__name__)


_SUFFIX_MAP: dict[str, str] = {
    "street": "st", "st.": "st",
    "avenue": "ave", "ave.": "ave",
    "drive": "dr", "dr.": "dr",
    "road": "rd", "rd.": "rd",
    "lane": "ln", "ln.": "ln",
    "court": "ct", "ct.": "ct",
    "place": "pl", "pl.": "pl",
    "boulevard": "blvd", "blvd.": "blvd",
    "circle": "cir", "cir.": "cir",
    "way": "way",
}
_SUFFIX_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _SUFFIX_MAP) + r')\b'
)
_NON_ALNUM_PATTERN = re.compile(r'[^a-z0-9 ]')
_MULTI_SPACE_PATTERN = re.compile(r'\s+')

_COUNTY_SUFFIX_PATTERN = re.compile(r'\s+county\s*$', re.IGNORECASE)

# OH city names that scrapers sometimes leak into property_address. When the
# normalized address ends with one of these (with no following street suffix),
# we strip it so dedup matches "1234 elm" against "1234 elm cleveland".
# Keep lowercase; matched as trailing whole-word tokens during normalize_address.
_OH_CITY_TRAILING_TOKENS = {
    "cleveland", "lakewood", "parma", "shaker heights", "euclid",
    "garfield heights", "maple heights", "westlake", "north olmsted",
    "brooklyn", "berea", "bedford", "bedford heights", "brecksville",
    "broadview heights", "independence", "mayfield heights",
    "middleburg heights", "olmsted falls", "parma heights", "pepper pike",
    "richmond heights", "rocky river", "seven hills", "solon", "south euclid",
    "university heights", "warrensville heights", "highland heights",
    "mayfield village", "newburgh heights", "north randall", "north royalton",
    "beachwood", "brook park", "chagrin falls", "cuyahoga heights",
    "fairview park", "gates mills", "glenwillow", "hunting valley",
    "linndale", "moreland hills", "oakwood village", "orange village",
    "valley view", "walton hills", "woodmere", "east cleveland",
    "cleveland heights", "strongsville", "olmsted twp", "oh",
}
_TRAILING_STATE_ZIP = re.compile(r'\s+oh\s*\d{5}(-\d{4})?\s*$', re.IGNORECASE)


def canonical_county(county: str | None) -> str | None:
    """Canonicalize a county name for storage.

    Strips trailing ' County', title-cases. All sources land on one shape.
    Frontend filter dropdowns may display the friendly 'Cuyahoga County' label —
    backend strips on query.
    """
    if county is None:
        return None
    c = html.unescape(county).strip()
    c = _COUNTY_SUFFIX_PATTERN.sub('', c)
    if not c:
        return None
    return ' '.join(w.capitalize() for w in c.lower().split())


def canonical_city(city: str | None) -> str | None:
    """Canonicalize a city name for storage.

    Title-cases and trims so scrapers writing 'CLEVELAND' / ' Cleveland '
    all land as 'Cleveland'. Without this the city filter dropdown sees
    duplicate entries and ILIKE filters miss rows. Mirrors canonical_county.
    """
    if city is None:
        return None
    c = html.unescape(city).strip()
    if not c:
        return None
    return ' '.join(w.capitalize() for w in c.lower().split())


# USPS street-type abbreviations (canonical form is the value).
_STREET_TYPE_MAP: dict[str, str] = {
    "road": "Rd", "rd": "Rd", "rd.": "Rd",
    "avenue": "Ave", "ave": "Ave", "ave.": "Ave",
    "drive": "Dr", "dr": "Dr", "dr.": "Dr",
    "street": "St", "st": "St", "st.": "St",
    "court": "Ct", "ct": "Ct", "ct.": "Ct",
    "lane": "Ln", "ln": "Ln", "ln.": "Ln",
    "boulevard": "Blvd", "blvd": "Blvd", "blvd.": "Blvd",
    "circle": "Cir", "cir": "Cir", "cir.": "Cir",
    "place": "Pl", "pl": "Pl", "pl.": "Pl",
    "terrace": "Ter", "ter": "Ter", "ter.": "Ter",
    "parkway": "Pkwy", "pkwy": "Pkwy", "pkwy.": "Pkwy",
    "highway": "Hwy", "hwy": "Hwy", "hwy.": "Hwy",
    "trail": "Trl", "trl": "Trl", "trl.": "Trl",
    "square": "Sq", "sq": "Sq", "sq.": "Sq",
    "way": "Way",
    "loop": "Loop",
    "turn": "Turn",
}

_DIRECTIONAL_MAP: dict[str, str] = {
    "north": "N", "n": "N", "n.": "N",
    "south": "S", "s": "S", "s.": "S",
    "east": "E", "e": "E", "e.": "E",
    "west": "W", "w": "W", "w.": "W",
    "northeast": "NE", "ne": "NE",
    "northwest": "NW", "nw": "NW",
    "southeast": "SE", "se": "SE",
    "southwest": "SW", "sw": "SW",
}

_UNIT_DESIGNATORS = frozenset({"apt", "apartment", "unit", "suite", "ste", "bldg", "building", "#"})

_SINGLE_DIRECTIONALS = frozenset({"n", "s", "e", "w", "ne", "nw", "se", "sw", "n.", "s.", "e.", "w."})


def canonical_address(address: str | None) -> str | None:
    """Canonicalize a street address for storage.

    Normalizes street-type tokens to USPS standard abbreviations and
    directionals to single-letter forms so '108 CEDAR DRIVE' and
    '108 Cedar Dr' both land as '108 Cedar Dr'. Title-case applied
    throughout. Idempotent.
    """
    if address is None:
        return None
    a = html.unescape(address).strip()
    a = re.sub(r'\s+', ' ', a)
    if not a:
        return None

    tokens = a.split()

    unit_start: int | None = None
    for i, tok in enumerate(tokens):
        if tok.lower() in _UNIT_DESIGNATORS or tok.startswith('#'):
            unit_start = i
            break

    body_tokens = tokens[:unit_start] if unit_start is not None else tokens
    unit_tokens = tokens[unit_start:] if unit_start is not None else []

    body_tokens = [t.capitalize() for t in body_tokens]

    for i, tok in enumerate(body_tokens):
        key = tok.lower().rstrip('.')
        if key in _STREET_TYPE_MAP:
            body_tokens[i] = _STREET_TYPE_MAP[key]

    street_type_values = frozenset(_STREET_TYPE_MAP.values())
    i = 1
    while i < len(body_tokens):
        tok = body_tokens[i]
        key = tok.lower().rstrip('.')
        if key not in _DIRECTIONAL_MAP or tok in street_type_values:
            break
        if i > 1 and key not in _SINGLE_DIRECTIONALS:
            break
        body_tokens[i] = _DIRECTIONAL_MAP[key]
        i += 1

    result_tokens = body_tokens + unit_tokens
    return ' '.join(result_tokens)


def normalize_address(addr: str) -> str:
    """Normalize a street address for cross-source deduplication.

    Final step strips trailing OH city / state / zip tokens that some scrapers
    leak into property_address. Without this, the same property normalized as
    "1234 elm" vs "1234 elm cleveland" will not dedup.
    """
    addr = addr.lower().strip()
    addr = _TRAILING_STATE_ZIP.sub('', addr)
    addr = _SUFFIX_PATTERN.sub(lambda m: _SUFFIX_MAP.get(m.group().lower(), m.group()), addr)
    addr = _NON_ALNUM_PATTERN.sub('', addr)
    addr = _MULTI_SPACE_PATTERN.sub(' ', addr).strip()
    tokens = addr.split()
    for _ in range(2):
        if len(tokens) <= 2:
            break
        if len(tokens) >= 4:
            two = f"{tokens[-2]} {tokens[-1]}"
            if two in _OH_CITY_TRAILING_TOKENS:
                tokens = tokens[:-2]
                continue
        if tokens[-1] in _OH_CITY_TRAILING_TOKENS:
            tokens = tokens[:-1]
            continue
        break
    return ' '.join(tokens)


def normalize_parcel_number(raw: str | None) -> str | None:
    """Normalize a parcel number to a single canonical form per county/state.

    CUYAHOGA (OH) — canonical form: DDD-NN-NNN (e.g. '110-19-068')
      Two formats in active use:
        Display: '110-19-068'  (Sheriff, Fiscal Officer, Land Bank)
        Compact: '11019068'    (Cleveland Open Data / code violations)
      Detection: purely numeric after stripping non-digits, 8 digits → Cuyahoga.

    SUMMIT COUNTY (OH / Akron) — canonical form: 7-digit zero-padded numeric
      string (e.g. '6700526', '0101379', '2400024'). Leading zeros are load-bearing.
      Fiscal-office DISPLAY inserts a dash after digit 2 ('67-08383' on RealAuction /
      Akron Legal News); the dash is stripped. Distinct from Cuyahoga (always 8 digits)
      and Shelby (>= 10 digits, or alpha / whitespace). Detection: no letters + <= 7 digits.

    SHELBY COUNTY (TN / Memphis) — canonical form: 14-char alphanumeric
      (e.g. '07204700000160').

      Three source formats all normalize to the same 14-char form:
        ReGIS PARCELID / PARID (spaced):  '072047  00016'
          → MAP segment + whitespace + GROUP segment
        ReGIS PAID (compact, numeric-only): '07204700016' (10–11 digits: MAP6 + GROUP)
        Tax Sale CSV Alt_Parcel / ePropertyPlus parcelNumber: '07204700000160' (14 chars)

      Canonical construction (verified against Tax CSV Alt_Parcel + ePropertyPlus
      parcelNumber as authoritative sources, 17 real-parcel cases including all
      alpha-MAP and alpha-GROUP variants from live ReGIS + Tax Sale data):

          canonical = MAP.ljust(6,'0') + '0' + grp_numeric.zfill(6) + qualifier

        where:
          MAP         = the prefix segment before the whitespace gap, right-padded
                        to exactly 6 chars with '0' (ljust). Numeric MAPs like
                        '072047' are already 6; short alpha-MAPs like 'D0217' (5)
                        become 'D02170'. DO NOT left-pad alpha MAPs with '0' — that
                        produces '0D0217...' which does not match any authoritative
                        source and breaks FK joins.
          '0'         = fixed structural separator at position [6].
          grp_numeric = GROUP characters with any trailing alpha qualifier stripped.
                        E.g. group '00514C' → grp_numeric='00514', qualifier='C'.
                        Group 'A00640' (leading alpha) → grp_numeric='A00640', qualifier='0'.
          qualifier   = trailing alpha char of GROUP if present (e.g. 'C' in '00514C'),
                        else '0' (whole-parcel qualifier).
          zfill(6)    = left-zero-pad grp_numeric to 6 chars.

      Real traced examples (Tax CSV Alt_Parcel is authoritative):
        '072047  00016' → '07204700000160'  (numeric MAP+GROUP, qualifier='0')
        '074037 H00081' → '0740370H000810'  (alpha-GROUP prefix, qualifier='0')
        'D0217   00225' → 'D0217000002250'  (alpha-MAP 5-char, right-padded → 'D02170')
        'C0244  A00640' → 'C024400A006400'  (alpha-MAP 5-char + alpha-GROUP)
        'D0222   00514C'→ 'D022200000514C'  (alpha-MAP 5-char + trailing qualifier 'C')

      INVARIANT: every Shelby source (tax-sale, land-bank, MMLBA) MUST call
      normalize_parcel_number() before using any parcel ID as an FK into
      tranchi.parcels or tranchi.signals. The same silent join-failure that bit
      Cuyahoga (fiscal_officer.py) will bite Shelby if you skip this. Downstream
      builders: use Tax CSV Alt_Parcel or ePropertyPlus parcelNumber (both already
      14-char canonical) in preference to the spaced PARCELID or compact PAID.
      Alpha-MAP PAID compact forms (e.g. 'D021700225') cannot be reconstructed
      to 14-char without the spaced form — they fall through to the unchanged
      return; if you only have PAID for an alpha-MAP parcel, use the spaced
      PARCELID from ReGIS or the Alt_Parcel from the tax CSV instead.

    Detection logic (ORDER MATTERS — most specific first):
      - Matches DDD-NN-NNN exactly → Cuyahoga (idempotent).
      - Purely numeric after strip, 8 digits → Cuyahoga compact.
      - No letters + <= 7 digits after strip → Summit (zero-pad to 7).
      - Exactly 14 alphanumeric chars, no spaces → already canonical (idempotent).
      - Contains internal whitespace (two non-space groups) → Shelby spaced form.
      - Purely numeric, 10–11 digits → Shelby PAID compact (numeric-MAP only).
      - Anything else → return uppercased as-is (don't crash on edge cases).
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None

    # ── Cuyahoga (OH): DDD-NN-NNN ─────────────────────────────────────────────
    if re.match(r"^\d{3}-\d{2}-\d{3}$", stripped):
        return stripped
    digits_only = re.sub(r"\D", "", stripped)
    if len(digits_only) == 8:
        return f"{digits_only[:3]}-{digits_only[3:5]}-{digits_only[5:]}"

    # ── Summit County (OH / Akron): 7-digit zero-padded numeric ──────────────
    # Single canonical form: 7-digit zero-padded STRING (e.g. '6700526', '0101379',
    # '2400024'). LEADING ZEROS ARE LOAD-BEARING — string-compare only; an int-cast
    # ('0101379' -> 101379) breaks the spine join. The Fiscal Office DISPLAY form
    # inserts a dash after digit 2 ('67-08383' on RealAuction/Akron Legal News);
    # the non-digit strip above already removed it, so '67-08383' -> '6708383'.
    # DETECTION is unambiguous within the live market set: a Summit parcel has NO
    # letters and <= 7 digits, whereas Cuyahoga (OH) is always exactly 8 digits
    # (handled above) and every Shelby (TN) form is either >= 10 digits (PAID 10-11,
    # 14-char canonical) or contains letters / internal whitespace (handled below).
    # zfill(7) is defensive for any source that drops a leading zero ('101379').
    if not re.search(r"[A-Za-z]", stripped) and 1 <= len(digits_only) <= 7:
        return digits_only.zfill(7)

    # ── Shelby County (TN): 14-char canonical ────────────────────────────────
    # Already canonical: exactly 14 alphanumeric chars, no spaces or hyphens.
    if re.match(r"^[A-Z0-9]{14}$", stripped, re.IGNORECASE):
        return stripped.upper()

    # Spaced form: MAP_segment + whitespace + GROUP_segment
    # MAP is 4–7 chars (numeric e.g. '072047', or alpha e.g. 'D0217', 'M0115H').
    # GROUP is 5–7 chars (numeric e.g. '00016', alpha-prefix e.g. 'H00081',
    # trailing-qualifier e.g. '00514C', or BOTH e.g. 'H00043C' → '0740370H00043C').
    spaced_m = re.match(r"^([A-Z0-9]{4,7})\s+([A-Z0-9]{5,7})$", stripped, re.IGNORECASE)
    if spaced_m:
        map_part = spaced_m.group(1).upper()
        grp_part = spaced_m.group(2).upper()

        # MAP: RIGHT-pad to 6 chars with '0' (ljust).
        # Numeric '072047' is already 6; short alpha 'D0217' → 'D02170'.
        # NEVER left-pad alpha MAPs — '0D0217' is wrong and breaks FK joins.
        map_padded = map_part.ljust(6, "0")[:6]

        # GROUP: if the last char is alpha it is the sub-parcel qualifier (e.g. 'C');
        # strip it and zero-pad the numeric portion. Otherwise qualifier = '0'.
        if grp_part and grp_part[-1].isalpha():
            qualifier = grp_part[-1]
            grp_numeric = grp_part[:-1]
        else:
            qualifier = "0"
            grp_numeric = grp_part

        grp_padded = grp_numeric.zfill(6)
        return f"{map_padded}0{grp_padded}{qualifier}"

    # PAID compact: purely numeric, 10–11 digits (numeric MAP6 + GROUP4or5).
    # Alpha-MAP PAID forms (e.g. 'D021700225') are not purely numeric, so they
    # fall through to the unchanged return below — cannot reconstruct without
    # knowing which chars belong to MAP vs GROUP in an alpha-MAP compact string.
    if re.match(r"^\d{10,11}$", stripped):
        map_part = stripped[:6]
        grp_part = stripped[6:]
        grp_padded = grp_part.zfill(6)
        return f"{map_part}0{grp_padded}0"

    # Can't normalize — return uppercased input unchanged (don't crash).
    return stripped.upper()


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


async def upsert_listings(
    pool: asyncpg.Pool,
    listings: list[RawListing],
    source_site: str,
    *,
    market: str,
    found_raw: int | None = None,
    filtered_count: int = 0,
    dry_run: bool = False,
) -> ScrapeResult:
    """Upsert all listings for a single source_site scrape run.

    Args:
        pool: asyncpg connection pool.
        listings: Prefiltered RawListing objects (hard filters already applied).
        source_site: Name of the scraping source.
        found_raw: Total raw listings from source before prefilter.
        filtered_count: Count rejected by prefilter (for scrape_runs.filtered).
        dry_run: If True, parse + validate but skip all DB writes.

    Returns:
        ScrapeResult with counts.
    """
    started_at = _utcnow()
    result = ScrapeResult(source_site=source_site)
    result.found = found_raw if found_raw is not None else len(listings)
    result.passed = len(listings)
    result.filtered = filtered_count

    if dry_run:
        logger.info("[DRY RUN] %s: would process %d listings", source_site, len(listings))
        return result

    run_id: UUID | None = None
    _today = today_et()

    async with pool.acquire() as conn:
        # Open scrape run record
        try:
            run_id = await conn.fetchval(
                """
                INSERT INTO tranchi.scrape_runs
                    (source_site, started_at, status)
                VALUES ($1, $2, 'running')
                RETURNING id
                """,
                source_site,
                started_at,
            )
        except Exception as exc:
            logger.error("Failed to create scrape_run for %r: %s", source_site, exc)
            result.errors += 1
            result.error_message = str(exc)
            return result

        # Upsert each listing
        for listing in listings:
            try:
                listing_id, is_new = await _upsert_one(conn, listing, market)
                if is_new:
                    result.new_inserted += 1
                    result.new_today += 1
                else:
                    result.updated += 1
            except Exception as exc:
                logger.error(
                    "Error upserting listing %r from %r: %s",
                    listing.property_address,
                    source_site,
                    exc,
                )
                result.errors += 1

        # Compute active + new_today from DB for accuracy
        try:
            result.active = await conn.fetchval(
                """
                SELECT COUNT(*) FROM tranchi.listings
                WHERE source_site = $1
                  AND status IN ('active', 'not_listed')
                  AND duplicate_of IS NULL
                """,
                source_site,
            ) or 0
            result.new_today = await conn.fetchval(
                """
                SELECT COUNT(*) FROM tranchi.listings
                WHERE source_site = $1
                  AND first_seen_at::date = $2
                """,
                source_site,
                _today,
            ) or 0
        except Exception as exc:
            logger.warning("Failed to compute active/new_today counts: %s", exc)

        # Close scrape run record
        completed_at = _utcnow()
        final_status = "error" if result.errors > 0 and result.new_inserted == 0 else "success"
        try:
            await conn.execute(
                """
                UPDATE tranchi.scrape_runs
                SET completed_at  = $1,
                    status        = $2,
                    found         = $3,
                    passed        = $4,
                    active        = $5,
                    filtered      = $6,
                    new_today     = $7,
                    error_message = $8
                WHERE id = $9
                """,
                completed_at,
                final_status,
                result.found,
                result.passed,
                result.active,
                result.filtered,
                result.new_today,
                result.error_message,
                run_id,
            )
        except Exception as exc:
            logger.error("Failed to finalize scrape_run %s: %s", run_id, exc)

    return result


async def _upsert_one(
    conn: asyncpg.Connection,
    listing: RawListing,
    market: str,
) -> tuple[UUID, bool]:
    """Insert or update a single listing row.

    Deduplication strategy:
      - Primary: (source_site, case_number) when case_number is set
      - Fallback: (source_site, property_address, sale_date)

    Returns:
        (listing_id, is_new) — is_new=True if a row was actually inserted.
    """
    canon_addr = canonical_address(listing.property_address)
    norm_addr = normalize_address(canon_addr) if canon_addr else None
    canon_county = canonical_county(listing.property_county)
    canon_city = canonical_city(listing.property_city)
    norm_parcel = normalize_parcel_number(listing.source_listing_id)

    existing_id: UUID | None = None

    if listing.case_number and norm_parcel:
        # Composite key (source, case_number, parcel). Required for probate:
        # one Estate case legitimately yields MANY property listings (a decedent
        # who owned multiple parcels). A case-only key would collapse them to one.
        # Single-parcel sources (sheriff, land_bank) are unaffected — (case,parcel)
        # is still unique per listing for them.
        existing_id = await conn.fetchval(
            """
            SELECT id FROM tranchi.listings
            WHERE source_site = $1 AND case_number = $2 AND source_listing_id = $3
            LIMIT 1
            """,
            listing.source_site,
            listing.case_number,
            norm_parcel,
        )
    elif listing.case_number:
        existing_id = await conn.fetchval(
            """
            SELECT id FROM tranchi.listings
            WHERE source_site = $1 AND case_number = $2
            LIMIT 1
            """,
            listing.source_site,
            listing.case_number,
        )
    elif norm_parcel:
        # Parcel-primary dedup (no case_number): match by (source_site, parcel).
        # Prevents transient duplicates when the same parcel is re-scraped across
        # cycles for sources that don't use case numbers (tax-sale, land-bank, MMLBA).
        # The cross-source dedup in run.py already uses parcel as cluster key;
        # this keeps the per-source row count from inflating on every scrape.
        # Only applies when norm_parcel is set — address+sale_date fallback still
        # fires below for sources that set neither case_number nor source_listing_id.
        existing_id = await conn.fetchval(
            """
            SELECT id FROM tranchi.listings
            WHERE source_site = $1 AND source_listing_id = $2
            LIMIT 1
            """,
            listing.source_site,
            norm_parcel,
        )
    else:
        existing_id = await conn.fetchval(
            """
            SELECT id FROM tranchi.listings
            WHERE source_site = $1
              AND property_address = $2
              AND sale_date IS NOT DISTINCT FROM $3
            LIMIT 1
            """,
            listing.source_site,
            canon_addr,
            listing.sale_date,
        )

    if existing_id is not None:
        # Address fields refreshed on every scrape — source is authoritative.
        # NULL coming from scraper does NOT clobber existing non-NULL values.
        await conn.execute(
            """
            UPDATE tranchi.listings
            SET last_seen_at         = NOW(),
                deposit_usd         = COALESCE($1, deposit_usd),
                status              = $2,
                normalized_address  = COALESCE($3, normalized_address),
                property_county     = COALESCE($4, property_county),
                property_address    = COALESCE($5, property_address),
                property_city       = COALESCE($6, property_city),
                property_zip        = COALESCE($7, property_zip),
                sale_date           = COALESCE($8, sale_date),
                signal_type         = COALESCE($9, signal_type),
                source_listing_id   = COALESCE($10, source_listing_id),
                auction_status      = COALESCE($11, auction_status),
                opening_bid_usd     = COALESCE($12, opening_bid_usd),
                appraised_value_usd = COALESCE($13, appraised_value_usd),
                sec_sale_date       = COALESCE($14, sec_sale_date),
                case_status         = COALESCE($16, case_status),
                case_status_date    = COALESCE($17, case_status_date),
                match_method        = COALESCE($18, match_method),
                match_confidence    = COALESCE($19, match_confidence),
                match_score         = COALESCE($20, match_score),
                decedent_name       = COALESCE($21, decedent_name),
                case_title          = COALESCE($22, case_title),
                decedent_dod        = COALESCE($23, decedent_dod),
                probate_internal_id = COALESCE($24, probate_internal_id),
                market              = $25,
                filing_date         = COALESCE($26, filing_date)
            WHERE id = $15
            """,
            listing.deposit_usd,
            listing.status,
            norm_addr,
            canon_county,
            canon_addr,
            canon_city,
            listing.property_zip,
            listing.sale_date,
            listing.signal_type,
            norm_parcel,
            listing.auction_status,
            listing.opening_bid_usd,
            listing.appraised_value_usd,
            listing.sec_sale_date,
            existing_id,
            listing.case_status,
            listing.case_status_date,
            listing.match_method,
            listing.match_confidence,
            listing.match_score,
            listing.decedent_name,
            listing.case_title,
            listing.decedent_dod,
            listing.probate_internal_id,
            market,
            listing.filing_date,
        )
        return existing_id, False

    new_id: UUID = await conn.fetchval(
        """
        INSERT INTO tranchi.listings (
            source_site, case_number, property_address,
            property_city, property_county, property_state,
            property_zip, sale_date, sale_time, sale_location,
            deposit_usd, trustee_name, status, normalized_address,
            signal_type, source_listing_id,
            auction_status, opening_bid_usd, appraised_value_usd, sec_sale_date,
            case_status, case_status_date, match_method, match_confidence, match_score,
            decedent_name, case_title, decedent_dod, probate_internal_id, market,
            filing_date
        ) VALUES (
            $1,  $2,  $3,
            $4,  $5,  $6,
            $7,  $8,  $9,  $10,
            $11, $12, $13, $14,
            $15, $16,
            $17, $18, $19, $20,
            $21, $22, $23, $24, $25,
            $26, $27, $28, $29, $30,
            $31
        )
        RETURNING id
        """,
        listing.source_site,
        listing.case_number,
        canon_addr,
        canon_city,
        canon_county,
        listing.property_state,
        listing.property_zip,
        listing.sale_date,
        listing.sale_time,
        listing.sale_location,
        listing.deposit_usd,
        listing.trustee_name,
        listing.status,
        norm_addr,
        listing.signal_type,
        norm_parcel,
        listing.auction_status,
        listing.opening_bid_usd,
        listing.appraised_value_usd,
        listing.sec_sale_date,
        listing.case_status,
        listing.case_status_date,
        listing.match_method,
        listing.match_confidence,
        listing.match_score,
        listing.decedent_name,
        listing.case_title,
        listing.decedent_dod,
        listing.probate_internal_id,
        market,
        listing.filing_date,
    )
    return new_id, True


async def update_scrape_run_stats(
    pool: asyncpg.Pool,
    run_id: UUID,
    *,
    dupes: int = 0,
    delisted: int = 0,
    expired: int = 0,
) -> None:
    """Update post-run stats (dupes/delisted/expired) after cross-source passes."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tranchi.scrape_runs
                SET dupes    = $1,
                    delisted = $2,
                    expired  = $3
                WHERE id = $4
                """,
                dupes,
                delisted,
                expired,
                run_id,
            )
    except Exception as exc:
        logger.warning("Failed to update scrape_run stats for %s: %s", run_id, exc)
