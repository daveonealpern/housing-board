#!/usr/bin/env python3
"""
Housing Signal Board - data collector.

Runs on a schedule in GitHub Actions. Pulls every source, writes data.json.
Nothing here needs a human. If a source fails it is recorded in data.json
under "errors" and shows on the page, rather than disappearing quietly.
"""

import os, sys, json, time, gzip, csv, datetime, urllib.request, urllib.parse, urllib.error

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
SEC_UA   = os.environ.get("SEC_USER_AGENT", "").strip() or "Housing Signal Board research contact@example.com"
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()
BRIEF_PATH      = os.environ.get("BRIEF_PATH", "brief.json")
FRED_BASE = "https://api.stlouisfed.org/fred"
OUT_PATH  = os.environ.get("OUT_PATH", "data.json")
TODAY = datetime.date.today()
START = (TODAY - datetime.timedelta(days=365 * 10)).isoformat()

# A horizontal marker drawn on a chart where an absolute level carries meaning.
# Series not listed here get a dashed line at their own median instead.
REFS = {
    "MSACSR":       {"v": 6.0,  "label": "balanced market"},
    "MORTGAGE30US": {"v": 6.0,  "label": "6% threshold"},
    "DRSFRMACBS":   {"v": 2.0,  "label": "pre-2020 norm"},
    "T10Y2Y":       {"v": 0.0,  "label": "inversion line"},
    "BAMLH0A0HYM2": {"v": 4.0,  "label": "calm-market norm"},
    "BAMLC0A4CBBB": {"v": 1.5,  "label": "calm-market norm"},
}

# Shown in the rates band at the top of the sheet, in this order.
RATES = ["MORTGAGE30US", "SPREAD", "DGS10", "T10Y2Y", "SOFR", "DPRIME", "FEDFUNDS"]

# sid -> multiplier to convert FRED's native value into a true, real-world
# number. Verified against each series' own FRED units field:
#   - permits/starts/sales/completions/employment are reported in thousands
#   - construction spend is reported in millions of dollars
#   - BAML spread series are reported in PERCENT, not basis points, despite
#     the market convention of quoting credit spreads in bp
SCALE = {
    "AUTHNOTT": 1000, "PERMIT": 1000, "PERMIT1": 1000, "HOUST1F": 1000, "HOUST": 1000,
    "HSN1F": 1000, "EXHOSLUSM495S": 1000, "COMPUTSA": 1000,
    "JTS2300JOL": 1000, "USCONS": 1000,
    "PRRESCONS": 1_000_000,
    "BAMLC0A4CBBB": 100, "BAMLH0A0HYM2": 100,
}

ERRORS = []
def note(msg):
    ERRORS.append(msg)
    print("  ! " + msg, file=sys.stderr)

def to_monthly(obs):
    """Collapse to one point per month, keeping the last reading in each.

    Daily, weekly, monthly and quarterly series share one payload. Without this
    a 96-point chart is eight years of permits but three months of SOFR, and
    every range comparison is measured against a different window.
    """
    if not obs:
        return obs
    by_month = {}
    for o in obs:                       # observations arrive sorted ascending
        by_month[o["d"][:7]] = o        # last of the month wins
    return [by_month[k] for k in sorted(by_month)][-120:]


