import asyncio, re, base64
from bs4 import BeautifulSoup
from app.scrapers.proware_client import ProwareSession
from app.scrapers import probate as P

BASE = "https://probate.cuyahogacounty.gov"
TERMS = "/pa/"
AGREE = "ctl00$mpContentPH$btnYes"
SEARCH = "/pa/CaseSearch.aspx"

async def main():
    async with ProwareSession(BASE, rate_limit_sec=1.0) as s:
        await s.accept_agreement(path=TERMS, agree_button_id=AGREE)
        st = await s.fetch_form_state(SEARCH)
        extra = {
            "ctl00$mpContentPH$txtCaseYear": "2026",
            "ctl00$mpContentPH$ddlCaseCat": "EST",
            "ctl00$mpContentPH$txtCaseNum": "306861",
            "ctl00$mpContentPH$btnSearchByCase": "Search By Case Number",
        }
        html, st2 = await s.post_back(SEARCH, target="", argument="", extra_fields=extra, viewstate=st)
        soup = BeautifulSoup(html, "html.parser")
        # Find ALL anchors + their hrefs / onclick (postback links)
        ids = set()
        for a in soup.find_all("a", href=True):
            h = a["href"]
            m = re.search(r"q=([A-Za-z0-9%=]+)", h)
            if m and "mod" not in m.group(1):
                ids.add((a.get_text(strip=True), m.group(1)))
            # decode base64 q
        print("Q LINKS:", ids)
        # Also dump table cell HTML to find onclick navigations
        for a in soup.find_all("a"):
            oc = a.get("onclick","")
            if oc:
                print("ONCLICK:", a.get_text(strip=True), oc[:120])
        # The decedent name is usually a link to summary. Print raw HTML of result table
        tbl = soup.find_all("table")
        for t in tbl:
            if "306861" in t.get_text():
                print("=== RESULT TABLE HTML (truncated) ===")
                print(str(t)[:2500])
                break

asyncio.run(main())
