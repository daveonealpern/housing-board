#!/usr/bin/env python3
"""
Housing Signal Board - data collector.

Runs on a schedule in GitHub Actions. Pulls every source, writes data.json.
Nothing here needs a human. If a source fails it is recorded in data.json
under "errors" and shows on the page, rather than disappearing quietly.
"""

import os, sys, json, time, datetime, urllib.request, urllib.parse, urllib.error

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
SEC_UA   = os.environ.get("SEC_USER_AGENT", "").strip() or "Housing Signal Board research contact@example.com"
FRED_BASE = "https://api.stlouisfed.org/fred"
OUT_PATH  = os.environ.get("OUT_PATH", "data.json")
TODAY = datetime.date.today()
START = (TODAY - datetime.timedelta(days=365 * 8)).isoformat()

ERRORS = []
def note(msg):
    ERRORS.append(msg)
    print("  ! " + msg, file=sys.stderr)

def get_json(url, headers=None, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": SEC_UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
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
    ("AUTHNOTT",   "Permits authorized, not started", "structural",  9, "K units",      "down",
     "A swelling backlog means builders hold entitlements and are choosing not to break ground.",
     "housing units authorized not started"),
    ("PERMIT",     "Building permits, total",         "structural",  7, "K units SAAR", "up",
     "The front of the construction pipeline. Turns before starts by one to two quarters.",
     "new private housing units authorized by building permits"),
    ("PERMIT1",    "Single-family permits",           "structural",  7, "K units SAAR", "up",
     "Cleaner than the total, which multifamily swings distort.",
     "new private housing units authorized single family"),
    ("HOUST1F",    "Single-family housing starts",    "structural",  4, "K units SAAR", "up",
     "Where permits become dirt. Compare against permits to see the conversion rate.",
     "privately owned housing starts single family"),
    ("HOUST",      "Housing starts, total",           "structural",  4, "K units SAAR", "up",
     "Total starts including multifamily, which swings hard on a few large projects.",
     "housing starts total new privately owned"),

    # --- demand and credit ---
    ("MORTGAGE30US", "30-year fixed mortgage rate",   "demand",      3, "%",            "down",
     "The affordability lever. Direction and speed matter more than the level.",
     "30-year fixed rate mortgage average"),
    ("DGS10",        "10-year Treasury",              "demand",      3, "%",            "down",
     "The base the mortgage rate prices off. Watch the gap between the two.",
     "10-year treasury constant maturity rate"),
    ("UMCSENT",      "Consumer sentiment",            "demand",      3, "index",        "up",
     "Headline sentiment is a weak housing signal on its own, but it turns early.",
     "university of michigan consumer sentiment"),
    ("TDSP",         "Household debt service ratio",  "demand",      4, "%",            "down",
     "How stretched household budgets already are before a payment shock.",
     "household debt service payments percent disposable income"),

    # --- activity now ---
    ("HSN1F",         "New home sales",               "coincident",  0, "K SAAR",       "up",
     "Recorded at contract signing, so slightly ahead of existing sales.",
     "new one family houses sold united states"),
    ("MSACSR",        "Months supply, new homes",     "coincident",  1, "months",       "down",
     "Above roughly six months has historically preceded builder discounting.",
     "monthly supply of new houses"),
    ("EXHOSLUSM495S", "Existing home sales",          "coincident", -1, "K SAAR",       "up",
     "Records at closing, so it reflects decisions made a month or two earlier.",
     "existing home sales"),
    ("COMPUTSA",      "Housing completions",          "coincident", -2, "K SAAR",       "up",
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
    ("JTS2300JOL",  "Construction job openings",      "cost",        2, "K",            "up",
     "Hiring intent turns before payrolls. Better labor read than employment level.",
     "job openings construction"),
    ("USCONS",      "Construction employment",        "cost",        0, "K",            "up",
     "Coincident. Useful mainly as a check on the openings series.",
     "all employees construction"),
    ("PRRESCONS",   "Residential construction spend", "cost",       -1, "$M SAAR",      "up",
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
                obs = [{"d": o["date"], "v": float(o["value"])}
                       for o in r.get("observations", []) if o["value"] not in (".", "")]
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
SEC_HEADERS = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}

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
        if tk not in lookup:
            note("No SEC record for ticker %s" % tk); continue
        cik, title = lookup[tk]
        rec = {"ticker": tk, "name": title, "cik": cik}
        try:
            sub = get_json("https://data.sec.gov/submissions/CIK%s.json" % cik, headers=SEC_HEADERS)
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
def main():
    if not FRED_KEY:
        print("FRED_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.",
              file=sys.stderr)
        sys.exit(1)

    print("FRED series...");     series, releases = pull_fred()
    print("Release calendar..."); calendar = pull_release_calendar(releases)
    print("SEC EDGAR...");        builders = pull_builders()
    print("Polymarket...");       markets  = pull_markets()

    payload = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "series": series,
        "calendar": calendar,
        "builders": builders,
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
