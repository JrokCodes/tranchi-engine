import asyncio
from app.scrapers.arcgis_client import query_features, count_features

URL = "https://gis.cuyahogacounty.us/server/rest/services/CCFO/EPV_Prod/FeatureServer/2"
OUT = "parcelpin,parcel_id,parcel_owner,par_addr_all,total_net_delq_balance,foreclosure_flag,tax_market_total"
Q = chr(39)  # single quote

async def main():
    # sanity: does the server respond at all?
    try:
        n = await count_features(URL, where="total_net_delq_balance > 0")
        print("count delq>0:", n)
    except Exception as e:
        print("count ERR", repr(e)[:200])

    for field in ("parcelpin", "parcel_id"):
        for val in ("140-10-044", "14010044"):
            where = field + "=" + Q + val + Q
            try:
                got = False
                async for batch in query_features(URL, where=where, out_fields=OUT, batch_size=10):
                    print("WHERE", where, "->", len(batch))
                    for a in batch[:3]:
                        print("   ", a)
                    got = True
                    break
                if not got:
                    print("WHERE", where, "-> 0 rows")
            except Exception as e:
                print("WHERE", where, "ERR", repr(e)[:160])

asyncio.run(main())
