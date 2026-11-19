# One-off source probe for the Housing Signal Board. Runs on GitHub Actions on
# a throwaway branch; prints what each source returns so the collector can be
# written against real responses.
import os, json, re, time, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "Housing Signal Board research contact@example.com"
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
YH = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

def get(url, headers=None, raw=False):
    req = urllib.request.Request(url, headers=headers or YH)
    with urllib.request.urlopen(req, timeout=40) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip; b = gzip.decompress(b)
        return b if raw else b.decode("utf-8", "replace")

def sec(url): 
    time.sleep(0.2)
    return get(url, {"User-Agent": SEC_UA, "Accept-Encoding": "gzip"})

print("=== YAHOO ===")
for s in ["CL=F","BZ=F","HO=F","RB=F","NG=F","LBR=F","LBS=F","HG=F","HRC=F","ALI=F","MRP","FOR","FPH","ITB","XHB","^TNX"]:
    try:
        j = json.loads(get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=ytd&interval=1d" % urllib.parse.quote(s)))
        r = j["chart"]["result"][0]; m = r["meta"]
        ts = r.get("timestamp") or []; cl = r["indicators"]["quote"][0].get("close") or []
        good = [(t, c) for t, c in zip(ts, cl) if c is not None]
        print("%-6s ok name=%r cur=%s price=%s prev=%s n=%d nulls=%d first=%s %.3f last=%s %.3f unit=%s exch=%s" % (
            s, m.get("shortName"), m.get("currency"), m.get("regularMarketPrice"), m.get("chartPreviousClose"),
            len(good), len(cl) - len(good),
            time.strftime("%Y-%m-%d", time.gmtime(good[0][0])), good[0][1],
            time.strftime("%Y-%m-%d", time.gmtime(good[-1][0])), good[-1][1], m.get("instrumentType"), m.get("exchangeName")))
    except Exception as e:
        print("%-6s FAIL %s" % (s, e))

print("\n=== FRED ===")
def fred(path, **p):
    p.update(api_key=FRED_KEY, file_type="json")
    return json.loads(get("https://api.stlouisfed.org/fred/%s?%s" % (path, urllib.parse.urlencode(p))))
for sid in ["WPU1333","WPU133","WPU137","WPU1371","WPU13710101","WPU0721","WPU072105","WPU07210603","WPU1392","WPU1394","WPU0811","WPU083","WPU1017","WPU057303","WPU0553","PCU327320327320","PCU327420327420","WPU1321","WPU102502","WPU0813","WPU08130101","WPU102"]:
    try:
        s = fred("series", series_id=sid)["seriess"][0]
        o = fred("series/observations", series_id=sid, sort_order="desc", limit=1)["observations"][0]
        print("%-16s %s | %s | last %s %s | pop %s" % (sid, s["title"], s["frequency_short"], o["date"], o["value"], s.get("popularity")))
    except Exception as e:
        print("%-16s FAIL %s" % (sid, e))
for q in ["ready-mixed concrete producer price", "gypsum products producer price", "plastic construction products producer price",
          "insulation materials producer price", "asphalt producer price index", "softwood lumber producer price", "diesel fuel producer price index"]:
    try:
        r = fred("series/search", search_text=q, order_by="popularity", limit=6)
        print("search %r:" % q)
        for s in r["seriess"]:
            print("   %-18s %s | %s | pop %s | end %s" % (s["id"], s["title"][:110], s["frequency_short"], s["popularity"], s["observation_end"]))
    except Exception as e:
        print("search FAIL", q, e)

print("\n=== GOOGLE NEWS ===")
def gnews(q):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(get(url))
    return [(i.findtext("pubDate"), (i.find("source").text if i.find("source") is not None else ""), i.findtext("title"), i.findtext("link")[:90]) for i in root.iter("item")]
QS = {
 "ca_private": '("Shea Homes" OR "Brookfield Residential" OR "New Home Company" OR "Trumark Homes" OR "City Ventures" OR "Tri Pointe" OR "Pardee Homes" OR "Taylor Morrison" OR "Landsea Homes" OR "Warmington" OR "Williams Homes" OR "Lewis Group" OR "Olson Homes" OR "Melia Homes" OR "Woodside Homes") California when:60d',
 "landbank": '("land banking" OR "land bank" OR "Millrose Properties" OR "Forestar" OR "Five Point Holdings" OR "Varde" OR "Kennedy Lewis" OR "Essential Housing") homebuilder when:60d',
}
for k, q in QS.items():
    try:
        items = gnews(q)
        print("%s: %d items" % (k, len(items)))
        for it in items[:25]: print("   ", it[0][:16], "|", it[1], "|", it[2][:150])
    except Exception as e:
        print(k, "FAIL", e)

print("\n=== SEC ===")
tick = json.loads(sec("https://www.sec.gov/files/company_tickers.json"))
lookup = {v["ticker"]: (str(v["cik_str"]).zfill(10), v["title"]) for v in tick.values()}
KW = re.compile(r"(construction cost|stick[- ]and[- ]brick|per square foot|lumber|labor|tariff|direct cost|house cost|cost to build|build cost|cycle time|construction time|cost inflation|materials cost|cost of materials|subcontract|cost savings|cost reduction|land cost|land and development cost)", re.I)
LKW = re.compile(r"(homesite|option|lots owned|lots controlled|takedown|Lennar|D\.R\. Horton|Horton|third[- ]party|counterpart|land sales|Great Park|Valencia|Candlestick|backlog|impair|deposit|lots sold|development pipeline|average sales price)", re.I)

def text_of(url):
    raw = sec(url)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw)
    t = re.sub(r"(?s)<[^>]+>", " ", raw)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t)

