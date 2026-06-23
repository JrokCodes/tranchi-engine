import asyncio, re
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
        # Now postback the decedent name link to reach CaseSummary
        html3, st3 = await s.post_back(
            SEARCH, target="ctl00$mpContentPH$gvSearchResults$ctl02$lbName",
            argument="", viewstate=st2,
        )
        # We may have been redirected to CaseSummary.aspx; check current page via parser
        # Try the scraper's own summary parser
        summary = P._parse_case_summary(html3)
        print("=== _parse_case_summary ===", summary)
        soup = BeautifulSoup(html3, "html.parser")
        txt = soup.get_text(" ", strip=True)
        print("contains 306861:", "306861" in txt)
        # find status field
        for label in ["Status","Case Status","Disposition","Filing Date","Date Closed"]:
            idx = txt.find(label)
            if idx >= 0:
                print(f"[{label}] ...{txt[idx:idx+80]}...")
        print("=== SNIPPET 0-1800 ===")
        print(txt[:1800])
        # If we did NOT land on summary, try GET CaseSummary.aspx (session may hold it)
        client = s._assert_client()
        r = await client.get("/pa/CaseSummary.aspx")
        t2 = BeautifulSoup(r.text,"html.parser").get_text(" ",strip=True)
        if "306861" in t2:
            print("=== CaseSummary.aspx GET SNIPPET ===")
            for label in ["Status","Filing Date","Case Type","Closed","Disposed"]:
                idx = t2.find(label)
                if idx>=0: print(f"[{label}] ...{t2[idx:idx+80]}...")

asyncio.run(main())
