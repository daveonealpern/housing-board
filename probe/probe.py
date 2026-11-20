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
    raw = re.sub(r"(?i)</(p|div|tr|li|h\d)>", "\n", raw)
    t = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)
DOCS = {
 "DHI_REL": "https://www.sec.gov/Archives/edgar/data/882184/000088218426000119/a9302026exhibit991.htm",
 "PHM_REL": "https://www.sec.gov/Archives/edgar/data/822416/000082241626000041/ex991earningspr9302026.htm",
 "FOR_REL": "https://www.sec.gov/Archives/edgar/data/1406587/000162828026068139/a9302026exhibit991.htm",
 "FPH_REL": "https://www.sec.gov/Archives/edgar/data/1574197/000157419726000032/ex-991xfphx93026x8k.htm",
 "MRP_REL": "https://www.sec.gov/Archives/edgar/data/2017206/000119312526432747/ck0002017206-ex99_1.htm",
 "LGI_REL": "https://www.sec.gov/Archives/edgar/data/1580670/000158067026000085/a09302026ex991earningsrele.htm",
 "MHO_REL": "https://www.sec.gov/Archives/edgar/data/799292/000079929226000030/exhibit991earningspressrel.htm",
 "NVR_REL": "https://www.sec.gov/Archives/edgar/data/906163/000090616326000099/q32026ex991_earningsrelease.htm",
}
for k, u in DOCS.items():
    try:
        t = text_of(u)
        print("\n@@@@@ %s %s chars %d" % (k, u, len(t)))
        print(t[:30000])
    except Exception as e:
        print("@@@@@ %s FAIL %s" % (k, e))
# Pulte 10-Q MD&A margin + vendor sentences
t = re.sub(r"\s+", " ", text_of("https://www.sec.gov/Archives/edgar/data/822416/000082241626000043/phm-20260930.htm"))
print("\n@@@@@ PHM_10Q selected")
for s in re.split(r"(?<=[.;])\s+(?=[A-Z(•–])", t):
    if re.search(r"gross margin|vendors|house cost|construction cost|land cost|lot cost|incentive|West|California|option agreements|lots (under|owned|controlled)|impairment", s, re.I) and len(s) < 900:
        print("  -", s)

print("\n@@@@@ GOOGLE NEWS")
def rss(q):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(get(url))
    return [((i.findtext("pubDate") or "")[:16], (i.find("source").text if i.find("source") is not None else ""), (i.findtext("title") or "")) for i in root.iter("item")]
QS = [
 '"Shea Homes" OR "Brookfield Residential" OR "Trumark Homes" OR "City Ventures" OR "New Home Company" when:60d',
 '"Tri Pointe" OR "Pardee Homes" OR "Taylor Morrison" OR "Landsea Homes" OR "Woodside Homes" when:60d',
 '"Warmington" OR "Lewis Group" OR "Olson Homes" OR "Melia Homes" OR "Williams Homes" OR "Rancho Mission Viejo" when:60d',
 '"land banking" OR "land banker" OR "lot banking" homebuilder when:60d',
 '"Millrose Properties" OR "Varde Partners" OR "Kennedy Lewis" OR "Essential Housing" OR "Forestar" when:60d',
]
for q in QS:
    for attempt in range(3):
        try:
            items = rss(q); print("Q %s -> %d" % (q[:70], len(items)))
            for it in items[:40]: print("    ", it[0], "|", it[1], "|", it[2][:160])
            break
        except Exception as e:
            print("Q %s attempt %d FAIL %s" % (q[:60], attempt, e)); time.sleep(12)
    time.sleep(4)