def sentences(t, kw, cap, maxlen=520):
    out, seen = [], set()
    for s in re.split(r"(?<=[.;])\s+(?=[A-Z(])", t):
        if kw.search(s) and 40 < len(s) and not re.search(r"forward-looking|Item \d|incorporated by reference", s, re.I):
            k = s[:80]
            if k in seen: continue
            seen.add(k); out.append(s[:maxlen])
            if len(out) >= cap: break
    return out

def latest(cik, forms, items=None):
    sub = json.loads(sec("https://data.sec.gov/submissions/CIK%s.json" % cik))
    f = sub["filings"]["recent"]
    for i, form in enumerate(f["form"]):
        if form in forms and (items is None or items in (f.get("items", [""]*(i+1))[i] or "")):
            acc = f["accessionNumber"][i].replace("-", "")
            return {"form": form, "filed": f["filingDate"][i], "period": f["reportDate"][i], "acc": acc,
                    "url": "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (int(cik), acc, f["primaryDocument"][i]), "cik": cik}
    return None

def release_url(r):
    idx = json.loads(sec("https://www.sec.gov/Archives/edgar/data/%s/%s/index.json" % (int(r["cik"]), r["acc"])))
    for it in idx["directory"]["item"]:
        n = it["name"].lower()
        if ("ex99" in n or "ex-99" in n or "exhibit99" in n or "ex991" in n) and n.endswith((".htm", ".html")):
            return "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (int(r["cik"]), r["acc"], it["name"])
    return None

for tk in ["DHI","LEN","PHM","NVR","TOL","KBH","MTH","CCS","LGIH","MHO"]:
    cik, title = lookup[tk]
    try:
        q = latest(cik, ("10-Q","10-K"))
        print("\n##### %s %s %s period %s filed %s %s" % (tk, title, q["form"], q["period"], q["filed"], q["url"]))
        t = text_of(q["url"])
        for s in sentences(t, KW, 40): print("  -", s)
        e = latest(cik, ("8-K",), "2.02")
        if e:
            ru = release_url(e)
            print("  >> release 8-K filed %s %s" % (e["filed"], ru))
            if ru:
                for s in sentences(text_of(ru), KW, 25): print("  *", s)
    except Exception as ex:
        print("##### %s FAIL %s" % (tk, ex))

for tk in ["MRP","FOR","FPH"]:
    if tk not in lookup: print("\n##### %s not in SEC ticker index" % tk); continue
    cik, title = lookup[tk]
    try:
        q = latest(cik, ("10-Q","10-K"))
        print("\n##### %s %s cik %s %s period %s filed %s %s" % (tk, title, cik, q["form"], q["period"], q["filed"], q["url"]))
        t = text_of(q["url"])
        for s in sentences(t, LKW, 45): print("  -", s)
        e = latest(cik, ("8-K",), "2.02")
        if e:
            ru = release_url(e)
            print("  >> release 8-K filed %s %s" % (e["filed"], ru))
            if ru:
                for s in sentences(text_of(ru), LKW, 25): print("  *", s)
    except Exception as ex:
        print("##### %s FAIL %s" % (tk, ex))
