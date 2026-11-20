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
t = text_of("https://www.sec.gov/Archives/edgar/data/882184/000088218426000157/dhi-20260930.htm")
print("@@@@@ DHI 10-K len", len(t))
KW = re.compile(r"(gross (profit )?margin|stick|square foot|incentive|construction cost|cost of (homes|sales)|land and lot cost|lot cost|cycle time|homes (closed|in inventory)|net sales orders|lots owned|controlled|West|Southwest|California|impairment|option charges|fiscal 2027|starts)", re.I)
seen = set(); n = 0
for s in re.split(r"(?<=[.;])\s+(?=[A-Z(•])", t):
    if KW.search(s) and 40 < len(s) < 1200 and not re.search(r"could|may adversely|risk", s):
        k = s[:90]
        if k in seen: continue
        seen.add(k); n += 1
        if n > 400: break
        if re.search(r"gross|stick|square|incentive|construction cost|lot cost|cycle|option charges|owned|controlled|Southwest|California|fiscal 2027", s, re.I):
            print("  -", s[:1100])
r = text_of("https://www.sec.gov/Archives/edgar/data/882184/000088218426000119/a9302026exhibit991.htm")
i = r.find("LAND AND LOT POSITION")
print("@@@@@ DHI LOT TABLE"); print(r[i:i+3500])
i = r.find("HOMES CLOSED")
print("@@@@@ DHI HOMES CLOSED"); print(r[i:i+1500])

print("\n@@@@@ YAHOO NEWS SEARCH")
for q in ["Shea Homes", "Tri Pointe Homes", "Brookfield Residential", "Taylor Morrison", "land banking homebuilder", "Millrose Properties"]:
    try:
        j = json.loads(get("https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode({"q": q, "quotesCount": 0, "newsCount": 10})))
        news = j.get("news", [])
        print("Q %s -> %d" % (q, len(news)))
        for a in news[:10]:
            print("    ", time.strftime("%Y-%m-%d", time.gmtime(a.get("providerPublishTime", 0))), "|", a.get("publisher"), "|", a.get("title", "")[:150])
    except Exception as e:
        print("Q %s FAIL %s" % (q, e))
    time.sleep(1)
print("\n@@@@@ BING SIMPLE")
for q in ['"Shea Homes"', '"Tri Pointe Homes"', '"land banking" homebuilders']:
    try:
        root = ET.fromstring(get("https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss"})))
        items = [((i.findtext("pubDate") or "")[:16], (i.findtext("title") or "")[:150]) for i in root.iter("item")]
        print("Q %s -> %d" % (q, len(items)))
        for it in items[:10]: print("    ", it)
    except Exception as e:
        print("Q %s FAIL %s" % (q, e))
print("\n@@@@@ GOOGLE ONE")
try:
    root = ET.fromstring(get("https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": '"Tri Pointe" when:60d', "hl": "en-US", "gl": "US", "ceid": "US:en"})))
    print("google ok", len(list(root.iter("item"))))
except Exception as e:
    print("google FAIL", e)
