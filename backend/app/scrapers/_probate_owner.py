"""Shared probate owner-vs-decedent mis-join guard (cross-market).

INVARIANT: an address-anchor probate join proves the decedent LIVED at a parcel,
NOT that the estate OWNS it. Mis-joins (decedent rented / never owned) have no
sale for the transfer guard to catch, so an ACTIVE owner-surname mismatch is the
"never owned" case. Demote those to match_confidence='review' (VISIBLE + badged;
'review' is in market_config.PROBATE_VISIBLE_CONFIDENCE), NEVER hard-hide —
heirs / trusts / LLCs legitimately differ from the decedent surname.

First shipped inline in summit_probate.py (2026-06-14) as a substring test; lifted
here for probate.py (Cuyahoga) + shelby_probate.py (Shelby) with a STRONGER rule.
The county registries store owner names SURNAME-FIRST ('LEE THOMAS R', 'PLACEK,
JODY L.', 'GRANT CARL E.'), so we compare the decedent's surname to the owner's
SURNAME (first token), not "appears anywhere in the owner string". The substring
test gave false negatives when the decedent surname showed up as the owner's GIVEN
name (decedent 'Sonya Thomas' vs owner 'SOZA, THOMAS' — different people, Soza is
the surname) — first-token comparison catches those while heirs sharing the
surname ('GRANT CARL E.' for decedent 'Joannie Grant') still match.

Name formats handled:
  decedent: 'Last, First M' (comma) | 'First [Middle] Last' (no comma; + JR/SR/II… suffix)
  owner:    'LAST FIRST M' | 'LAST, FIRST M.' | company ('… LLC' -> surname won't match -> review)
"""
from __future__ import annotations

import re

_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
# Alias markers — keep only the primary name before them ('Joann Hall AKA McLemore'
# -> 'Joann Hall'), else the alias's last token is mistaken for the surname.
_ALIAS_RE = re.compile(r"\b(?:A\.?K\.?A\.?|F\.?K\.?A\.?|N\.?K\.?A\.?|NEE)\b", re.I)


def _tokens(name: str) -> list[str]:
    """Uppercased word tokens, commas/periods treated as separators."""
    return [t for t in (w.strip(".,").upper() for w in name.replace(",", " ").split()) if t]


def decedent_surname(decedent_name: str | None) -> str | None:
    """Uppercased decedent surname (>=2 chars), or None.

    'Last, First M'      -> 'LAST'  (comma form: CourtView / Summit)
    'First Middle Last'  -> 'LAST'  (no-comma form: Cuyahoga title-case / Shelby caps)
    Trailing generational suffixes (JR/SR/II/III/IV/V) are ignored.
    """
    if not decedent_name:
        return None
    s = _ALIAS_RE.split(decedent_name)[0].strip()
    if not s:
        return None
    if "," in s:
        toks = _tokens(s.split(",", 1)[0])
        last = toks[0] if toks else ""
    else:
        toks = [t for t in _tokens(s) if t not in _SUFFIXES]
        last = toks[-1] if toks else ""
    return last if len(last) >= 2 else None


def owner_surname(owner_name: str | None) -> str | None:
    """The owner's surname = first token (registries are surname-first), or None.

    For a company owner the first token is a company word (e.g. 'SOUTHWEST'), which
    won't equal a person's surname — so a company-held parcel correctly trips the
    mismatch and gets a 'review' flag for a human to check.
    """
    if not owner_name:
        return None
    toks = _tokens(owner_name)
    return toks[0] if toks and len(toks[0]) >= 2 else None


def surname_mismatch(decedent_name: str | None, owner_name: str | None) -> bool:
    """True when the owner's surname differs from the decedent's surname.

    Conservative: returns False (no demotion) when either name is missing or a
    surname can't be extracted — never demote without evidence.
    """
    if not decedent_name or not owner_name:
        return False
    d = decedent_surname(decedent_name)
    o = owner_surname(owner_name)
    if not d or not o:
        return False
    return d != o
