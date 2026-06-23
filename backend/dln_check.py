import asyncio, httpx, json
from datetime import date
from app.scrapers.dln import _API_URL, _PER_PAGE, _parse_mdy
from app.scrapers.db import normalize_parcel_number
from app.scrapers._time import today_et
from app.scrapers.user_agents import random_ua

TARGET_CASE = "113987"
TARGET_PARCEL_NORM = normalize_parcel_number("140-10-044")

async def scan(client, feed_type, orderby, meta_key):
    page = 1
    total_pages = 70
    found = []
    seen_case = []
    while page <= min(total_pages, 70):
        params = {"page": page, "per_page": _PER_PAGE, "type": feed_type, "orderby": orderby, "order": "desc"}
        if meta_key:
            params["meta_key"] = meta_key
        r = await client.get(_API_URL, params=params, timeout=30)
        if r.status_code != 200:
            print(feed_type, "page", page, "HTTP", r.status_code); break
        data = r.json()
        total_pages = int(data.get("total_pages") or total_pages)
        rows = data.get("data") or []
        if not rows:
            break
        for rec in rows:
            acf = rec.get("acf") or {}
            case = (acf.get("case_no") or "").strip()
            pn = normalize_parcel_number(acf.get("parcel_num") or acf.get("ppn"))
            if TARGET_CASE in case or (pn and pn == TARGET_PARCEL_NORM):
                found.append({"case_no": case, "parcel_num": acf.get("parcel_num") or acf.get("ppn"),
                              "addr": acf.get("addr"), "location": acf.get("location"),
                              "sale_date": acf.get("sale_date"), "sec_sale_date": acf.get("sec_sale_date"),
                              "defendant": acf.get("defendant"), "min_bid": acf.get("min_bid"),
                              "feed": feed_type})
        if page >= total_pages:
            break
        page += 1
        await asyncio.sleep(0.4)
    return found, total_pages

async def main():
    today = today_et()
    print("today_et:", today, "target_parcel_norm:", TARGET_PARCEL_NORM)
    async with httpx.AsyncClient(headers={"User-Agent": random_ua()}) as client:
        for ft, ob, mk in (("sheriff-sales", "meta_value", "sale_date"), ("delinquent-tax", "case_no", None)):
            found, tp = await scan(client, ft, ob, mk)
            print("FEED", ft, "total_pages", tp, "matches", len(found))
            for f in found:
                print(json.dumps(f, default=str))

asyncio.run(main())
