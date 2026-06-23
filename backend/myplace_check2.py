import asyncio, httpx
from app.scrapers import fiscal_officer as fo

PARCEL = "140-10-044"

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
        html = await fo._fetch_search_page(client, PARCEL, fo._ENTIRE_COUNTY_CODE, fo._MODE_PARCEL)
        hits = fo._parse_hit_list(html)
        h = hits[0]
        print("type:", type(h))
        print("repr:", repr(h)[:500])
        if hasattr(h, "_fields"):
            print("fields:", h._fields)
        elif hasattr(h, "__dict__"):
            print("dict:", h.__dict__)
asyncio.run(main())
