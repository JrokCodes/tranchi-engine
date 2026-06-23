import asyncio, httpx, re
from app.scrapers import fiscal_officer as fo

PARCEL = "140-10-044"

async def main():
    async with httpx.AsyncClient(follow_redirects=True, timeout=40) as client:
        html = await fo._fetch_search_page(client, PARCEL, fo._ENTIRE_COUNTY_CODE, fo._MODE_PARCEL)
        hits = fo._parse_hit_list(html)
        print("hits:", len(hits))
        for h in hits[:3]:
            print("  owner:", getattr(h, "owner_name", None), "| situs:", getattr(h, "situs_address", None))
        # look for any sale-date hints in the html
        for m in set(re.findall(r"(?:sale|transfer)[^<]{0,40}(\d{1,2}/\d{1,2}/\d{4})", html, re.I)):
            print("  sale-hint:", m)
asyncio.run(main())
