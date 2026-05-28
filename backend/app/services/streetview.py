"""
Google Street View Static API — URL builder for property images.
Constructs deterministic URLs from property addresses. No geocoding needed.
Ported from gotham-deal-engine; default state is OH for Tranchi (Cuyahoga County).
"""
from urllib.parse import quote_plus

from app.config import settings


def build_street_view_url(
    address: str,
    city: str | None = None,
    state: str = "OH",
    zip_code: str | None = None,
    width: int = 600,
    height: int = 400,
) -> str | None:
    """Build a Google Street View Static API URL for a property address.

    Returns None if no API key is configured.
    """
    if not settings.GOOGLE_MAPS_API_KEY:
        return None

    parts = [address]
    if city:
        parts.append(city)
    parts.append(state)
    if zip_code:
        parts.append(zip_code)

    location = quote_plus(", ".join(parts))

    return (
        f"https://maps.googleapis.com/maps/api/streetview"
        f"?size={width}x{height}"
        f"&location={location}"
        f"&pitch=10"
        f"&source=outdoor"
        f"&key={settings.GOOGLE_MAPS_API_KEY}"
    )
