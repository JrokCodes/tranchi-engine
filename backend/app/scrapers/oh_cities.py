"""Cuyahoga County municipality → county lookup table.

Used by scrapers where the source provides a city but not a county.
Coverage: all 59 municipalities in Cuyahoga County, OH per the Cuyahoga
County Fiscal Office municipality roster.

Lookup is case-insensitive. All keys are lowercase. Values are canonical
county names (no ' County' suffix, matching canonical_county() output).
"""
from __future__ import annotations

# All keys lowercase. Values are canonical county names.
_CITY_TO_COUNTY: dict[str, str] = {
    # Cuyahoga County — 59 municipalities
    "bay village": "Cuyahoga",
    "beachwood": "Cuyahoga",
    "bedford": "Cuyahoga",
    "bedford heights": "Cuyahoga",
    "berea": "Cuyahoga",
    "bratenahl": "Cuyahoga",
    "brecksville": "Cuyahoga",
    "broadview heights": "Cuyahoga",
    "brook park": "Cuyahoga",
    "brooklyn": "Cuyahoga",
    "brooklyn heights": "Cuyahoga",
    "chagrin falls": "Cuyahoga",
    "cleveland": "Cuyahoga",
    "cleveland heights": "Cuyahoga",
    "cuyahoga heights": "Cuyahoga",
    "east cleveland": "Cuyahoga",
    "euclid": "Cuyahoga",
    "fairview park": "Cuyahoga",
    "garfield heights": "Cuyahoga",
    "gates mills": "Cuyahoga",
    "glenwillow": "Cuyahoga",
    "highland heights": "Cuyahoga",
    "highland hills": "Cuyahoga",
    "hunting valley": "Cuyahoga",
    "independence": "Cuyahoga",
    "lakewood": "Cuyahoga",
    "linndale": "Cuyahoga",
    "lyndhurst": "Cuyahoga",
    "maple heights": "Cuyahoga",
    "mayfield heights": "Cuyahoga",
    "mayfield village": "Cuyahoga",
    "mentor-on-the-lake": "Cuyahoga",
    "middleburg heights": "Cuyahoga",
    "moreland hills": "Cuyahoga",
    "newburgh heights": "Cuyahoga",
    "north olmsted": "Cuyahoga",
    "north randall": "Cuyahoga",
    "north royalton": "Cuyahoga",
    "oakwood": "Cuyahoga",
    "oakwood village": "Cuyahoga",
    "olmsted falls": "Cuyahoga",
    "olmsted township": "Cuyahoga",
    "olmsted twp": "Cuyahoga",
    "orange": "Cuyahoga",
    "orange village": "Cuyahoga",
    "parma": "Cuyahoga",
    "parma heights": "Cuyahoga",
    "pepper pike": "Cuyahoga",
    "richmond heights": "Cuyahoga",
    "rocky river": "Cuyahoga",
    "seven hills": "Cuyahoga",
    "shaker heights": "Cuyahoga",
    "solon": "Cuyahoga",
    "south euclid": "Cuyahoga",
    "strongsville": "Cuyahoga",
    "university heights": "Cuyahoga",
    "valley view": "Cuyahoga",
    "walton hills": "Cuyahoga",
    "warrensville heights": "Cuyahoga",
    "westlake": "Cuyahoga",
    "woodmere": "Cuyahoga",

    # Adjacent counties — included so prefilter can assign county correctly
    # even though Phase 1 scope is Cuyahoga only. Prefilter does not reject
    # by county (Marc: pull everything), but canonicalization still runs.
    "akron": "Summit",
    "barberton": "Summit",
    "cuyahoga falls": "Summit",
    "tallmadge": "Summit",
    "mentor": "Lake",
    "willoughby": "Lake",
    "wickliffe": "Lake",
    "painesville": "Lake",
    "elyria": "Lorain",
    "lorain": "Lorain",
    "medina": "Medina",
    "brunswick": "Medina",
    "chardon": "Geauga",
}


def lookup_county(city: str | None) -> str | None:
    """Return the canonical county for an Ohio city.

    Returns None when city is unrecognized.
    """
    if not city:
        return None
    key = city.lower().strip().replace(',', '').replace('.', '').strip()
    return _CITY_TO_COUNTY.get(key)


# Flat set of all known Cuyahoga municipality names (title-cased).
# Used by scrapers that need to validate a city is within Cuyahoga.
CUYAHOGA_MUNICIPALITIES: frozenset[str] = frozenset(
    city.title()
    for city, county in _CITY_TO_COUNTY.items()
    if county == "Cuyahoga"
)
