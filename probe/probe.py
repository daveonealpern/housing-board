import os, json, re, time, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip() or "Housing Signal Board research contact@example.com"
YH = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or YH)
    with urllib.request.urlopen(req, timeout=40) as r:
        b = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip; b = gzip.decompress(b)
        return b.decode("utf-8", "replace")
def sec(url):
    time.sleep(0.2); return get(url, {"User-Agent": SEC_UA, "Accept-Encoding": "gzip"})
def text_of(url):
    raw = sec(url)
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<ix:header>.*?</ix:header>", " ", raw)
    t = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", t)
def mdna(t):
    # last "Management's Discussion and Analysis" heading that is followed by real text, up to Market Risk
    starts = [m.start() for m in re.finditer(r"MANAGEMENT.S DISCUSSION AND ANALYSIS|Management.s Discussion and Analysis of Financial Condition", t)]
    best = ""
    for s in starts:
        e = re.search(r"QUANTITATIVE AND QUALITATIVE DISCLOSURES|Quantitative and Qualitative Disclosures About Market Risk", t[s+200:])
        chunk = t[s: s + 200 + (e.start() if e else 400000)]
        if len(chunk) > len(best): best = chunk
    return best or t
def sents(t, kw, cap, maxlen=600):
    out, seen = [], set()
    for s in re.split(r"(?<=[.;])\s+(?=[A-Z(•])", t):
        if kw.search(s) and len(s) > 40 and not re.search(r"forward-looking|Table of Con", s[:60], re.I):
            k = s[:90]
            if k in seen: continue
            seen.add(k); out.append(s[:maxlen])
            if len(out) >= cap: break
    return out
def ex99(cik, acc):
    acc_d = "%s-%s-%s" % (acc[:10], acc[10:12], acc[12:])
    page = sec("https://www.sec.gov/Archives/edgar/data/%d/%s/%s-index.htm" % (int(cik), acc, acc_d))
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", page):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        if len(cells) >= 4 and re.search(r"EX-99", cells[3]):
            m = re.search(r'href="([^"]+)"', cells[2])
            if m and m.group(1).lower().endswith((".htm", ".html")):
                return "https://www.sec.gov" + m.group(1)
    return None
def latest(cik, forms, items=None, skip=0):
    f = json.loads(sec("https://data.sec.gov/submissions/CIK%s.json" % cik))["filings"]["recent"]
    n = 0
    for i, form in enumerate(f["form"]):
        if form in forms and (items is None or items in (f.get("items", [""]*(i+1))[i] or "")):
            if n < skip: n += 1; continue
            acc = f["accessionNumber"][i].replace("-", "")
            return {"form": form, "filed": f["filingDate"][i], "acc": acc, "cik": cik,
                    "url": "https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (int(cik), acc, f["primaryDocument"][i])}
tick = json.loads(sec("https://www.sec.gov/files/company_tickers.json"))
lookup = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in tick.values()}

COST = re.compile(r"(construction cost|stick[- ]and[- ]brick|per square foot|lumber|labor cost|labor availab|house cost|direct cost|direct construction|cost of materials|material cost|materials, labor|tariff|cycle time|build time|vendors|cost savings|construction and land cost|land cost|lot cost)", re.I)

print("=== DHI 10-K MD&A (broad) ===")
q = latest(lookup["DHI"], ("10-K",))
t = mdna(text_of(q["url"]))
print("mdna chars", len(t))
BROAD = re.compile(r"(homes closed|closed \d|net sales orders|cancellation|gross margin|gross profit|incentive|lots (owned|controlled)|homebuilding lot|West region|California|impairment|average (sales|selling) price|backlog|spec|stick|construction cost|per square foot|fiscal 2027|expect|guidance|cycle time|land and lot|lot cost|share repurchase|rental|Forestar)", re.I)
for s in sents(t, BROAD, 120): print("  -", s)

print("\n=== DHI release ===")
e = latest(lookup["DHI"], ("8-K",), "2.02"); u = ex99(e["cik"], e["acc"]); print(u)
if u:
    for s in sents(text_of(u), re.compile(r"(stick|construction cost|per square foot|gross margin|incentive|fiscal 2027|expect|guidance|cycle|lots|orders|cancellation|homes closed)", re.I), 40): print("  *", s)

