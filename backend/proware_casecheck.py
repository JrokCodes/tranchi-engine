import asyncio, re, sys
from bs4 import BeautifulSoup
from app.scrapers.proware_client import ProwareSession

BASE = "https://probate.cuyahogacounty.gov"
TERMS = "/pa/"
AGREE = "ctl00$mpContentPH$btnYes"
SEARCH = "/pa/CaseSearch.aspx"
CASE = "2026EST306861"

async def main():
    async with ProwareSession(BASE, rate_limit_sec=1.0) as s:
        try:
            await s.accept_agreement(path=TERMS, agree_button_id=AGREE)
        except Exception as e:
            print("AGREE_ERR", repr(e))
        # GET the CaseSearch page, dump candidate input/select field names
        st = await s.fetch_form_state(SEARCH)
        client = s._assert_client()
        resp = await client.get(SEARCH)
        soup = BeautifulSoup(resp.text, "html.parser")
        print("=== INPUT/SELECT fields on CaseSearch ===")
        for tag in soup.find_all(["input","select"]):
            n = tag.get("name") or tag.get("id")
            t = tag.get("type")
            if n and not n.startswith("__"):
                print(f"{tag.name} type={t} name={n}")
        # find any case-number-ish textbox
        print("=== buttons ===")
        for tag in soup.find_all("input", {"type":"submit"}):
            print("submit", tag.get("name"), tag.get("value"))

asyncio.run(main())