def get_json(url, headers=None, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": SEC_UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":          # gzip magic number
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < tries - 1:
                time.sleep(4 * (attempt + 1)); continue
            raise
        except Exception:
            if attempt < tries - 1:
                time.sleep(3); continue
            raise

def fred(path, **params):
    params.update({"api_key": FRED_KEY, "file_type": "json"})
    return get_json(FRED_BASE + path + "?" + urllib.parse.urlencode(params))

# ---------------------------------------------------------------------------
# Indicator registry.  lead = months of forecasting lead (negative = lagging).
# good = the direction that means expansion.
# ---------------------------------------------------------------------------
SERIES = [
    # --- land and permit pipeline ---
    ("AUTHNOTT",   "Permits authorized, not started", "structural",  9, "units",      "down",
     "A swelling backlog means builders hold entitlements and are choosing not to break ground.",
     "housing units authorized not started"),
    ("PERMIT",     "Building permits, total",         "structural",  7, "units SAAR", "up",
     "The front of the construction pipeline. Turns before starts by one to two quarters.",
     "new private housing units authorized by building permits"),
    ("PERMIT1",    "Single-family permits",           "structural",  7, "units SAAR", "up",
     "Cleaner than the total, which multifamily swings distort.",
     "new private housing units authorized single family"),
    ("HOUST1F",    "Single-family housing starts",    "structural",  4, "units SAAR", "up",
     "Where permits become dirt. Compare against permits to see the conversion rate.",
     "privately owned housing starts single family"),
    ("HOUST",      "Housing starts, total",           "structural",  4, "units SAAR", "up",
     "Total starts including multifamily, which swings hard on a few large projects.",
     "housing starts total new privately owned"),

    # --- demand and credit ---
    ("MORTGAGE30US", "30-year fixed mortgage rate",   "demand",      3, "%",            "down",
     "The affordability lever. Direction and speed matter more than the level.",
     "30-year fixed rate mortgage average"),
    ("UMCSENT",      "Consumer sentiment",            "demand",      3, "index",        "up",
     "Headline sentiment is a weak housing signal on its own, but it turns early.",
     "university of michigan consumer sentiment"),
    ("TDSP",         "Household debt service ratio",  "demand",      4, "%",            "down",
     "How stretched household budgets already are before a payment shock.",
     "household debt service payments percent disposable income"),

    # --- capital markets: the rate complex and credit spreads ---
    ("T10Y2Y",       "2s10s curve",                   "capital",    12, "%",            "up",
     "10-year minus 2-year. Below zero is an inversion, which has led recessions "
     "by roughly a year. Steepening back above zero is the signal, not the inversion itself.",
     "10-year treasury minus 2-year treasury constant maturity"),
    ("DGS2",         "2-year Treasury",               "capital",     3, "%",            "down",
     "Tracks where the market thinks policy is going over the next two years.",
     "2-year treasury constant maturity rate"),
    ("DGS10",        "10-year Treasury",              "capital",     3, "%",            "down",
     "The base the 30-year mortgage prices off. Watch the gap between the two.",
     "10-year treasury constant maturity rate"),
    ("DGS30",        "30-year Treasury",              "capital",     3, "%",            "down",
     "The long end. Matters for permanent takeout financing more than for construction.",
     "30-year treasury constant maturity rate"),
    ("SOFR",         "SOFR, overnight",               "capital",     3, "%",            "down",
     "The overnight secured rate that replaced LIBOR in 2023. The floor under "
     "floating construction debt.",
     "secured overnight financing rate"),
    ("SOFR30DAYAVG", "SOFR 30-day average",           "capital",     3, "%",            "down",
     "Backward-looking compounded average from the New York Fed. The closest free "
     "public stand-in for 1-month Term SOFR, which is licensed and not on FRED.",
     "30-day average sofr"),
    ("SOFR90DAYAVG", "SOFR 90-day average",           "capital",     3, "%",            "down",
     "The quarterly-reset equivalent. Use this one if your facility resets quarterly.",
     "90-day average sofr"),
    ("DPRIME",       "Bank prime loan rate",          "capital",     3, "%",            "down",
     "What smaller land and construction facilities actually price off. Moves in "
     "lockstep with fed funds, historically about 300bp above it.",
     "bank prime loan rate"),
    ("FEDFUNDS",     "Federal funds rate",            "capital",     4, "%",            "down",
     "The policy rate. Housing responds to the long end, but this sets the tone.",
     "federal funds effective rate"),
    ("MORTGAGE15US", "15-year fixed mortgage",        "capital",     3, "%",            "down",
     "The gap to the 30-year shows how much of the curve borrowers are paying for.",
     "15-year fixed rate mortgage average"),
    ("BAMLC0A4CBBB", "BBB corporate spread",          "capital",     6, "bp",           "down",
     "Investment-grade credit stress. Widening here tightens builder revolvers "
     "before it shows up in any housing number.",
     "ice bofa bbb us corporate index option-adjusted spread"),
    ("BAMLH0A0HYM2", "High yield spread",             "capital",     6, "bp",           "down",
     "The risk appetite gauge. Blows out first when credit turns.",
     "ice bofa us high yield index option-adjusted spread"),

    # --- activity now ---
    ("HSN1F",         "New home sales",               "coincident",  0, "units SAAR",       "up",
     "Recorded at contract signing, so slightly ahead of existing sales.",
     "new one family houses sold united states"),
    ("MSACSR",        "Months supply, new homes",     "coincident",  1, "months",       "down",
     "Above roughly six months has historically preceded builder discounting.",
     "monthly supply of new houses"),
    ("EXHOSLUSM495S", "Existing home sales",          "coincident", -1, "units SAAR",       "up",
     "Records at closing, so it reflects decisions made a month or two earlier.",
     "existing home sales"),
    ("COMPUTSA",      "Housing completions",          "coincident", -2, "units SAAR",       "up",
     "Supply arriving now from starts twelve to eighteen months ago.",
     "new privately owned housing units completed"),
    ("MSPUS",         "Median sales price, US homes", "coincident", -2, "$",            "up",
     "Mix-sensitive. A falling median can mean cheaper homes selling, not falling values.",
     "median sales price of houses sold"),
    ("CSUSHPINSA",    "Case-Shiller national",        "coincident", -3, "index",        "up",
     "Two-month lag on a three-month moving average, so it confirms very late.",
     "s&p case-shiller u.s. national home price index"),

    # --- cost and labor ---
    ("WPUSI012011", "Construction materials PPI",     "cost",        1, "index",        "down",
     "Reaches pro formas before it reaches completed cost.",
     "producer price index construction materials"),
    ("JTS2300JOL",  "Construction job openings",      "cost",        2, "jobs",            "up",
     "Hiring intent turns before payrolls. Better labor read than employment level.",
     "job openings construction"),
    ("USCONS",      "Construction employment",        "cost",        0, "jobs",            "up",
     "Coincident. Useful mainly as a check on the openings series.",
     "all employees construction"),
    ("PRRESCONS",   "Residential construction spend", "cost",       -1, "$ SAAR",      "up",
     "Put-in-place dollars, so it reflects work already underway.",
     "total private construction spending residential"),

    # --- distress ---
    ("DRSFRMACBS",     "SF mortgage delinquency",     "distress",   -8, "%",            "down",
     "Lags badly but confirms turns. Watch the rate of change, not the level.",
     "delinquency rate single-family residential mortgages commercial banks"),
    ("DRCRELEXFACBS",  "CRE loan delinquency",        "distress",   -7, "%",            "down",
     "Multifamily is the segment to watch given the completion wave working through.",
     "delinquency rate commercial real estate loans banks"),

    # --- california ---
    ("CABPPRIV", "California permits authorized",     "ca",          7, "units",        "up",
     "State pipeline. Compare against national permits to see if California is diverging.",
     "new private housing units authorized california"),
    ("CASTHPI",  "California house price index",      "ca",         -3, "index",        "up",
     "FHFA all-transactions, quarterly. Slow but consistent.",
     "all-transactions house price index for california"),
    ("LXXRSA",   "Los Angeles Case-Shiller",          "ca",         -3, "index",        "up",
     "Your metro, same two-month lag as the national index.",
     "s&p case-shiller ca-los angeles home price index"),
    ("CAUR",     "California unemployment",           "ca",         -1, "%",            "down",
     "Context for absorption. Rising unemployment shrinks the buyer pool before price moves.",
     "unemployment rate in california"),
]

# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------
def resolve_series_id(sid, search_text):
    """If an ID has been retired or renamed, find the closest live replacement."""
    try:
        r = fred("/series/search", search_text=search_text, limit=5,
                 order_by="popularity", sort_order="desc")
        hits = r.get("seriess", [])
        if hits:
            note("Series %s not found; substituted %s (%s)" % (sid, hits[0]["id"], hits[0]["title"]))
            return hits[0]["id"], hits[0]["title"]
    except Exception as e:
        note("Search fallback failed for %s: %s" % (sid, e))
    return None, None

def pull_fred():
    out, release_of = {}, {}
    for sid, name, bucket, lead, units, good, read, search_text in SERIES:
        active = sid
        obs = None
        for _ in range(2):
            try:
                r = fred("/series/observations", series_id=active,
                         observation_start=START, sort_order="asc")
                mult = SCALE.get(sid, 1)
                obs = [{"d": o["date"], "v": float(o["value"]) * mult}
                       for o in r.get("observations", []) if o["value"] not in (".", "")]
                obs = to_monthly(obs)
                break
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    alt, _title = resolve_series_id(active, search_text)
                    if alt and alt != active:
                        active = alt; continue
                note("FRED %s failed: HTTP %s" % (active, e.code)); break
            except Exception as e:
                note("FRED %s failed: %s" % (active, e)); break

        rec = {"name": name, "bucket": bucket, "lead": lead, "units": units,
               "good": good, "read": read, "sid": active, "obs": obs or []}
        if sid in REFS:
            rec["ref"] = REFS[sid]

        # metadata: units, frequency, last update stamp
        try:
            meta = fred("/series", series_id=active).get("seriess", [{}])[0]
            rec["frequency"] = meta.get("frequency", "")
            rec["updated"] = meta.get("last_updated", "")[:10]
            rec["title"] = meta.get("title", name)
        except Exception:
            pass

        # which FRED release publishes this series
        try:
            rel = fred("/series/release", series_id=active).get("releases", [{}])[0]
            rec["release_id"] = rel.get("id")
            rec["release_name"] = rel.get("name", "")
            release_of[rel.get("id")] = rel.get("name", "")
        except Exception:
            pass

        out[sid] = rec
        print("  %-16s %5d obs" % (active, len(rec["obs"])))
        time.sleep(0.35)
    return out, release_of

def add_spread(series):
    """30-year mortgage rate minus the 10-year Treasury, in basis points."""
    m, t = series.get("MORTGAGE30US"), series.get("DGS10")
    if not m or not t or not m["obs"] or not t["obs"]:
        note("Spread skipped: a leg is missing"); return
    tre, out, last = {o["d"]: o["v"] for o in t["obs"]}, [], None
    for o in m["obs"]:
        if o["d"] in tre:
            last = tre[o["d"]]
        else:
            for p in reversed(t["obs"]):
                if p["d"] <= o["d"]:
                    last = p["v"]; break
        if last is not None:
            out.append({"d": o["d"], "v": round((o["v"] - last) * 100)})
    out = to_monthly(out)
    if out:
        series["SPREAD"] = {
            "name": "Mortgage spread over 10Y", "bucket": "demand", "lead": 3,
            "units": "bp", "good": "down", "sid": "SPREAD",
            "release_name": "Computed", "obs": out,
            "ref": {"v": 170, "label": "historical norm"},
            "read": "Historically near 170bp and far wider since 2022. Compression "
                    "improves affordability with no Fed move at all.",
        }


def pull_release_calendar(release_ids):
    """Authoritative future publication dates, straight from FRED's own calendar."""
    cal = []
    end = (TODAY + datetime.timedelta(days=150)).isoformat()
    try:
        r = fred("/releases/dates", realtime_start=TODAY.isoformat(), realtime_end=end,
                 include_release_dates_with_no_data="true", sort_order="asc", limit=1000)
        seen = set()
        for d in r.get("release_dates", []):
            rid = d.get("release_id")
            if rid not in release_ids or rid in seen:
                continue
            seen.add(rid)
            dt = datetime.date.fromisoformat(d["date"])
            cal.append({"name": release_ids[rid] or d.get("release_name", ""),
                        "date": d["date"],
                        "days": (dt - TODAY).days})
    except Exception as e:
        note("Release calendar failed: %s" % e)
    cal.sort(key=lambda x: x["days"])
    return cal

# ---------------------------------------------------------------------------
# SEC EDGAR - public homebuilders
# ---------------------------------------------------------------------------
BUILDERS = ["DHI", "LEN", "PHM", "NVR", "TOL", "KBH", "MTH", "TMHC", "TPH", "CCS", "LGIH", "MHO"]
INVENTORY_TAGS = ["InventoryRealEstate", "InventoryOperativeBuilders",
                  "RealEstateInventoryConstructionInProcess", "InventoryNet"]
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept": "application/json"}

# Yahoo Finance's chart endpoint. Unlike FRED and SEC EDGAR, this is genuinely
# unofficial: no published API, no key, no SLA, and it is known to change
# shape or rate-limit without notice. It remains the best available no-key
# option; Stooq's free CSV route now sits behind a CAPTCHA-issued key as of
# early 2026, which is unusable from an unattended script. A failure here
# surfaces in the errors list like any other source rather than breaking the
# run, and is the one part of this collector that could not be verified
# against a live response before shipping.
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

# Builders whose SEC reporting has ended for good, taken private by
# acquisition. Their CIK stays valid forever, since the SEC's historical
# record does not disappear when a ticker delists, so their final filing and
# full inventory history keep showing here; only the "expect a newer filing"
# assumption is switched off for them (see refresh_brief). Both drop out of
# SEC's live company_tickers.json once delisted, so their CIK is hardcoded
# and verified against the returned entity name rather than looked up.
DELISTED = {
    "TPH":  {"cik": "0001561680", "name": "Tri Pointe Homes",
             "note": "Acquired by Sumitomo Forestry, closed 2026-05-14"},
    "TMHC": {"cik": "0001562476", "name": "Taylor Morrison Home Corp",
             "note": "Acquired by Berkshire Hathaway, closed 2026-07-24"},
}

def pull_builders():
    rows = []
    try:
        tickers = get_json("https://www.sec.gov/files/company_tickers.json",
                           headers={"User-Agent": SEC_UA})
        lookup = {v["ticker"]: (str(v["cik_str"]).zfill(10), v["title"])
                  for v in tickers.values()}
    except Exception as e:
        note("SEC ticker index failed: %s" % e)
        return rows

    for tk in BUILDERS:
        delisted = tk in DELISTED
        expect = None
        if tk in lookup:
            cik, title = lookup[tk]
        elif delisted:
            cik, title = DELISTED[tk]["cik"], DELISTED[tk]["name"]
            expect = DELISTED[tk]["name"]
        else:
            note("No SEC record for ticker %s" % tk); continue
        rec = {"ticker": tk, "name": title, "cik": cik, "delisted": delisted}
        try:
            sub = get_json("https://data.sec.gov/submissions/CIK%s.json" % cik, headers=SEC_HEADERS)
            # The Submissions API's field is "name". "entityName" belongs to a
            # different SEC endpoint (CompanyFacts) and never appears here, so
            # using it made this check silently fail on every run.
            ent = sub.get("name", "")
            if expect and expect.lower() not in ent.lower():
                note("CIK %s is %s, not %s - skipped" % (cik, ent, tk)); continue
            if ent:
                rec["name"] = ent
            f = sub.get("filings", {}).get("recent", {})
            for i, form in enumerate(f.get("form", [])):
                if form in ("10-Q", "10-K"):
                    acc = f["accessionNumber"][i].replace("-", "")
                    rec["form"] = form
                    rec["filed"] = f["filingDate"][i]
                    rec["period"] = f.get("reportDate", [""] * (i + 1))[i]
                    rec["url"] = ("https://www.sec.gov/Archives/edgar/data/%s/%s/%s"
                                  % (int(cik), acc, f["primaryDocument"][i]))
                    break
        except Exception as e:
            note("SEC filings for %s failed: %s" % (tk, e))

        # inventory dollars, whichever XBRL tag this filer uses
        for tag in INVENTORY_TAGS:
            try:
                c = get_json("https://data.sec.gov/api/xbrl/companyconcept/CIK%s/us-gaap/%s.json"
                             % (cik, tag), headers=SEC_HEADERS)
                pts = [u for u in c.get("units", {}).get("USD", []) if u.get("form") in ("10-Q", "10-K")]
                pts.sort(key=lambda u: u["end"])
                if pts:
                    rec["inventory_tag"] = tag
                    rec["inventory"] = [{"d": p["end"], "v": p["val"]} for p in pts[-24:]]
                    break
            except Exception:
                continue
        rows.append(rec)
        print("  %-6s %s %s" % (tk, rec.get("form", "-"), rec.get("filed", "")))
        time.sleep(0.25)
    return rows

# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------
def pull_markets():
    out = []
    try:
        ev = get_json("https://gamma-api.polymarket.com/events"
                      "?tag_slug=housing&closed=false&limit=25",
                      headers={"User-Agent": SEC_UA})
        for e in (ev if isinstance(ev, list) else []):
            top = None
            for m in e.get("markets", []):
                p = m.get("outcomePrices")
                if isinstance(p, str):
                    try: p = json.loads(p)
                    except Exception: p = None
                if p:
                    v = float(p[0])
                    if not top or v > top["p"]:
                        top = {"q": m.get("groupItemTitle") or m.get("question") or "", "p": v}
            if e.get("title"):
                out.append({"title": e["title"], "vol": float(e.get("volume") or 0), "top": top})
        out.sort(key=lambda x: -x["vol"])
    except Exception as e:
        note("Polymarket failed: %s" % e)
    return out

# ---------------------------------------------------------------------------
# Builder brief
#
# Lot counts owned versus optioned, incentive load and regional commentary live
# in MD&A prose, not tagged XBRL, so they cannot be scraped. When an Anthropic
# key is present the collector reads each newly filed 10-Q and rewrites the
# brief. Without one it leaves the stored brief alone and flags on the sheet
# which builders have filed since it was written.
# ---------------------------------------------------------------------------
TAG_RE = None

def strip_html(raw):
    import re, html as htmlmod
    global TAG_RE
    if TAG_RE is None:
        TAG_RE = (re.compile(r"(?is)<(script|style|ix:header)[^>]*>.*?</\1>"),
                  re.compile(r"(?s)<[^>]+>"), re.compile(r"[ \t\r\f\v]+"),
                  re.compile(r"\n{3,}"))
    drop, tags, spaces, blanks = TAG_RE
    t = drop.sub(" ", raw)
    t = tags.sub(" ", t)
    t = htmlmod.unescape(t)
    t = spaces.sub(" ", t)
    return blanks.sub("\n\n", t).strip()


def anthropic(prompt, max_tokens=4000):
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.loads(r.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")


def parse_json_reply(txt):
    t = txt.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    a, b = t.find("{"), t.rfind("}")
    return json.loads(t[a:b+1]) if a >= 0 and b > a else None


NUMFIELDS = ["deliveries_units", "deliveries_yoy_pct", "orders_units", "orders_yoy_pct",
             "margin_pct", "cancel_rate_pct"]

EXTRACT_PROMPT = """You are reading one homebuilder's quarterly SEC filing. Extract only what the
filing itself states. Any figure not present in this text must be null. Never estimate,
never carry a number over from another company, never infer from general knowledge.

Company: {name} ({ticker})   Period ending: {period}

Return ONLY a JSON object, no prose and no code fence. Every "_pct" or "_units"
field must be a bare number (e.g. 20.7, not "20.7%" and not "20.7% adj") taken
directly from the filing, or null if the filing does not state it. Never
estimate or back a number out from a percentage; if only the percentage is
given, leave the unit field null.
{{
  "deliveries": "e.g. '2,662 (-10% YoY)' or null",
  "deliveries_units": "home closings this quarter as a bare integer, or null",
  "deliveries_yoy_pct": "YoY change in deliveries as a signed number, or null",
  "orders": "net new orders with YoY change, or null",
  "orders_units": "net new orders this quarter as a bare integer, or null",
  "orders_yoy_pct": "YoY change in net orders as a signed number, or null",
  "margin": "homebuilding gross margin percent, note if adjusted, or null",
  "margin_pct": "homebuilding gross margin as a bare number, or null",
  "cancels": "cancellation rate and its basis, or null",
  "cancel_rate_pct": "cancellation rate as a bare number, or null",
  "lots_owned": "number of lots owned, or null",
  "lots_optioned": "number of lots optioned or controlled, or null",
  "communities": "active selling community count or growth guidance, or null",
  "incentives": "incentive load as stated, or null",
  "backlog": "backlog units and value, or null",
  "california": "any statement specific to California or a California region. null if none.",
  "notes": "at most two sentences on land strategy or outlook, in the filing's own terms"
}}

FILING TEXT:
{text}
"""

SYNTH_PROMPT = """You are writing a quarterly homebuilder brief for a California land entitlement
consultant who works in Los Angeles and Ventura Counties. Below is structured data extracted
from each builder's latest filing, plus the previous edition of the brief for continuity.

Rules:
- Use only the extracted data. Do not introduce figures that are not in it.
- If nothing in the data speaks to Southern California, say so plainly rather than inventing it.
- No public builder discloses at county level, so never attribute a Ventura County claim to a
  filing. Carry forward the prior ventura section unless the new data genuinely changes it.
- Write plainly. No em dashes. Short direct sentences.

Return ONLY a JSON object, no prose and no code fence:
{{
  "headline": "one sentence, under 20 words",
  "national": ["3 to 4 paragraphs"],
  "land": ["2 to 4 paragraphs on lot positions and land strategy"],
  "socal": ["2 to 4 paragraphs, or one paragraph saying the filings are silent on it"],
  "ventura": ["carry forward or refine the prior section"],
  "takeaway": ["2 to 3 short paragraphs of implications for a land seller"]
}}

EXTRACTED DATA:
{data}

PREVIOUS BRIEF:
{prior}
"""


def load_brief():
    try:
        with open(BRIEF_PATH) as f:
            return json.load(f)
    except Exception:
        return {"as_of": None, "covered": {}, "national": [], "metrics": [],
                "land": [], "socal": [], "ventura": [], "takeaway": []}


def refresh_brief(builders):
    brief = load_brief()
    covered = brief.get("covered", {}) or {}

    fresh = [b for b in builders
             if b.get("period") and not b.get("delisted")
             and b["period"] > covered.get(b["ticker"], "")]

    if not fresh:
        brief["stale"] = []
        return brief

    names = ", ".join("%s (%s)" % (b["ticker"], b["period"]) for b in fresh)
    print("  new filings since last brief: %s" % names)

    if not ANTHROPIC_KEY:
        brief["stale"] = [{"ticker": b["ticker"], "period": b["period"], "url": b.get("url")}
                          for b in fresh]
        note("Brief is behind by %d filing(s): %s. Add ANTHROPIC_API_KEY to refresh "
             "it automatically, or ask for a manual read." % (len(fresh), names))
        return brief

    extracts = []
    for b in fresh:
        if not b.get("url"):
            continue
        try:
            req = urllib.request.Request(b["url"], headers={"User-Agent": SEC_UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            text = strip_html(raw.decode("utf-8", "ignore"))[:180000]
            got = parse_json_reply(anthropic(EXTRACT_PROMPT.format(
                name=b["name"], ticker=b["ticker"], period=b["period"], text=text), 2000))
            if got:
                got.update({"ticker": b["ticker"], "name": b["name"], "quarter": b["period"]})
                extracts.append(got)
                print("    read %s" % b["ticker"])
        except Exception as e:
            note("Could not read %s filing: %s" % (b["ticker"], e))
        time.sleep(1)

    if not extracts:
        note("No filings could be read; brief left unchanged.")
        brief["stale"] = [{"ticker": b["ticker"], "period": b["period"]} for b in fresh]
        return brief

    prior = {k: brief.get(k, []) for k in ("national", "land", "socal", "ventura", "takeaway")}
    try:
        new = parse_json_reply(anthropic(SYNTH_PROMPT.format(
            data=json.dumps(extracts, indent=1)[:120000],
            prior=json.dumps(prior)[:40000]), 6000))
    except Exception as e:
        note("Brief synthesis failed: %s" % e)
        return brief
    if not new:
        note("Brief synthesis returned nothing usable; keeping previous edition.")
        return brief

    # keep any builder row we did not refresh this run
    kept = [m for m in brief.get("metrics", [])
            if m.get("ticker") not in {e["ticker"] for e in extracts}]
    rows = kept + [{k: e.get(k) for k in
                    ("ticker", "name", "quarter", "deliveries", "orders", "margin", "cancels",
                     "lots_owned", "lots_optioned", "communities", "incentives")}
                   for e in extracts]

    # Append this quarter to each builder's numeric history rather than
    # overwrite it, so charts can plot a trend once more than one quarter
    # has been read. Keyed by quarter so a re-read of the same filing never
    # double-counts. Capped at eight quarters, about two years, per builder.
    history = brief.get("metric_history", {}) or {}
    for e in extracts:
        h = history.setdefault(e["ticker"], [])
        if not any(x.get("quarter") == e["quarter"] for x in h):
            h.append({"quarter": e["quarter"], **{k: e.get(k) for k in NUMFIELDS}})
            h.sort(key=lambda x: x["quarter"])
            history[e["ticker"]] = h[-8:]

    brief.update({
        "metric_history": history,
        "as_of": TODAY.isoformat(),
        "source": "auto",
        "headline": new.get("headline", brief.get("headline", "")),
        "national": new.get("national", brief.get("national", [])),
        "land": new.get("land", brief.get("land", [])),
        "socal": new.get("socal", brief.get("socal", [])),
        "ventura": new.get("ventura", brief.get("ventura", [])),
        "takeaway": new.get("takeaway", brief.get("takeaway", [])),
        "metrics": rows,
        "stale": [],
    })
    for e in extracts:
        covered[e["ticker"]] = e["quarter"]
    brief["covered"] = covered

    with open(BRIEF_PATH, "w") as f:
        json.dump(brief, f, indent=2)
    print("  brief rewritten from %d filing(s)" % len(extracts))
    return brief


# ---------------------------------------------------------------------------
# Stock prices
# ---------------------------------------------------------------------------
def pull_resale_data():
    """Existing-home resale market: median sale price and homes sold, at
    national, California, and Ventura County level, from one consistent
    source so the three levels are actually comparable to each other.

    Neither FRED nor SEC EDGAR carry closed-sale data below the national
    level; FRED's Ventura County series (via Realtor.com) is inventory and
    days-on-market only, not sales volume or price. Redfin's Data Center is
    the one free, no-key source confirmed to publish the same two metrics
    at all three levels: https://www.redfin.com/news/data-center/downloads

    Redfin rebuilt this Data Center in May 2026, unifying their monthly and
    weekly pipelines. Column names changed as part of that rebuild; the
    mapping below is read from Redfin's own published legacy-to-current
    column reference (redfin.com/news/data-center/methodology), not assumed.
    Column matching is case-insensitive and tries several known names per
    field, since this source has already changed shape once and may again.
    On any mismatch this logs the FULL header and a sample row rather than
    guessing, so a future failure is fixable from the log alone.
    """
    BASE = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/"
    TARGETS = [
        ("national", "national_market_tracker.tsv000.gz", lambda rt, rn: rt.strip().lower() == "national"),
        ("ca",       "state_market_tracker.tsv000.gz",    lambda rt, rn: rn.strip().lower() == "california"),
        ("ventura",  "county_market_tracker.tsv000.gz",   lambda rt, rn: "ventura" in rn.strip().lower() and "ca" in rn.strip().lower()),
    ]
    LABELS = {"national": "United States", "ca": "California", "ventura": "Ventura County, CA"}

    # Each field: acceptable header names, tried in order, matched
    # case-insensitively. First entry is the current (post-May-2026) name
    # per Redfin's own legacy-column-reference table; later entries are
    # older names, kept in case a run hits a not-yet-migrated file.
    COLS = {
        "period_end":  ["PERIOD_END", "period_end"],
        "region_type": ["REGION_TYPE", "region_type"],
        "region_name": ["REGION", "REGION_NAME", "region_name"],
        "price":       ["Median Sale Price NSA ($)", "Median Sale Price ($)", "median_sale_price"],
        "sold":        ["Homes Sold", "homes_sold"],
    }

    def find_col(header, candidates):
        lower = {h.strip().lower(): h for h in header}
        for cand in candidates:
            if cand.lower() in lower:
                return lower[cand.lower()]
        return None

    levels = []
    for key, fname, matcher in TARGETS:
        raw = None
        last_err = None
        attempts = 2 if key == "national" else 1  # national has 403'd before; worth one retry
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(BASE + fname, headers={"User-Agent": SEC_UA})
                with urllib.request.urlopen(req, timeout=120) as r:
                    raw = gzip.decompress(r.read())
                break
            except Exception as e:
                last_err = e
                if attempt < attempts - 1:
                    time.sleep(3)
        if raw is None:
            extra = (" Redfin rebuilt this Data Center in May 2026; if this persists, the "
                     "national file may have been renamed or retired as part of that change "
                     "rather than this being a transient block." if key == "national" else "")
            note("Resale %s failed: %s.%s" % (key, last_err, extra))
            time.sleep(0.5)
            continue
        try:
            text = raw.decode("utf-8", "replace")
            lines_ = text.splitlines()
            reader = csv.DictReader(lines_, delimiter="\t")
            header = reader.fieldnames or []

            resolved = {k: find_col(header, v) for k, v in COLS.items()}
            missing = [k for k, v in resolved.items() if v is None]
            if missing:
                sample = lines_[1][:300] if len(lines_) > 1 else "(no data rows)"
                note("Resale %s: could not find column(s) %s. Full header: %s | Sample row: %s"
                     % (key, missing, header, sample))
                continue
            c_end, c_type, c_region, c_price, c_sold = (
                resolved["period_end"], resolved["region_type"], resolved["region_name"],
                resolved["price"], resolved["sold"])

            price, sold = [], []
            seen_dates = set()
            for row in reader:
                if not matcher(row.get(c_type, ""), row.get(c_region, "")):
                    continue
                d = (row.get(c_end) or "")[:10]
                if not d or d in seen_dates:
                    continue
                try:
                    p = row.get(c_price, "")
                    h = row.get(c_sold, "")
                    if p not in ("", None):
                        price.append({"d": d, "v": round(float(p), 2)})
                    if h not in ("", None):
                        sold.append({"d": d, "v": round(float(h), 1)})
                    seen_dates.add(d)
                except ValueError:
                    continue
            price.sort(key=lambda o: o["d"]); sold.sort(key=lambda o: o["d"])
            if not price and not sold:
                note("Resale %s: file read, columns found, but no rows matched region %s (region values may have changed too)"
                     % (key, LABELS[key]))
                continue
            levels.append({"key": key, "label": LABELS[key],
                            "median_price": price[-96:], "homes_sold": sold[-96:]})
            print("  %-9s %4d price pts, %4d sold pts" % (key, len(price), len(sold)))
        except Exception as e:
            note("Resale %s failed: %s" % (key, e))
        time.sleep(0.5)

    return {"as_of": TODAY.isoformat(), "levels": levels,
            "source": "Redfin Data Center, redfin.com/news/data-center/downloads"}


def pull_stock_prices():
    out = []
    for tk in BUILDERS:
        try:
            j = get_json("https://query1.finance.yahoo.com/v8/finance/chart/%s"
                         "?range=ytd&interval=1d" % tk, headers=YAHOO_HEADERS)
            res = (j.get("chart") or {}).get("result") or []
            if not res:
                note("No price data returned for %s" % tk); continue
            res = res[0]
            ts = res.get("timestamp") or []
            closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
            if not pairs:
                note("Price series for %s came back empty" % tk); continue
            series = [{"d": datetime.datetime.utcfromtimestamp(t).date().isoformat(), "v": round(c, 2)}
                      for t, c in pairs]
            meta = res.get("meta") or {}
            first, last = series[0]["v"], series[-1]["v"]
            ytd_pct = round((last - first) / first * 100, 1) if first else None
            out.append({
                "ticker": tk,
                "price": meta.get("regularMarketPrice", last),
                "ytd_pct": ytd_pct,
                "as_of": series[-1]["d"],
                "series": series,
                "currency": meta.get("currency", "USD"),
                "delisted": tk in DELISTED,
            })
            print("  %-6s $%.2f  YTD %s%%" % (tk, meta.get("regularMarketPrice", last),
                  ytd_pct if ytd_pct is not None else "?"))
        except Exception as e:
            note("Stock price for %s failed: %s" % (tk, e))
        time.sleep(0.4)
    return out


# ---------------------------------------------------------------------------
def main():
    if not FRED_KEY:
        print("FRED_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.",
              file=sys.stderr)
        sys.exit(1)

    print("FRED series...");     series, releases = pull_fred()
    add_spread(series)
    print("Release calendar..."); calendar = pull_release_calendar(releases)
    print("SEC EDGAR...");        builders = pull_builders()
    print("Polymarket...");       markets  = pull_markets()
    print("Builder brief...");   brief    = refresh_brief(builders)
    print("Resale market...");   resale   = pull_resale_data()
    print("Stock prices...");    stocks   = pull_stock_prices()

    payload = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "series": series,
        "rates": [r for r in RATES if r in series],
        "calendar": calendar,
        "builders": builders,
        "brief": brief,
        "stocks": stocks,
        "resale": resale,
        "markets": markets,
        "errors": ERRORS,
    }
    d = os.path.dirname(OUT_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    ok = sum(1 for s in series.values() if s["obs"])
    print("\nWrote %s - %d/%d series, %d builders, %d markets, %d errors"
          % (OUT_PATH, ok, len(series), len(builders), len(markets), len(ERRORS)))

if __name__ == "__main__":
    main()