for tk in ["LEN","PHM","NVR","TOL","KBH","MTH","CCS","LGIH","MHO"]:
    q = latest(lookup[tk], ("10-Q","10-K"))
    t = mdna(text_of(q["url"]))
    print("\n=== %s %s %s MD&A chars %d ===" % (tk, q["form"], q["filed"], len(t)))
    for s in sents(t, COST, 30): print("  -", s)
    e = latest(lookup[tk], ("8-K",), "2.02")
    if e:
        u = ex99(e["cik"], e["acc"]); print("  >> release", e["filed"], u)
        if u:
            for s in sents(text_of(u), COST, 15): print("  *", s)

print("\n=== LAND BANKS ===")
LB = re.compile(r"(lots owned|lots controlled|owned and controlled|fully developed|D\.R\. Horton|% of|percent|guidance|expect|fiscal 2027|land sales|homesites|counterpart|Rausch|Dream Finders|Other Agreements|Valencia|Great Park|Candlestick|Hearthstone|homesite sales|average price per|acre|backlog|record)", re.I)
for tk in ["FOR","FPH","MRP"]:
    q = latest(lookup[tk], ("10-Q","10-K"))
    t = mdna(text_of(q["url"]))
    print("\n--- %s %s %s MD&A chars %d" % (tk, q["form"], q["filed"], len(t)))
    for s in sents(t, LB, 45): print("  -", s)
    e = latest(lookup[tk], ("8-K",), "2.02")
    if e:
        u = ex99(e["cik"], e["acc"]); print("  >> release", e["filed"], u)
        if u:
            for s in sents(text_of(u), LB, 30): print("  *", s)

print("\n=== NEWS SOURCES ===")
NAMES = '"Shea Homes" OR "Brookfield Residential" OR "New Home Company" OR "Trumark" OR "City Ventures" OR "Tri Pointe" OR "Pardee Homes" OR "Taylor Morrison" OR "Landsea" OR "Warmington" OR "Lewis Group" OR "Olson Homes" OR "Melia Homes" OR "Woodside Homes" OR "Williams Homes"'
def show(label, fn):
    try:
        items = fn(); print("%s: %d" % (label, len(items)))
        for it in items[:15]: print("    ", it)
    except Exception as ex:
        print("%s FAIL %s" % (label, ex))
def rss(url):
    root = ET.fromstring(get(url))
    return [((i.findtext("pubDate") or "")[:16], (i.findtext("title") or "")[:140], (i.findtext("link") or "")[:100]) for i in root.iter("item")]
show("bing ca", lambda: rss("https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": "(" + NAMES + ") California", "format": "rss"})))
show("bing landbank", lambda: rss("https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": '"land banking" homebuilder', "format": "rss"})))
def gdelt(q):
    u = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({"query": q, "mode": "artlist", "format": "json", "maxrecords": "50", "timespan": "60d", "sort": "datedesc"})
    j = json.loads(get(u) or "{}")
    return [(a.get("seendate"), a.get("domain"), a.get("title", "")[:130]) for a in j.get("articles", [])]
show("gdelt ca", lambda: gdelt('(' + NAMES + ') sourcecountry:US'))
time.sleep(6)
show("gdelt landbank", lambda: gdelt('("land banking" OR "Millrose" OR "Forestar" OR "Five Point Holdings") homebuilder'))
time.sleep(2)
show("google retry", lambda: rss("https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": '"Shea Homes" when:60d', "hl": "en-US", "gl": "US", "ceid": "US:en"})))
for name, u in [("builderonline", "https://www.builderonline.com/rss"), ("therealdeal la", "https://therealdeal.com/la/feed/"),
                ("ocregister re", "https://www.ocregister.com/business/real-estate/feed/"), ("bisnow", "https://www.bisnow.com/rss/los-angeles"),
                ("probuilder", "https://www.probuilder.com/rss.xml"), ("nahb eye", "https://eyeonhousing.org/feed/"),
                ("globest", "https://www.globest.com/feed/"), ("thebuilderspost", "https://www.thebuilderspost.com/feed/"),
                ("housingwire", "https://www.housingwire.com/feed/")]:
    show(name, lambda u=u: rss(u))
