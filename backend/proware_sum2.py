import asyncio, re
from bs4 import BeautifulSoup
from app.scrapers.proware_client import ProwareSession
from app.scrapers import probate as P

BASE = "https://probate.cuyahogacounty.gov"
TERMS = "/pa/"; AGREE = "ctl00$mpContentPH$btnYes"; SEARCH = "/pa/CaseSearch.aspx"

async def main():
    async with ProwareSession(BASE, rate_limit_sec=1.0) as s:
        await s.accept_agreement(path=TERMS, agree_button_id=AGREE)
        st = await s.fetch_form_state(SEARCH)
        extra = {"ctl00$mpContentPH$txtCaseYear":"2026","ctl00$mpContentPH$ddlCaseCat":"EST",
                 "ctl00$mpContentPH$txtCaseNum":"306861","ctl00$mpContentPH$btnSearchByCase":"Search By Case Number"}
        html, st2 = await s.post_back(SEARCH, extra_fields=extra, viewstate=st)
        client = s._assert_client()
        # Build postback WITHOUT the search button, only the gridview event target, with fresh viewstate from results page
        post = st2.as_post_data()
        post["__EVENTTARGET"] = "ctl00$mpContentPH$gvSearchResults$ctl02$lbName"
        post["__EVENTARGUMENT"] = ""
        await s._rate_limiter()
        r = await client.post(SEARCH, data=post)
        r.raise_for_status()
        # follow possible redirect to CaseSummary
        url = str(r.url)
        print("LANDED URL:", url)
        soup = BeautifulSoup(r.text, "html.parser")
        txt = soup.get_text(" ", strip=True)
        print("contains 306861:", "306861" in txt, "| CaseSummary in url:", "Summary" in url)
        summary = P._parse_case_summary(r.text)
        print("PARSED:", summary)
        for label in ["Status","Case Status","Disposition","Filing Date","Closed","Type"]:
            idx = txt.find(label)
            if idx>=0: print(f"[{label}] ...{txt[idx:idx+90]}...")
        print("SNIPPET:", txt[:1200])

asyncio.run(main())
