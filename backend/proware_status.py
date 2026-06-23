import asyncio, re, sys
from bs4 import BeautifulSoup
from app.scrapers.proware_client import ProwareSession

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
        txt = soup.get_text(" ", strip=True)
        # Print any case number + status text we can find
        print("=== contains 306861:", "306861" in txt, " | 2026EST306861:", "2026EST306861" in txt)
        # Look for status keywords
        for kw in ["Open","Closed","Disposed","Terminated","Dismissed","Active","Status","Reopened"]:
            for m in re.finditer(kw, txt):
                seg = txt[max(0,m.start()-40):m.start()+40]
                print(f"[{kw}] ...{seg}...")
                break
        # Dump any result table rows / links
        print("=== links with q= (case summary links) ===")
        for a in soup.find_all("a", href=True):
            if "CaseSummary" in a["href"] or "q=" in a["href"]:
                print(a.get_text(strip=True), "->", a["href"][:80])
        # Print a chunk of result area
        print("=== TEXT SNIPPET (first 1500 chars) ===")
        print(txt[:1500])

asyncio.run(main())
