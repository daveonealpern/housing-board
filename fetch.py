#!/usr/bin/env python3
"""
Housing Signal Board - data collector.

Runs on a schedule in GitHub Actions. Pulls every source, writes data.json.
Nothing here needs a human. If a source fails it is recorded in data.json
under "errors" and shows on the page, rather than disappearing quietly.
"""

import os, sys, json, time, gzip, csv, re, datetime, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
SEC_UA   = os.environ.get("SEC_USER_AGENT", "").strip() or "Housing Signal Board research contact@example.com"
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip()

# Credit status for the two paid APIs, shown on both pages so a top-up is
# never missed. status: ok, low, out, limit, rejected, error or off (no key).
BILLING = {"anthropic": {"status": "off"}, "x": {"status": "off"}}
X_LOW_BALANCE = 2.00          # USD; below this the pages say X credits are running low

def set_billing(service, status, message="", **extra):
    BILLING[service] = {"status": status, "message": message, "checked": TODAY.isoformat(), **extra}
    print("  billing %s: %s %s" % (service, status, message))

# Anthropic answers an empty account or a reached spend limit with HTTP 400 or
# 429 and these phrases in the error message.
ANTHROPIC_CREDIT_RX = re.compile(r"credit balance is too low|purchase credits|plans? (&|and) billing", re.I)
ANTHROPIC_LIMIT_RX = re.compile(r"usage limit|spend(ing)? limit|regain access", re.I)

def classify_anthropic_error(code, body):
    if code == 401:
        set_billing("anthropic", "rejected", "The Claude API key was rejected. Create a new key in the "
                    "Claude Console and replace the ANTHROPIC_API_KEY secret in GitHub.")
    elif ANTHROPIC_CREDIT_RX.search(body):
        set_billing("anthropic", "out", "The daily narrative and filing reads are paused until credits "
                    "are added.")
    elif ANTHROPIC_LIMIT_RX.search(body):
        m = re.search(r"regain access on ([^.]+)", body)
        set_billing("anthropic", "limit", "The Claude Console spend limit has been reached%s. Raise the "
                    "limit or wait for it to reset." % (" (access returns %s)" % m.group(1) if m else ""))
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
#   - permits/starts/new home sales/completions/employment are reported in thousands
#   - existing home sales (EXHOSLUSM495S) is the exception: FRED reports it in
#     whole units (about 4,000,000), so it takes no multiplier. It was scaled
#     by 1,000 here until 2026-11-13, which showed 3.75 billion sales a year.
#   - construction spend is reported in millions of dollars
#   - BAML spread series are reported in PERCENT, not basis points, despite
#     the market convention of quoting credit spreads in bp
SCALE = {
    "AUTHNOTT": 1000, "PERMIT": 1000, "PERMIT1": 1000, "HOUST1F": 1000, "HOUST": 1000,
    "HSN1F": 1000, "COMPUTSA": 1000,
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
     "The long end of the curve. Matters more for the long-term mortgage that eventually replaces a construction loan than for the construction loan itself.",
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
     "The quarterly-reset equivalent. Use this one if your loan's rate resets every quarter rather than monthly.",
     "90-day average sofr"),
    ("DPRIME",       "Bank prime loan rate",          "capital",     3, "%",            "down",
     "What smaller land and construction loans actually price off. Moves in lockstep with the fed funds rate, historically about 300bp above it.",
     "bank prime loan rate"),
    ("FEDFUNDS",     "Federal funds rate",            "capital",     4, "%",            "down",
     "The policy rate. Housing responds to the long end, but this sets the tone.",
     "federal funds effective rate"),
    ("MORTGAGE15US", "15-year fixed mortgage",        "capital",     3, "%",            "down",
     "The gap to the 30-year shows how much of the curve borrowers are paying for.",
     "15-year fixed rate mortgage average"),
    ("BAMLC0A4CBBB", "BBB corporate spread",          "capital",     6, "bp",           "down",
     "Investment-grade credit stress. When this widens, it gets more expensive for builders to draw on their revolving credit lines, tightening their cash position before it ever shows up in a housing number.",
     "ice bofa bbb us corporate index option-adjusted spread"),
    ("BAMLH0A0HYM2", "High yield spread",             "capital",     6, "bp",           "down",
     "The risk appetite gauge. Blows out first when credit turns.",
     "ice bofa us high yield index option-adjusted spread"),

    # --- activity now ---
    ("HSN1F",         "New home sales",               "coincident",  0, "units SAAR",       "up",
     "Recorded at contract signing, so slightly ahead of existing sales.",
     "new one family houses sold united states"),
    ("MSACSR",        "Months supply, new homes",     "coincident",  1, "months",       "down",
     "How many months it would take to sell every new home currently on the market at the current sales pace. A rising number means inventory is building up faster than it is selling; above roughly six months has historically preceded builders cutting prices to move it.",
     "monthly supply of new houses"),
    ("EXHOSLUSM495S", "Existing home sales",          "coincident", -1, "units SAAR",       "up",
     "Previously owned homes changing hands, the resale market, as opposed to new construction. "
     "Counted at closing, so a home that went under contract in one month often does not show "
     "up here until the next, a month or two after the buyer actually decided.",
     "existing home sales"),
    ("COMPUTSA",      "Housing completions",          "coincident", -2, "units SAAR",       "up",
     "Supply arriving now from starts twelve to eighteen months ago.",
     "new privately owned housing units completed"),
    ("MSPUS",         "Median sales price, US homes", "coincident", -2, "$",            "up",
     "Mix-sensitive. A falling median can mean cheaper homes selling, not falling values.",
     "median sales price of houses sold"),
    ("CSUSHPINSA",    "Case-Shiller national",        "coincident", -3, "index",        "up",
     "An index of home prices nationally, tracking the same houses as they resell over time rather than a raw median of whatever happened to sell. Built from a three-month moving average and published two months late, so it confirms a price turn well after it already happened.",
     "s&p case-shiller u.s. national home price index"),

    # --- cost and labor ---
    ("WPUSI012011", "Construction materials PPI",     "cost",        1, "index",        "down",
     "Reaches a builder's cost projections before it reaches their completed, as-built cost.",
     "producer price index construction materials"),
    ("JTS2300JOL",  "Construction job openings",      "cost",        2, "jobs",            "up",
     "Hiring intent turns before payrolls. Better labor read than employment level.",
     "job openings construction"),
    ("USCONS",      "Construction employment",        "cost",        0, "jobs",            "up",
     "How many people are actually on construction payrolls, as opposed to job openings, which measures hiring intent. Useful mainly as a check on whether posted openings are actually turning into hires.",
     "all employees construction"),
    ("PRRESCONS",   "Residential construction spend", "cost",       -1, "$ SAAR",      "up",
     "The dollar value of construction work actually completed in the period, not contracts signed or work planned, so it reflects work already underway.",
     "total private construction spending residential"),

    # --- distress ---
    ("DRSFRMACBS",     "SF mortgage delinquency",     "distress",   -8, "%",            "down",
     "Lags badly but confirms turns. Watch the rate of change, not the level.",
     "delinquency rate single-family residential mortgages commercial banks"),
    ("DRCRELEXFACBS",  "CRE loan delinquency",        "distress",   -7, "%",            "down",
     "Banks classify loans on income-producing properties like apartment buildings, offices, "
     "and retail centers as commercial real estate, separate from a single-family home "
     "mortgage. Apartment buildings are the sub-segment to watch here: many broke ground in "
     "the same building boom and are finishing construction around the same time, so a wave "
     "of new units ends up competing for renters in the same local markets at once.",
     "delinquency rate commercial real estate loans banks"),

    # --- california ---
    ("CABPPRIV", "California permits authorized",     "ca",          7, "units",        "up",
     "State pipeline. Compare against national permits to see if California is diverging.",
     "new private housing units authorized california"),
    ("CASTHPI",  "California house price index",      "ca",         -3, "index",        "up",
     "A home price index for California specifically, built from every recorded sale and refinance appraisal the regulator can access, not just a sample. Published quarterly, slower to update than Case-Shiller but consistent over time.",
     "all-transactions house price index for california"),
    ("LXXRSA",   "Los Angeles Case-Shiller",          "ca",         -3, "index",        "up",
     "Your metro, same two-month lag as the national index.",
     "s&p case-shiller ca-los angeles home price index"),
    ("CAUR",     "California unemployment",           "ca",         -1, "%",            "down",
     "Context for how fast homes actually sell. Rising unemployment shrinks the pool of qualified buyers before prices move.",
     "unemployment rate in california"),
    ("DCOILWTICO",   "Crude oil, WTI",                 "commodity",   1, "$",     "down",
     "The US benchmark oil price. Feeds construction costs indirectly, through diesel for "
     "equipment and trucking, asphalt for paving, and the plastics and adhesives used "
     "throughout a home, more than through any single material directly.",
     "wti crude oil price"),
    ("WPU081",       "Lumber",                         "commodity",   2, "index", "down",
     "Producer price index for softwood and hardwood lumber. The most volatile major input "
     "in home construction: framing lumber prices have swung by more than half within a "
     "single year before, driven by mill capacity, tariffs, and wildfire or beetle damage "
     "to timber supply.",
     "producer price index lumber"),
    ("WPU101704",    "Steel, structural",              "commodity",   2, "index", "down",
     "Producer price index for hot rolled steel bars, plates, and structural shapes, the "
     "form steel takes in a building's frame and rebar rather than sheet steel used in cars "
     "or appliances.",
     "producer price index hot rolled steel bars plates structural shapes"),
    ("WPUSI019011",  "Copper",                         "commodity",   2, "index", "down",
     "Producer price index across copper and copper products. Runs through electrical "
     "wiring and plumbing pipe and fittings; a global industrial metal, so it moves with "
     "worldwide demand and mine supply more than with US housing activity specifically.",
     "producer price index copper and copper products"),
    # Horton's 10-K names lumber, drywall and cement as the largest material
    # components of a home, so the board carries concrete and gypsum beside
    # lumber, plus plastics for pipe, siding and insulation board. IDs checked
    # against FRED on 2026-11-19 (all report through October 2026).
    ("WPU1333",      "Ready-mix concrete",             "commodity",   2, "index", "down",
     "Producer price index for ready-mix concrete, the slab, footings and flatwork of a "
     "house. Moves with cement, aggregate and diesel costs and rarely falls.",
     "producer price index ready-mix concrete"),
    ("WPU137",       "Gypsum products (drywall)",      "commodity",   2, "index", "down",
     "Producer price index for gypsum products, mostly wallboard. A large-volume interior "
     "material whose plants run on natural gas.",
     "producer price index gypsum products"),
    ("WPU0721",      "Plastic construction products",  "commodity",   2, "index", "down",
     "Producer price index for plastic pipe, siding, flooring and foam board. Tracks resin "
     "prices, which follow oil and natural gas.",
     "producer price index plastic construction products"),
]

# A concrete, illustrative scenario per series, shown on hover alongside the
# standing note above. Written to make the mechanism tangible, not just
# restate the definition; several exist specifically because the underlying
# concept (a yield curve inversion, a completion wave, put-in-place spend)
# does not explain itself from the name alone.
EXAMPLES = {
    "AUTHNOTT": "For example: a builder pulls permits for a 200-home phase but only breaks "
                "ground on 140 this quarter. The other 60 sit in this backlog until they "
                "either start construction or the permit expires.",
    "PERMIT": "For example: a jump here in March typically shows up as more dirt turning "
              "in housing starts by around May or June.",
    "PERMIT1": "For example: one large apartment complex getting a single permit for 300 "
               "units can swing the total-permits number sharply in a month; this series "
               "strips that out.",
    "HOUST1F": "For example: if permits ran at 100,000 last month but starts only hit "
               "80,000, roughly 20,000 homes are sitting between paperwork and groundbreaking.",
    "HOUST": "For example: one large 400-unit apartment tower breaking ground can move this "
             "number more than a hundred small single-family starts combined.",
    "MORTGAGE30US": "For example: on a $400,000 loan, moving from 6.5% to 7.0% adds roughly "
                    "$130 to the monthly payment, enough to price some buyers out entirely.",
    "UMCSENT": "For example: sentiment can drop sharply after a bad headline even while "
               "people keep buying homes at the same pace, which is why it counts as a weak "
               "signal on its own.",
    "TDSP": "For example: when this ratio is already elevated, a small rise in rates or a "
            "job loss leaves far less room to absorb before a household falls behind on "
            "payments.",
    "T10Y2Y": "For example: in 2022 this curve went negative for the first time in years, "
              "correctly flagging elevated recession risk roughly a year ahead of when "
              "growth actually slowed.",
    "DGS2": "For example: this yield tends to rise when the market expects the Fed to keep "
            "raising rates, and fall once cuts are expected.",
    "DGS10": "For example: the 30-year mortgage rate usually runs about 1.5 to 2.5 points "
             "above this yield; a mortgage rate far outside that range signals something "
             "else, like spread widening, is going on.",
    "DGS30": "For example: a builder using construction-to-permanent financing cares more "
             "about where this yield sits at the time of the permanent take-out loan than "
             "at groundbreaking.",
    "SOFR": "For example: a construction loan priced at \"SOFR plus 250bp\" moves "
            "dollar-for-dollar with this rate every time it resets.",
    "SOFR30DAYAVG": "For example: a loan that resets monthly and references 1-month Term "
                    "SOFR will track very close to this series, since it is the closest "
                    "free public substitute.",
    "SOFR90DAYAVG": "For example: a facility that resets every quarter will track this "
                    "series more closely than the overnight or 30-day figures.",
    "DPRIME": "For example: a small local construction loan quoted at \"prime plus 1%\" "
              "moves in direct lockstep with this number.",
    "FEDFUNDS": "For example: a quarter-point Fed cut here does not guarantee the 30-year "
                "mortgage rate moves at all in the short run, since mortgage pricing follows "
                "the 10-year Treasury more closely.",
    "MORTGAGE15US": "For example: a 15-year loan often carries a rate half a point or more "
                    "below the 30-year on the same day, reflecting the shorter payoff period.",
    "BAMLC0A4CBBB": "For example: if this widens by 50bp, a builder's next bond issuance or "
                    "credit facility renewal likely gets priced meaningfully more expensively, "
                    "even if Treasury yields have not moved at all.",
    "BAMLH0A0HYM2": "For example: this spread often jumps well before equity markets sell "
                    "off, since credit investors tend to reprice risk first.",
    "HSN1F": "For example: a buyer signing a contract on a to-be-built home in March counts "
             "here immediately, even though the sale will not close for months.",
    "MSACSR": "For example: at 10,000 homes for sale and 1,500 selling per month, that is "
              "roughly 6.7 months of supply, a level that has historically preceded builders "
              "cutting prices.",
    "EXHOSLUSM495S": "For example: a home that goes under contract in March typically does "
                     "not count here until the sale closes in April or May.",
    "COMPUTSA": "For example: a wave of apartment buildings that broke ground in a hot "
                "construction year will show up as completions here roughly twelve to "
                "eighteen months later, arriving on the market around the same time.",
    "MSPUS": "For example: if builders shift toward more starter homes and fewer luxury "
             "homes, this median can fall even while every single home sells for more than "
             "it did last year.",
    "CSUSHPINSA": "For example: because this uses a three-month moving average reported two "
                  "months late, a price turn that already happened in July might not clearly "
                  "show up here until October.",
    "WPUSI012011": "For example: a spike in lumber or steel prices here typically shows up "
                   "in a builder's cost projections before it ever appears in their actual "
                   "completed-home costs.",
    "JTS2300JOL": "For example: a builder posting more openings for framers and electricians "
                  "this month is signaling planned activity before a single new hire shows "
                  "up on payroll.",
    "USCONS": "For example: this confirms whether the hiring intent shown by job openings "
              "actually turned into people getting hired.",
    "PRRESCONS": "For example: this counts the dollar value of a foundation actually poured "
                 "this month, not the total contract value of a home that will take a year "
                 "to build.",
    "DRSFRMACBS": "For example: this rate was still near normal levels well after the 2008 "
                  "housing bust had already begun, since missed payments take months to show "
                  "up as a formally delinquent loan.",
    "DRCRELEXFACBS": "For example: a 300-unit apartment building takes out a commercial loan "
                     "to get built, then opens into a neighborhood where several similar "
                     "buildings just finished too. All of them are chasing the same renters, "
                     "so it fills more slowly than planned, rents come under pressure to "
                     "attract tenants, and the building's income falls short of what its loan "
                     "payment assumed.",
    "CABPPRIV": "For example: if California's permit count falls while the national number "
                "holds steady, that is a sign the state's slowdown is more severe, or "
                "starting earlier, than the rest of the country.",
    "CASTHPI": "For example: this index is built from repeat sales of the same homes over "
               "time, so it tracks value changes more cleanly than a raw median price, which "
               "mixes in whatever happened to sell that quarter.",
    "LXXRSA": "For example: Los Angeles can diverge from the statewide CASTHPI number if the "
              "coastal metro is cooling while inland California markets stay hot, or the "
              "other way around.",
    "CAUR": "For example: a local employer announcing layoffs shrinks the pool of people who "
            "can qualify for a mortgage in that area, months before it shows up as softer "
            "home sales.",
    "DCOILWTICO": "For example: a $20 jump in the price of a barrel of oil raises the diesel "
                  "cost of running excavators and delivery trucks on a job site, and pushes up "
                  "asphalt prices for driveways and roads, without a single stick of lumber "
                  "changing price.",
    "WPU081": "For example: lumber prices roughly tripled in a few months during 2021, adding "
              "an estimated $20,000 or more to the cost of building a typical new single-family "
              "home before falling back just as fast.",
    "WPU101704": "For example: a builder pricing out a large custom home with steel beam "
                 "supports gets a materials quote that is only valid for 30 days, because the "
                 "mill's own price can move meaningfully before the steel is even delivered.",
    "WPU1333": "For example: a slab-on-grade tract home pours roughly 50 to 80 cubic yards "
               "of concrete across foundation, driveway and walks, so a few dollars a yard "
               "repeats across every lot in a phase.",
    "WPU137": "For example: a 2,500 square foot house hangs on the order of 8,000 to 10,000 "
              "square feet of drywall, and wallboard makers pass gas-price swings through "
              "within a quarter or two.",
    "WPU0721": "For example: PVC drain and supply pipe, vinyl siding and foam sheathing all "
               "reprice with resin, so an oil or gas spike reaches the house through plastics "
               "as well as fuel.",
    "WPUSI019011": "For example: a spike in global copper prices, driven by demand from data "
                   "centers or electric vehicles rather than anything happening in US housing, "
                   "still raises the cost of wiring a new home.",
}

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
        if sid in EXAMPLES:
            rec["example"] = EXAMPLES[sid]
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
BUILDERS = ["DHI", "LEN", "PHM", "NVR", "TOL", "KBH", "MTH", "CCS", "LGIH", "MHO"]
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

# Tri Pointe Homes (acquired by Sumitomo Forestry, closed 2026-05-14) and
# Taylor Morrison (acquired by Berkshire Hathaway, closed 2026-07-24) are
# intentionally absent from BUILDERS above. Earlier versions of this board
# kept them visible with a "delisted" label and their final filing, on the
# reasoning that historical footprint was useful context. On review that
# read as showing companies that no longer exist as if they still competed
# in this market, so they are dropped outright rather than flagged. Their
# acquirers are not tracked in their place: neither Sumitomo Forestry nor
# Berkshire Hathaway is a pure-play homebuilder, and folding a conglomerate
# into a peer group built for comparing builder-to-builder metrics would
# make every comparison on this board less meaningful, not more complete.

EIGHTK_ITEMS = {"1.01": "material agreement", "1.02": "agreement terminated", "2.01": "acquisition or sale",
                "2.02": "results of operations", "2.03": "new debt", "2.05": "restructuring costs",
                "2.06": "impairment", "3.02": "unregistered share sale", "5.02": "officer or director change",
                "5.03": "bylaw change", "5.07": "shareholder vote", "7.01": "Reg FD disclosure",
                "8.01": "other events"}

def recent_8ks(cik, f, days=21):
    """8-Ks filed in the last few weeks from a submissions 'recent' block: the
    official channel for earnings dates, guidance, deals and executive moves,
    usually the same day a press release goes out."""
    out = []
    cutoff = (TODAY - datetime.timedelta(days=days)).isoformat()
    for i, form in enumerate(f.get("form", [])):
        if form not in ("8-K", "8-K/A"):
            continue
        filed = f["filingDate"][i]
        if filed < cutoff:
            break                      # the recent block is newest first
        items = [x.strip() for x in (f.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
        acc = f["accessionNumber"][i]
        out.append({"form": form, "filed": filed, "acc": acc,
                    "items": [x for x in items if x != "9.01"],
                    "item_names": [EIGHTK_ITEMS.get(x, x) for x in items if x != "9.01"],
                    "url": "https://www.sec.gov/Archives/edgar/data/%s/%s/%s"
                           % (int(cik), acc.replace("-", ""), f["primaryDocument"][i])})
    return out[:6]

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
            # The Submissions API's field is "name". "entityName" belongs to a
            # different SEC endpoint (CompanyFacts) and never appears here, so
            # using it made an earlier version of this check silently fail.
            ent = sub.get("name", "")
            if ent:
                rec["name"] = ent
            f = sub.get("filings", {}).get("recent", {})
            rec["recent_8k"] = recent_8ks(cik, f)
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

def pull_filings(tickers):
    """Latest 10-Q or 10-K for each ticker, with the same fields as builders."""
    rows = []
    try:
        idx = get_json("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA})
        lookup = {v["ticker"]: (str(v["cik_str"]).zfill(10), v["title"]) for v in idx.values()}
    except Exception as e:
        note("SEC ticker index failed: %s" % e)
        return rows
    for tk in tickers:
        if tk not in lookup:
            note("No SEC record for ticker %s" % tk); continue
        cik, title = lookup[tk]
        rec = {"ticker": tk, "name": title, "cik": cik}
        try:
            sub = get_json("https://data.sec.gov/submissions/CIK%s.json" % cik, headers=SEC_HEADERS)
            rec["name"] = sub.get("name") or title
            f = sub.get("filings", {}).get("recent", {})
            rec["recent_8k"] = recent_8ks(cik, f)
            for i, form in enumerate(f.get("form", [])):
                if form in ("10-Q", "10-K"):
                    acc = f["accessionNumber"][i].replace("-", "")
                    rec.update({"form": form, "filed": f["filingDate"][i],
                                "period": f.get("reportDate", [""] * (i + 1))[i],
                                "url": "https://www.sec.gov/Archives/edgar/data/%s/%s/%s"
                                       % (int(cik), acc, f["primaryDocument"][i])})
                    break
        except Exception as e:
            note("SEC filings for %s failed: %s" % (tk, e))
        rows.append(rec)
        print("  %-6s %s %s" % (tk, rec.get("form", "-"), rec.get("filed", "")))
        time.sleep(0.25)
    return rows


LANDBANK_PROMPT = """You are reading the latest quarterly or annual SEC filing of {name} ({ticker}),
a land banker or lot developer that holds land for homebuilders. Extract only what the filing
states. Never estimate. No em dashes.

Return ONLY a JSON object, no prose and no code fence:
{{
  "period": "the period in words, e.g. 'Sep 2026 qtr' or 'FY ended Sep 2026'",
  "facts": ["4 to 5 short sentences, each with one hard figure: homesites or lots held,
             capital invested and yields or option fees, lots or land sold and to whom,
             impairments or deposit write-offs, guidance. Anchor time to fixed periods."]
}}

FILING TEXT:
{text}
"""

def filing_text(url, limit=180000):
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return strip_html(raw.decode("utf-8", "ignore"))[:limit]


def refresh_landbanks(brief, filings):
    """With an API key, re-read any land banker whose filing is newer than the
    facts on file and replace its facts. Without a key the facts simply keep
    their period label; there is no warning, since the board still works."""
    lb = brief.get("landbanks")
    if not lb or not ANTHROPIC_KEY:
        return brief
    covered = lb.setdefault("covered", {})
    changed = False
    for f in filings:
        if not f.get("url") or not f.get("period") or f["period"] <= covered.get(f["ticker"], ""):
            continue
        try:
            got = parse_json_reply(anthropic(LANDBANK_PROMPT.format(
                name=f["name"], ticker=f["ticker"], text=filing_text(f["url"])), 1500))
        except Exception as e:
            note("Could not read %s filing: %s" % (f["ticker"], e)); continue
        if not got or not got.get("facts"):
            continue
        for row in lb.get("public", []):
            if row.get("ticker") == f["ticker"]:
                row.update({"period": got.get("period") or row.get("period"),
                            "facts": got["facts"][:5], "url": f["url"]})
        covered[f["ticker"]] = f["period"]
        changed = True
        print("    read %s" % f["ticker"])
    if changed:
        lb["as_of"] = TODAY.isoformat()
        with open(BRIEF_PATH, "w") as fh:
            json.dump(brief, fh, indent=2)
    return brief


# ---------------------------------------------------------------------------
# News for the land banker, California private builder and cost panels
# ---------------------------------------------------------------------------
# Google News search RSS is the only free source that covers trade press,
# business journals and local papers together. It rate-limits GitHub's
# runners now and then, so each query retries, then falls back to Bing's news
# RSS, and if both fail the previous run's items are kept. A feed is only
# reported as an issue once it has been stale for more than three days.
# Google News honours "when:" only on plain OR chains (no parentheses), so the
# names go in small groups. Every item must name one of the builders in its
# title. The national names (Shea, Tri Pointe and the rest also build outside
# California) must also mention a California place or come from trade press,
# and place-names that double as builders (Rancho Mission Viejo) need a
# homebuilding word in the title. Checked against live Google results
# 2026-11-20.
CA_ONLY_BUILDERS = ["Trumark Homes", "City Ventures", "Olson Homes",
                    "Rancho Mission Viejo", "Melia Homes", "SummerHill Homes", "Pardee Homes",
                    "Brandywine Homes"]
NATIONAL_PRIVATE = ["Shea Homes", "Tri Pointe", "Brookfield Residential", "Risewell Homes",
                    "Taylor Morrison", "Woodside Homes", "Richmond American"]
CA_WORDS = re.compile(r"California|Calif\b|SoCal|Orange County|\bO\.?C\.?\b|Los Angeles|\bL\.?A\.?\b|Inland Empire|"
                      r"Riverside|San Bernardino|San Diego|Bay Area|San Francisco|San Jose|Sacramento|Fresno|"
                      r"Bakersfield|Ventura|Santa Clarita|Irvine|Simi|Oxnard|Thousand Oaks|Camarillo|Temecula|"
                      r"Menifee|Chino|Ontario|Fontana|Palmdale|Lancaster|Roseville|Stockton|Livermore|"
                      r"Orange County Register|Orange County Business Journal|San Diego Business Journal|"
                      r"Los Angeles Business Journal|Sacramento Business Journal|San Francisco Business Times|"
                      r"Silicon Valley Business Journal|Los Angeles Times|Press-Enterprise|Mercury News|"
                      r"Chronicle|Sacramento Bee|Union-Tribune|Builder Magazine|HousingWire|Builder's Daily|"
                      r"Bloomberg|Reuters|Wall Street Journal", re.I)
NEED_CONTEXT = {"rancho mission viejo": re.compile(r"home|village|communit|sales|develop|builder|lots|acre", re.I)}
NEWS_DROP = re.compile(r"Mill City Ventures|crypto|token|air quality|\bAQI\b|population|"
                       r"stock forecast|stock analysis|price target", re.I)
# Securities law firms put out a dozen near-identical "shareholder alert"
# releases for each class action. One is news; the rest bury everything else.
LAW_FIRM_ALERT = re.compile(r"\balerts?\b|shareholder|class action|lead plaintiff|securities (fraud|litigation)|"
                            r"\bLLP\b|\bP\.C\.|law firm|investors (who|with) losses", re.I)
WIRE_SLUG = re.compile(r"^[A-Z0-9 -]{6,}$")    # bare AP wire slugs such as CA-CITY-VENTURES
_or = lambda names: " OR ".join('"%s"' % n for n in names)
NEWS_QUERIES = {
    "ca_private": [(_or(CA_ONLY_BUILDERS[:4]), "ca"), (_or(CA_ONLY_BUILDERS[4:]), "ca"),
                   (_or(NATIONAL_PRIVATE[:4]), "national"), (_or(NATIONAL_PRIVATE[4:]), "national")],
    "landbank":   [('"Millrose Properties" OR "Forestar Group" OR "Five Point Holdings"', None),
                   ('"land banking" homebuilders', None),
                   ('"land banker" OR "lot banking" OR "homesite option"', None)],
}
# Daily feeds for the narrative: the public builders by name, then California
# and Ventura County housing. Short windows, since these feed a daily read.
PUBLIC_ALIASES = {"DHI": ["D.R. Horton", "DR Horton", "Horton"], "LEN": ["Lennar"],
                  "PHM": ["PulteGroup", "Pulte"], "TOL": ["Toll Brothers"], "KBH": ["KB Home"],
                  "MTH": ["Meritage"], "CCS": ["Century Communities"], "MHO": ["M/I Homes"],
                  "LGIH": ["LGI Homes"], "NVR": ["NVR", "Ryan Homes"]}
# Stock-move, ratings and fund-holdings items (mostly auto-generated) carry no
# information the board's own price data lacks.
BUILDER_SPAM = re.compile(r"shares (sold|bought|acquired|purchased)|\bstake\b|position in|holdings in|"
                          r"price target|rating|upgrade|downgrade|short interest|options|stock forecast|"
                          r"stock analysis|dividend|\bETF\b|(stock|shares) (rises?|falls?|jumps?|drops?|gains?|"
                          r"dips?|slides?|climbs?|soars?|sinks?|tumbles?)|trading (up|down)|is it time to|"
                          r"should you buy|valuation|undervalued|overvalued", re.I)
HOUSING_WORDS = re.compile(r"\bhomes?\b|housing|builder|develop|construction|permit|apartment|\bland\b|"
                           r"\brent|mortgage|real estate|subdivision|\bunits\b|zoning|RHNA|\bADU|escrow|"
                           r"home sales|home prices|wildfire rebuild|annexation|specific plan|general plan", re.I)
NEWS_QUERIES.update({
    "builders": [('"D.R. Horton" OR "Lennar" OR "PulteGroup" OR "Toll Brothers"', "public"),
                 ('"KB Home" OR "Meritage Homes" OR "Century Communities"', "public"),
                 ('"M/I Homes" OR "LGI Homes" OR "Ryan Homes" OR "NVR Inc"', "public")],
    "west":     [('"Southern California" housing', "housing"),
                 ('California homebuilders', "housing"),
                 ('"Inland Empire" housing', "housing")],
    "ventura":  [('"Ventura County" housing', "housing"),
                 ('"Ventura County" homes', "housing"),
                 ('"Simi Valley" OR "Thousand Oaks" OR "Camarillo" OR "Oxnard" OR "Moorpark"', "housing")],
})
NEWS_WINDOW = {"builders": (10, 15), "west": (10, 10), "ventura": (14, 10)}   # days, items kept
NEWS_DAYS = 45
COSTS_FEED = "https://eyeonhousing.org/feed/"   # NAHB's economics blog
COST_WORDS = re.compile(r"material|lumber|tariff|construction cost|building cost|steel|labor|wage|worker|input|framing", re.I)

def _rss_items(url, headers=None):
    req = urllib.request.Request(url, headers=headers or YAHOO_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        if "bing.com/news/apiclick" in link:      # unwrap Bing's click-tracking redirect
            link = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        pub = None
        raw = (it.findtext("pubDate") or "").strip()
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                pub = datetime.datetime.strptime(raw, fmt).replace(tzinfo=None); break
            except ValueError:
                continue
        if title and link:
            out.append({"title": title, "link": link, "source": source,
                        "published": pub.date().isoformat() if pub else None})
    return out

def _news_search(q, days=None):
    gurl = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q + " when:%dd" % (days or NEWS_DAYS), "hl": "en-US", "gl": "US", "ceid": "US:en"})
    for wait in (0, 6, 15):
        time.sleep(wait)
        try:
            return _rss_items(gurl)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                break
        except Exception:
            break
    burl = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss"})
    return _rss_items(burl)

def _tidy(items, n=8, days=None):
    cutoff = (TODAY - datetime.timedelta(days=(days or NEWS_DAYS) + (15 if not days else 2))).isoformat()
    seen, out, alert = set(), [], False
    for it in sorted(items, key=lambda x: x.get("published") or "", reverse=True):
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]
        if key in seen or (it.get("published") and it["published"] < cutoff):
            continue
        if LAW_FIRM_ALERT.search(it["title"]):
            if alert:
                continue
            alert = True
        seen.add(key); out.append(it)
    return out[:n]

def pull_news(previous):
    prev = previous or {}
    out = {}
    def keep(item, kind):
        t = item["title"]
        if NEWS_DROP.search(t) or WIRE_SLUG.match(t):
            return False
        if kind is None:
            return True
        if kind == "public":
            if BUILDER_SPAM.search(t):
                return False
            tks = [tk for tk, al in PUBLIC_ALIASES.items()
                   if any(re.search(r"\b%s\b" % re.escape(a), t) for a in al)]
            if not tks:
                return False
            item["tickers"] = tks
            return True
        if kind == "housing":
            return bool(HOUSING_WORDS.search(t))
        names = CA_ONLY_BUILDERS if kind == "ca" else NATIONAL_PRIVATE
        hit = next((n for n in names if n.lower() in t.lower()), None)
        if not hit:
            return False
        ctx = NEED_CONTEXT.get(hit.lower())
        if ctx and not ctx.search(t):
            return False
        return kind == "ca" or bool(CA_WORDS.search(t + " " + item.get("source", "")))

    def search(key, queries):
        items = []
        for q, kind in queries:
            try:
                got = _news_search(q, NEWS_WINDOW.get(key, (None,))[0])
                kept = [i for i in got if keep(i, kind)]
                print("  %-10s %-60.60s %2d found, %2d kept" % (key, q, len(got), len(kept)))
                items += kept
            except Exception as e:
                print("  %-10s query failed: %s" % (key, e))
            time.sleep(2)
        return items
    jobs = [(k, lambda k=k, q=q: search(k, q)) for k, q in NEWS_QUERIES.items()]
    jobs.append(("costs", lambda: [dict(i, source=i.get("source") or "NAHB Eye on Housing")
                                   for i in _rss_items(COSTS_FEED) if COST_WORDS.search(i["title"])]))
    for key, job in jobs:
        try:
            days, n = NEWS_WINDOW.get(key, (None, 8))
            items = _tidy(job(), n, days)
            if not items:
                raise ValueError("no items")
            out[key] = {"items": items, "fetched": TODAY.isoformat()}
            print("  %-10s %d items" % (key, len(items)))
        except Exception as e:
            old = prev.get(key) or {}
            out[key] = old
            age = (TODAY - datetime.date.fromisoformat(old["fetched"])).days if old.get("fetched") else 99
            print("  %-10s failed (%s); keeping items from %s" % (key, e, old.get("fetched")))
            if age > 3:
                note("News feed '%s' has not refreshed for %d days: %s" % (key, age, e))
        time.sleep(2)
    return out


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


# The model reasons before it answers, and that reasoning is billed as output
# and counts against max_tokens. On Nov 23 it used 11,000 to 15,000 tokens of
# reasoning for a 3,600-token answer, so every call gets this much room on top
# of the answer length it asks for. Only tokens actually used are billed; the
# limit itself costs nothing.
REASONING_ROOM = 16000

def anthropic(prompt, max_tokens=4000, timeout=600, room=REASONING_ROOM):
    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens + room,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        classify_anthropic_error(e.code, body)
        raise RuntimeError("Anthropic API HTTP %s: %s" % (e.code, body[:300]))
    global LAST_REPLY_META
    LAST_REPLY_META = {"stop_reason": out.get("stop_reason"), "usage": out.get("usage"),
                       "blocks": [b.get("type") for b in out.get("content", [])]}
    return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")

LAST_REPLY_META = {}


def check_anthropic_key():
    """Confirms the API key, model and credits every run, so an empty account
    shows up on the pages that morning rather than when a filing cannot be
    read. The model list is free; the five-token test message costs a small
    fraction of a cent and is what tells an account with credits from one
    without."""
    if not ANTHROPIC_KEY:
        print("  ANTHROPIC_API_KEY not set; new filings will wait for a manual read")
        return
    hdr = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}
    try:
        j = get_json("https://api.anthropic.com/v1/models?limit=100", headers=hdr, tries=2)
        ids = [m.get("id") for m in j.get("data", [])]
        if ANTHROPIC_MODEL not in ids:
            note("Anthropic model %s is not available to this API key. Available: %s. Set the "
                 "ANTHROPIC_MODEL secret to one of these." % (ANTHROPIC_MODEL, ", ".join(ids[:8])))
            return
        print("  API key accepted; model %s available" % ANTHROPIC_MODEL)
    except urllib.error.HTTPError as e:
        classify_anthropic_error(e.code, e.read().decode("utf-8", "ignore"))
        if BILLING["anthropic"]["status"] == "off":
            set_billing("anthropic", "error", "The Claude API answered HTTP %s to the key check." % e.code)
        return
    except Exception as e:
        print("  could not reach the Anthropic API to check the key: %s" % e)
        return
    try:
        anthropic("Reply with the word OK.", max_tokens=5, timeout=120, room=0)
        set_billing("anthropic", "ok", "Key, model and credits confirmed.")
    except Exception as e:
        if BILLING["anthropic"]["status"] in ("off", "ok"):
            set_billing("anthropic", "error", "The Claude API test message failed: %s" % str(e)[:200])


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
  "build_cost": "what the filing says about direct, house or stick-and-brick construction costs,
                 with any figure (per square foot, per home, or the change versus the prior year
                 or quarter), in one sentence. null if the filing is silent.",
  "cycle_time": "construction cycle or build time as stated, e.g. '116 days, from 126'. null if none.",
  "cost_drivers": "one sentence on what the filing says moved gross margin (lot costs,
                   incentives, direct costs, tariffs, labor). null if none.",
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
- The brief is read for weeks after it is written, so anchor every time reference to a fixed
  period or date: "the September quarter", "fiscal 2026", "due in late November". Never write
  relative time ("this month", "this quarter", "recently", "last edition", "upcoming"), and never
  refer to the brief itself or to earlier editions. Carried-forward facts get their own period
  ("at the end of its July quarter"), not "from the last edition".

Return ONLY a JSON object, no prose and no code fence:
{{
  "headline": "one sentence, under 20 words",
  "national": ["3 to 4 paragraphs"],
  "land": ["2 to 4 paragraphs on lot positions and land strategy"],
  "socal": ["2 to 4 paragraphs, or one paragraph saying the filings are silent on it"],
  "ventura": ["carry forward or refine the prior section"],
  "takeaway": ["2 to 3 short paragraphs of implications for a land seller"],
  "construction_summary": "2 to 4 sentences on what the builders say about their direct construction
                           costs, cycle times and cost drivers, using the build_cost, cycle_time and
                           cost_drivers fields. Name builders and figures.",
  "app": {{
    "signals": [{{"value": "a short figure such as 8,777 or $24.8M", "text": "under 15 words on what it is"}}],
    "west": [{{"name": "Builder · segment", "stat": "one short figure", "tone": "better, worse or flat", "text": "under 20 words"}}],
    "watch": [{{"label": "one or two words", "text": "one or two sentences"}}],
    "cost_notes": [{{"who": "builder name or names", "text": "one or two sentences on build costs, labor or lot costs"}}],
    "cost_summary": "one or two sentences on what builders say about costs",
    "macro_note": "one sentence on what builders say about rates and demand",
    "next_filings": [{{"name": "e.g. D.R. Horton annual report", "when": "month"}}]
  }}
}}

The "app" block feeds the phone version of this brief. signals: 3 to 5 land or balance
sheet figures from the data (lot walk-aways, deposit write-offs, impairments, land charges).
west: one entry per builder whose western or California segment the data describes; keep the
prior entry for any builder not in this data. watch: carry the prior entries forward unless the
data changes them. next_filings: the next reports expected from tracked builders, dropping any
already covered by this data.

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
             if b.get("period") and b["period"] > covered.get(b["ticker"], "")]

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
    prior["app"] = brief.get("app", {})
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

    # The construction-cost table: one row per builder, replaced whenever that
    # builder's new filing says something about costs, otherwise left as is.
    cons = brief.setdefault("construction", {"builders": []})
    rows_c = {r.get("ticker"): r for r in cons.get("builders", [])}
    for e in extracts:
        if not any(e.get(k) for k in ("build_cost", "cycle_time", "cost_drivers")):
            continue
        r = rows_c.get(e["ticker"]) or {"ticker": e["ticker"], "name": e["name"]}
        try:
            per = datetime.date.fromisoformat(e["quarter"]).strftime("%b %Y") + " qtr"
        except Exception:
            per = e["quarter"]
        r.update({"period": per, "source": "Filing for period ending %s" % e["quarter"]})
        if e.get("build_cost"):   r["build_cost"] = e["build_cost"]
        if e.get("cycle_time"):   r["cycle_time"] = e["cycle_time"]
        if e.get("cost_drivers"): r["drivers"] = e["cost_drivers"]
        if e.get("margin"):       r["margin"] = e["margin"]
        rows_c[e["ticker"]] = r
    cons["builders"] = list(rows_c.values())
    if new.get("construction_summary"):
        cons["summary"] = new["construction_summary"]
    cons["as_of"] = TODAY.isoformat()

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
        "app": {**(brief.get("app") or {}), **(new.get("app") or {})},
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
def pull_headlines():
    """Recent real estate headlines, for the six-tile section on the board.

    Source: HousingWire's own RSS feed (housingwire.com/feed/), verified by
    fetching it directly rather than assumed - confirmed standard RSS 2.0,
    updated roughly hourly, with real current items at verification time.
    HousingWire is the standard trade publication for US housing and
    mortgage lending, and its feed already covers exactly this board's
    subject matter (rates, builders, policy, CEQA-adjacent stories) without
    needing a second source. Title and a short publisher-provided summary
    are shown with a link back to the original, the standard RSS-reader
    pattern; no article body is fetched or reproduced.

    Deliberately a single source: RSS is a stable, decades-old format with
    no key and no rate limit, a different risk profile than Yahoo or
    Redfin's unofficial endpoints, so the added complexity of a second
    source for redundancy is not worth it here. If this feed ever changes
    shape, the error below carries the raw XML root tag so a future fix
    starts from an actual data point, not a guess.
    """
    URL = "https://www.housingwire.com/feed/"
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": SEC_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        channel = root.find("channel")
        if channel is None:
            note("Headlines: RSS root has no <channel> element (tag was <%s>)" % root.tag)
            return []
        items = channel.findall("item")
        if not items:
            note("Headlines: feed parsed but contained zero <item> entries")
            return []

        out = []
        for item in items[:6]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc_raw = item.findtext("description") or ""
            desc = re.sub(r"<[^>]+>", "", desc_raw).strip()  # strip the <p> wrapper RSS descriptions carry
            cats = [c.text for c in item.findall("category") if c.text]
            # skip HousingWire's own internal membership/tag categories, keep
            # the first genuinely descriptive one if present
            skip = {"HWmember", "Columnist", "The Builder's Daily"}
            category = next((c for c in cats if c not in skip), (cats[0] if cats else None))
            pub_raw = item.findtext("pubDate") or ""
            pub_iso = None
            try:
                pub_iso = datetime.datetime.strptime(pub_raw, "%a, %d %b %Y %H:%M:%S %z").isoformat()
            except ValueError:
                pass
            if not title or not link:
                continue
            out.append({"title": title, "link": link, "description": desc,
                        "category": category, "published": pub_iso})

        if not out:
            note("Headlines: items were present but none had both a title and a link")
        return out
    except ET.ParseError as e:
        note("Headlines: feed did not parse as XML: %s" % e)
        return []
    except Exception as e:
        note("Headlines failed: %s" % e)
        return []


def pull_resale_data():
    """Existing-home resale market: median sale price, homes sold, median
    days on market, and the sale-to-list price ratio, at national,
    California, and Ventura County level, from one consistent source so the
    three levels are actually comparable to each other.

    Neither FRED nor SEC EDGAR carry closed-sale data below the national
    level; FRED's Ventura County series (via Realtor.com) is inventory and
    days-on-market only, not sale price, volume, or the list-price ratio.
    Redfin's Data Center is the one free, no-key source that publishes all
    four at every level: https://www.redfin.com/news/data-center/downloads

    Source history, so the next fix starts from facts:
      - Through May 2026 this read the legacy files under
        redfin_market_tracker/ (state_market_tracker.tsv000.gz etc.). Those
        files stopped updating on 2026-06-02 and are frozen at May 2026. The
        national one was always named us_national_market_tracker.tsv000.gz;
        national_market_tracker.tsv000.gz never existed, which is what the
        recurring 403 was (S3 answers 403, not 404, for a missing key).
      - The legacy files carried one row per property type per month and
        the old parser kept whichever row came first, so the board was
        showing a single property-type slice (California "homes sold" of
        about 1,100 a month), not all residential.
      - Since the May 2026 rebuild, Redfin's own Downloads page reads
        pre-built CSVs from redfin_data_center/housing_market/monthly/ in
        the same public bucket (verified 2026-10-23: country.csv,
        all_states.csv, all_counties.csv, refreshed monthly). These are
        All Residential, all construction, one row per region per month,
        and they are exactly what that page serves. No key, no bot wall.
      - Redfin publishes national and state rows seasonally adjusted where
        it can and county rows unadjusted; median sale price is unadjusted
        at every level. That is stated on the sheet.
    """
    BASE = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_data_center/housing_market/monthly/"
    LEVELS = [
        # key, label, file, region name as Redfin writes it
        ("national", "United States",      "country.csv",      None),
        ("ca",       "California",         "all_states.csv",   "California"),
        ("ventura",  "Ventura County, CA", "all_counties.csv", "Ventura County, CA"),
    ]
    COLS = {
        "period_end":   ["PERIOD END", "PERIOD_END"],
        "region_name":  ["REGION NAME", "REGION"],
        "price":        ["MEDIAN SALE PRICE NSA ($)", "MEDIAN SALE PRICE ($)"],
        "sold":         ["HOMES SOLD"],
        "dom":          ["MEDIAN DAYS ON MARKET (DAYS)"],
        "sale_to_list": ["AVERAGE SALE TO LIST RATIO (%)"],
    }
    NUMERIC_FIELDS = ["price", "sold", "dom", "sale_to_list"]
    OUTPUT_KEY = {"price": "median_price", "sold": "homes_sold", "dom": "dom", "sale_to_list": "sale_to_list"}

    def find_col(header, candidates):
        lower = {h.strip().lower(): i for i, h in enumerate(header)}
        for cand in candidates:
            if cand.lower() in lower:
                return lower[cand.lower()]
        return None

    def read_level(key, fname, region):
        """Streams the file and keeps only the lines for this region, so the
        150 MB county file never has to sit in memory."""
        req = urllib.request.Request(BASE + fname, headers={"User-Agent": SEC_UA})
        header, rows = None, []
        needle = region.encode() if region else None
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw in r:
                if header is None:
                    header = next(csv.reader([raw.decode("utf-8-sig", "replace")]))
                    continue
                if needle and needle not in raw:
                    continue
                rows.append(next(csv.reader([raw.decode("utf-8", "replace")])))
        if not header:
            note("Resale %s: %s came back empty" % (key, fname))
            return None
        idx = {k: find_col(header, v) for k, v in COLS.items()}
        if idx["period_end"] is None:
            note("Resale %s: no PERIOD END column in %s. Full header: %s" % (key, fname, header))
            return None
        missing = [k for k in NUMERIC_FIELDS if idx[k] is None]
        if missing:
            note("Resale %s: column(s) %s not found in %s, continuing without them. Full header: %s"
                 % (key, missing, fname, header))
        series = {f: {} for f in NUMERIC_FIELDS}
        for row in rows:
            if region and idx["region_name"] is not None and row[idx["region_name"]].strip() != region:
                continue
            d = row[idx["period_end"]][:10]
            if not d:
                continue
            for f in NUMERIC_FIELDS:
                if idx[f] is None:
                    continue
                v = row[idx[f]].strip()
                if v in ("", "NA", "null"):
                    continue
                try:
                    series[f][d] = round(float(v), 2)
                except ValueError:
                    continue
        out = {OUTPUT_KEY[f]: [{"d": d, "v": v} for d, v in sorted(s.items())][-96:]
               for f, s in series.items()}
        if not any(out.values()):
            note("Resale %s: %s read, but no rows matched %s" % (key, fname, region or "the country"))
            return None
        return out

    levels = []
    for key, label, fname, region in LEVELS:
        try:
            s = read_level(key, fname, region)
        except Exception as e:
            note("Resale %s failed: %s" % (key, e))
            s = None
        if s:
            levels.append({"key": key, "label": label, **s})
            last = (s.get("median_price") or [{}])[-1]
            print("  %-9s %4d price pts, latest %s" % (key, len(s.get("median_price", [])), last.get("d")))
        time.sleep(0.5)

    return {"as_of": TODAY.isoformat(), "levels": levels,
            "source": "Redfin Data Center, Housing Market Tracker (monthly, all residential), "
                      "redfin.com/news/data-center/downloads. National and California figures are "
                      "seasonally adjusted where Redfin adjusts them; Ventura County is unadjusted; "
                      "median sale price is unadjusted at every level"}


def yahoo_ytd(tk, digits=2):
    """Year-to-date daily closes for one Yahoo symbol, in the shape the board
    uses for every price row: price, ytd_pct (from the first close of the
    year), as_of, series [{d, v}]. Raises on any failure so the caller can
    word its own error."""
    j = get_json("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=ytd&interval=1d"
                 % urllib.parse.quote(tk), headers=YAHOO_HEADERS)
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("no price data returned")
    res = res[0]
    ts = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    if not pairs:
        raise ValueError("price series came back empty")
    series = [{"d": datetime.datetime.utcfromtimestamp(t).date().isoformat(), "v": round(c, digits)}
              for t, c in pairs]
    meta = res.get("meta") or {}
    first, last = series[0]["v"], series[-1]["v"]
    return {
        "ticker": tk,
        "price": meta.get("regularMarketPrice", last),
        "ytd_pct": round((last - first) / first * 100, 1) if first else None,
        "as_of": series[-1]["d"],
        "series": series,
        "currency": meta.get("currency", "USD"),
        "name": meta.get("shortName") or meta.get("longName"),
    }


def pull_stock_prices(tickers=None, label="Stock price"):
    out = []
    for tk in (tickers or BUILDERS):
        try:
            row = yahoo_ytd(tk)
            out.append(row)
            print("  %-6s $%.2f  YTD %s%%" % (tk, row["price"],
                  row["ytd_pct"] if row["ytd_pct"] is not None else "?"))
        except Exception as e:
            note("%s for %s failed: %s" % (label, tk, e))
        time.sleep(0.4)
    return out


# Construction commodities, as front-month futures from the same Yahoo chart
# endpoint. Symbols and units checked against live responses on 2026-11-19.
# Front-month contracts roll, so a year-to-date line can show a small step at
# a roll date; the producer price indexes in SERIES are the smoother monthly
# read on the same materials.
COMMODITIES = [
    ("LBR=F", "Lumber",                  "$/1,000 bd ft", 2,
     "Framing lumber futures. The swing input in a wood-frame house."),
    ("HRC=F", "Hot-rolled steel",        "$/short ton",   0,
     "Midwest hot-rolled coil. Rebar, beams, connectors and appliances."),
    ("HG=F",  "Copper",                  "$/lb",          3,
     "Wiring, plumbing and HVAC line sets."),
    ("ALI=F", "Aluminum",                "$/metric ton",  0,
     "Windows, sliding doors, siding and ductwork."),
    ("CL=F",  "Crude oil, WTI",          "$/barrel",      2,
     "Feeds asphalt paving and roofing, plastics and freight."),
    ("HO=F",  "Diesel (NY Harbor ULSD)", "$/gallon",      3,
     "Fuel for grading equipment, concrete trucks and deliveries."),
    ("NG=F",  "Natural gas",             "$/MMBtu",       3,
     "Energy for drywall, brick, glass and insulation plants."),
]

def pull_commodities():
    out = []
    for tk, name, unit, dp, why in COMMODITIES:
        try:
            row = yahoo_ytd(tk, digits=max(dp, 2))
            row.update({"name": name, "unit": unit, "decimals": dp, "why": why})
            out.append(row)
            print("  %-6s %s  YTD %s%%" % (tk, row["price"], row["ytd_pct"]))
        except Exception as e:
            note("Commodity price for %s (%s) failed: %s" % (name, tk, e))
        time.sleep(0.4)
    return out


# Public land bankers and lot developers that hold land for the public
# builders. Millrose is the homesite option REIT spun out of Lennar, Forestar
# is D.R. Horton's majority-owned lot developer, and Five Point sells land at
# Great Park, Valencia and Candlestick and owns the Hearthstone land-banking
# platform. Private land bankers have no price to track; they appear in the
# brief and in the news feed below.
LANDBANK_TICKERS = ["MRP", "FOR", "FPH"]


# ---------------------------------------------------------------------------
def refresh_resale_paragraph(brief, resale):
    """Keeps the brief's Ventura resale paragraph in step with the resale panel.

    That paragraph used to be written by hand in brief.json, so it went stale the
    month after it was written (it still quoted September after the board had
    October). It is now rebuilt from the same Redfin data on every run. The
    paragraph is found by its mention of Redfin; if none exists it is added as the
    second paragraph of the Ventura section. brief.json on disk is left alone.
    """
    level = next((l for l in (resale or {}).get("levels", []) if l.get("key") == "ventura"), None)
    if not level or not level.get("median_price"):
        return brief

    def latest_and_year_ago(obs):
        if not obs:
            return None, None
        last = obs[-1]
        target = "%d%s" % (int(last["d"][:4]) - 1, last["d"][4:])
        prior = [o for o in obs if o["d"] <= target]
        return last, (prior[-1] if prior else None)

    def change(last, prior):
        if not last or not prior or not prior["v"]:
            return None
        return (last["v"] / prior["v"] - 1) * 100

    price, price_ya = latest_and_year_ago(level.get("median_price"))
    sold, sold_ya = latest_and_year_ago(level.get("homes_sold"))
    dom = (level.get("dom") or [None])[-1]
    stl = (level.get("sale_to_list") or [None])[-1]
    month = datetime.date.fromisoformat(price["d"]).strftime("%B")

    def moved(pct):
        return ("up %.1f%%" % pct) if pct >= 0 else ("down %.1f%%" % abs(pct))

    bits = ["median sale price $%s" % format(int(round(price["v"])), ",")]
    pc = change(price, price_ya)
    if pc is not None:
        bits[-1] += ", %s from a year earlier" % moved(pc)
    if sold:
        sc = change(sold, sold_ya)
        bits.append("on %s sales%s" % (format(int(round(sold["v"])), ","),
                                        (", " + moved(sc)) if sc is not None else ""))
    tail = []
    if dom:
        tail.append("a %d-day median time on market" % round(dom["v"]))
    if stl:
        tail.append("sales at %.1f%% of list" % stl["v"])
    text = ("The resale side, from Redfin's %s county data in the resale panel on this board: %s"
            % (month, ", ".join(bits)))
    if tail:
        text += ", with " + " and ".join(tail)
    text += "."

    paras = list(brief.get("ventura") or [])
    idx = next((i for i, p in enumerate(paras) if "Redfin" in p), None)
    if idx is None:
        paras.insert(min(1, len(paras)), text)
    else:
        paras[idx] = text
    brief["ventura"] = paras
    return brief


# ---------------------------------------------------------------------------
# Builder posts on X (optional)
# ---------------------------------------------------------------------------
# Read only when an X_BEARER_TOKEN secret exists (X API, pay per use). Each
# candidate handle is accepted only if the account's own website link or bio
# names the builder's domain, so a look-alike or squatted handle is never
# read. Resolved account ids are kept in narrative.json so the lookup runs
# once a month, and each timeline is read from the last post already seen,
# so only new posts are billed.
X_BEARER = os.environ.get("X_BEARER_TOKEN", "").strip()
# Checked Nov 23, 2026. Six accounts link their builder's site and were
# verified by the API lookup. M/I Homes' @mihomes links through a shortener,
# but mihomes.com names it as its own account (twitter:site), so it is
# confirmed by the builder's site instead. D.R. Horton, Century Communities and
# NVR/Ryan Homes link no X account from their websites, so none is read: an
# @DR_Horton account carries drhorton.com in its profile but D.R. Horton's own
# site does not point to it.
X_CANDIDATES = {
    "LEN":  (["Lennar"], "lennar.com"),
    "PHM":  (["PulteHomes", "PulteGroup"], "pulte"),
    "TOL":  (["TollBrothers"], "tollbrothers.com"),
    "KBH":  (["KBHome"], "kbhome.com"),
    "MTH":  (["MeritageHomes"], "meritagehomes.com"),
    "MHO":  (["mihomes"], "mihomes.com"),
    "LGIH": (["LGIHomes"], "lgihomes.com"),
}
X_CONFIRMED_BY_SITE = {"MHO": "mihomes"}

def _x_get(path, **params):
    url = "https://api.x.com/2/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + X_BEARER,
                                               "User-Agent": "housing-signal-board"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def x_credit_check():
    """Remaining X API credit, from the usage endpoint (returns no posts, so
    nothing is billed per resource). Sets the billing status the pages show."""
    try:
        j = _x_get("usage/credits")
        bal = float((j.get("data") or {}).get("total_balance", 0))
        free = float((j.get("data") or {}).get("free_balance", 0))
        if bal <= 0:
            set_billing("x", "out", "Builder posts on X are paused until credits are added.",
                        balance=bal, free=free)
        elif bal < X_LOW_BALANCE:
            set_billing("x", "low", "$%.2f left." % bal, balance=bal, free=free)
        else:
            set_billing("x", "ok", "$%.2f of X API credit left." % bal, balance=bal, free=free)
        return bal
    except urllib.error.HTTPError as e:
        x_http_error(e, "credit check")
    except Exception as e:
        set_billing("x", "error", "Could not check X API credits: %s" % e)
    return None

def x_http_error(e, what):
    body = ""
    try:
        body = e.read().decode("utf-8", "ignore")
    except Exception:
        pass
    if e.code == 401:
        set_billing("x", "rejected", "The X API token was rejected (%s). Regenerate the Bearer Token "
                    "and replace the X_BEARER_TOKEN secret in GitHub." % what)
    elif e.code == 402 or re.search(r"credit|payment|billing|spend", body, re.I):
        set_billing("x", "out", "Credits are used up or the monthly spending limit was reached (seen on "
                    "the %s). Builder posts on X are paused until credits are added or the limit resets." % what)
    elif e.code == 403:
        set_billing("x", "error", "The X API refused access (%s, HTTP 403): %s" % (what, body[:160]))
    else:
        print("  X %s failed: HTTP %s %s" % (what, e.code, body[:160]))

def pull_x_posts(state, previous):
    old = (previous or {}).get("items", [])
    if not X_BEARER:
        return previous or {}
    bal = x_credit_check()
    if bal is not None and bal <= 0 or BILLING["x"]["status"] in ("rejected", "out"):
        return previous or {}
    accts = state.setdefault("x_accounts", {})
    try:
        stale = (TODAY - datetime.date.fromisoformat(state.get("x_resolved", "2000-01-01"))).days > 30
        missing = [tk for tk in X_CANDIDATES if tk not in accts]
        retry = bool(missing) and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        if not accts or stale or retry:
            # A manual run retries only the builders not yet found; the full
            # lookup repeats monthly. Lookups are billed per account found.
            todo = X_CANDIDATES if (not accts or stale) else {tk: X_CANDIDATES[tk] for tk in missing}
            names = [n for c, _ in todo.values() for n in c]
            j = _x_get("users/by", usernames=",".join(names),
                       **{"user.fields": "url,entities,description,verified,name"})
            found = {u["username"].lower(): u for u in j.get("data", [])}
            if todo is X_CANDIDATES:
                accts.clear()
            for tk, (cands, domain) in todo.items():
                for c in cands:
                    u = found.get(c.lower())
                    if not u:
                        continue
                    urls = " ".join(x.get("expanded_url", "") for x in
                                    ((u.get("entities") or {}).get("url") or {}).get("urls", []))
                    if (domain in (urls + " " + (u.get("description") or "")).lower()
                            or X_CONFIRMED_BY_SITE.get(tk, "").lower() == u["username"].lower()):
                        accts[tk] = {"id": u["id"], "username": u["username"], "name": u.get("name")}
                        break
                    print("  X %-5s @%s exists but does not link %s (name %r, link %r)"
                          % (tk, u["username"], domain, u.get("name"), urls[:80]))
                print("  X %-5s %s" % (tk, accts.get(tk, {}).get("username", "no verified account")))
            state["x_resolved"] = TODAY.isoformat()
    except urllib.error.HTTPError as e:
        x_http_error(e, "account lookup")
        return previous or {}
    except Exception as e:
        print("  X lookup failed: %s" % e)
        return previous or {}
    for tk in [t for t in accts if t not in X_CANDIDATES]:      # dropped from the list
        accts.pop(tk, None)
    since = state.setdefault("x_since", {})
    new = []
    for tk, a in accts.items():
        params = {"max_results": 5, "exclude": "retweets,replies", "tweet.fields": "created_at"}
        if since.get(tk):
            params["since_id"] = since[tk]
        try:
            j = _x_get("users/%s/tweets" % a["id"], **params)
        except urllib.error.HTTPError as e:
            x_http_error(e, "%s timeline" % tk)
            if BILLING["x"]["status"] in ("rejected", "out"):
                break
            continue
        except Exception as e:
            print("  X %s timeline failed: %s" % (tk, e)); continue
        for t in j.get("data", []):
            new.append({"ticker": tk, "handle": a["username"], "text": t.get("text", ""),
                        "published": (t.get("created_at") or "")[:10], "id": t["id"],
                        "link": "https://x.com/%s/status/%s" % (a["username"], t["id"])})
        newest = (j.get("meta") or {}).get("newest_id")
        if newest:
            since[tk] = newest
        time.sleep(1)
    items = sorted(new + [o for o in old if o.get("id") not in {n["id"] for n in new}],
                   key=lambda x: x.get("published", ""), reverse=True)[:30]
    print("  X posts: %d new, %d kept" % (len(new), len(items)))
    return {"items": items, "fetched": TODAY.isoformat()}


# ---------------------------------------------------------------------------
# The daily narrative
# ---------------------------------------------------------------------------
# One running account of the market in three sections (national, California
# and the West, Ventura County), rewritten each morning from whatever is new
# since the last edition: data releases, price moves, filings, 8-Ks, news and
# builder posts. Continuity is the point, so narrative.json keeps
#   - the current text, with [n] markers pointing at a source registry,
#   - a ledger of claims: each forecast or assumption the narrative has made,
#     with the date it was made and whether later evidence confirmed or
#     contradicted it,
#   - revisions: where new evidence went against an earlier claim, what was
#     said, what changed and why, which the pages show,
#   - a short history of editions, and the evidence already considered, so
#     nothing is reported as new twice.
# On a day with nothing material the text stays as it is. If the API call
# fails, the last edition stays up and the failure is reported after two
# misses in a row.
NARRATIVE_PATH = os.environ.get("NARRATIVE_PATH", "narrative.json")
SECTION_NAMES = {"national": "National: macro, rates and the public builders",
                 "west": "California and the West",
                 "ventura": "Ventura County"}
KEY_SERIES = ["MORTGAGE30US", "DGS10", "SPREAD", "HOUST1F", "PERMIT1", "HSN1F", "MSACSR", "EXHOSLUSM495S",
              "CSUSHPINSA", "CABPPRIV", "LXXRSA", "CAUR", "WPUSI012011", "WPU081", "UNRATE", "CPIAUCSL"]

def load_narrative():
    try:
        with open(NARRATIVE_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}

def _fmt_val(v, units):
    u = (units or "").lower()
    if "percent" in u or u in ("%", "pct"):
        return "%.2f%%" % v
    if abs(v) >= 1000:
        return "{:,.0f}".format(v)
    return "%.2f" % v if abs(v) < 100 else "%.1f" % v

def _yoy(obs):
    if len(obs) < 2:
        return None
    last = obs[-1]
    target = (datetime.date.fromisoformat(last["d"]) - datetime.timedelta(days=365)).isoformat()
    prior = [o for o in obs if o["d"] <= target]
    if not prior or not prior[-1]["v"]:
        return None
    return round((last["v"] - prior[-1]["v"]) / abs(prior[-1]["v"]) * 100, 1)

def eightk_text(url, limit=2500):
    """The press release behind an 8-K when it has one (exhibit 99), else the
    8-K's own item text."""
    try:
        base = url.rsplit("/", 1)[0]
        idx = get_json(base + "/index.json", headers=SEC_HEADERS)
        docs = [x["name"] for x in idx.get("directory", {}).get("item", [])]
        ex = next((d for d in docs if re.search(r"ex-?99", d, re.I) and d.lower().endswith((".htm", ".html"))), None)
        text = filing_text(base + "/" + ex if ex else url, limit=60000)
        if not ex:
            m = re.search(r"Item\s+\d\.\d\d", text)
            text = text[m.start():] if m else text
        return re.sub(r"\s+", " ", text)[:limit]
    except Exception as e:
        return "(text unavailable: %s)" % e

def build_evidence(state, series, stocks, commodities, landbanks, builders, lb_filings, news, headlines, brief):
    """New items since the last edition, each with a stable source id."""
    seen = set(state.get("seen", []))
    marks = dict(state.get("marks", {}))       # copies: committed only once the edition is written
    nid = state.get("next_id", 1)
    ev, new_seen = [], []

    def add(kind, title, detail="", url="", source="", date=None, key=None):
        nonlocal nid
        key = key or url or (kind + ":" + title)
        if key in seen or key in new_seen:
            return
        ev.append({"id": nid, "kind": kind, "title": title, "detail": detail, "url": url,
                   "source": source, "date": date or TODAY.isoformat()})
        new_seen.append(key)
        nid += 1

    # data releases newer than the last edition saw
    # marks hold [date, value] of each series as the last edition saw it, so a
    # daily series is compared with what the narrative last said, not with the
    # month-end point stored before it.
    first_run = not marks
    for sid, sr in series.items():
        obs = sr.get("obs") or []
        if not obs:
            continue
        last = obs[-1]
        seen_at = marks.get(sid)
        seen_d, seen_v = (seen_at if isinstance(seen_at, list) else [seen_at or "", None])
        if not first_run and last["d"] > seen_d:
            prev = obs[-2] if len(obs) > 1 else None
            y = _yoy(obs)
            if seen_v is not None:
                before = "the previous edition had %s for %s" % (_fmt_val(seen_v, sr.get("units")), seen_d)
            else:
                before = "previous %s (%s)" % (_fmt_val(prev["v"], sr.get("units")) if prev else "n/a", prev["d"] if prev else "")
            add("data", "%s: %s for %s" % (sr.get("name", sid), _fmt_val(last["v"], sr.get("units")), last["d"]),
                "%s%s. Units: %s. %s" % (before, "; %+.1f%% year over year" % y if y is not None else "",
                                          sr.get("units", ""), sr.get("read", "")),
                "https://fred.stlouisfed.org/series/%s" % sid, "FRED", last["d"], key="data:%s:%s" % (sid, last["d"]))
        marks[sid] = [last["d"], last["v"]]

    # price moves since the last edition, as one item per group
    px = dict(state.get("px", {}))
    def moves(rows, label, cost=False):
        parts = []
        for r in rows:
            p0 = px.get(r["ticker"])
            chg = "" if not p0 else ", %+.1f%% since the last edition" % ((r["price"] - p0) / p0 * 100)
            parts.append("%s %s (%+.1f%% YTD%s)" % (r.get("name") or r["ticker"], r["price"], r.get("ytd_pct") or 0, chg))
            px[r["ticker"]] = r["price"]
        if parts:
            when = max((r.get("as_of") or "") for r in rows)
            add("market", label + " closes through " + when, "; ".join(parts), "", "Yahoo Finance", when,
                key="market:%s:%s" % (label, when))
    moves(stocks, "Public builder share prices")
    moves(landbanks, "Land banker share prices")
    moves(commodities, "Construction commodity futures", cost=True)

    # new 10-Qs and 10-Ks, and a rewritten filing brief
    for b in builders + lb_filings:
        if b.get("filed") and b.get("url"):
            add("filing", "%s filed its %s for the period ended %s" % (b.get("name", b["ticker"]), b.get("form"), b.get("period")),
                "", b["url"], "SEC EDGAR", b["filed"], key="filing:" + b["url"]) if not first_run else new_seen.append("filing:" + b["url"])
    if brief.get("as_of") and brief["as_of"] != state.get("brief_as_of"):
        add("brief", "Filing brief rewritten %s: %s" % (brief["as_of"], brief.get("headline", "")),
            " ".join((brief.get("takeaway") or [])[:3])[:1500], "", "Builder brief on this board", brief["as_of"],
            key="brief:" + brief["as_of"])

    # 8-Ks, with the press release text
    n8 = 0
    for b in builders + lb_filings:
        for k in b.get("recent_8k", []):
            key = "8k:" + k["acc"]
            if key in seen or n8 >= 12:
                continue
            if not k["items"] or k["items"] == ["5.07"]:
                new_seen.append(key); continue
            add("8-K", "%s 8-K (%s)" % (b.get("name", b["ticker"]), ", ".join(k["item_names"]) or "exhibits"),
                eightk_text(k["url"]), k["url"], "SEC EDGAR", k["filed"], key=key)
            n8 += 1
            time.sleep(0.2)

    # news, headlines and builder posts
    labels = {"builders": "public builder news", "west": "California housing news", "ventura": "Ventura County news",
              "ca_private": "California private builder news", "landbank": "land banking news", "costs": "NAHB cost news",
              "x_posts": "builder post on X"}
    for key, label in labels.items():
        for it in ((news.get(key) or {}).get("items") or [])[:12]:
            text = it.get("text") if key == "x_posts" else it.get("title")
            add("x" if key == "x_posts" else "news", text[:280], label + (" (%s)" % ", ".join(it["tickers"]) if it.get("tickers") else ""),
                it.get("link", ""), it.get("handle") and "@" + it["handle"] or it.get("source", ""), it.get("published"))
    for h in (headlines or [])[:6]:
        add("news", h.get("title", ""), (h.get("description") or "")[:300], h.get("link", ""), "HousingWire",
            (h.get("published") or "")[:10])

    return ev, new_seen, {"marks": marks, "px": px, "next_id": nid}

NARRATIVE_PROMPT = """You keep a running market narrative for Colton Lee Communities, a Southern California
land developer that entitles and sells residential land to public and private homebuilders, based in
Ventura County. Readers are developers who need to know what the public builders, the macro economy,
construction costs and the California and Ventura County markets are doing. Today is {today}.

The narrative has three sections:
  national: macro, rates, credit and the public builders (orders, margins, incentives, land behaviour)
  west: California and the West, including California private builders and land bankers
  ventura: Ventura County and its cities

Below are the PREVIOUS EDITION (dated {prev_date}), the CLAIMS LEDGER (statements, forecasts and
assumptions earlier editions made, with the date each was made), RECENT HISTORY of editions, a DATA
SNAPSHOT for context, and NEW EVIDENCE since the previous edition, each item with a numeric id.

Your job:
1. Decide what in the new evidence is material to a land developer. Routine items, marketing posts and
   stock chatter are not. Builder news, 8-Ks, guidance, land deals, data releases that move the picture,
   cost changes and California or Ventura County housing news are.
2. Update the narrative so it reads as one continuous, chronological account. Keep sentences from the
   previous edition word for word where nothing changed, with their [n] markers. Revise only what the
   new evidence changes, and add new developments with their dates (write "Nov 20", never "yesterday",
   "this week" or "recently").
3. Never contradict an earlier statement silently. When new evidence goes against a claim in the ledger,
   say so in the narrative itself, naming the date of the earlier statement, what the evidence now shows
   and why the picture changed, and add it to "revisions". When evidence supports a claim, you may
   mark it confirmed.
4. Every number must come from the evidence, the data snapshot or the previous edition. Cite new
   evidence with its id in square brackets, e.g. [214]. Markers already in the previous edition point at
   earlier sources; keep them on sentences you keep. Do not invent sources.
5. Name the public builders and what they said or did whenever builder news, 8-Ks or posts are present.
6. Plain English, short sentences, no em dashes, no hedging boilerplate. national 2 to 4 paragraphs,
   west 2 to 3, ventura 1 to 3, each under 120 words. The headline is one sentence under 25 words on
   the single most important development, not a list. At most 5 "changes", most important first.
7. If nothing in the new evidence is material, return exactly {{"unchanged": true}}.

Return ONLY a JSON object, no prose and no code fence:
{{
  "unchanged": false,
  "headline": "one sentence",
  "sections": {{"national": ["paragraph", ...], "west": [...], "ventura": [...]}},
  "changes": [{{"text": "one sentence on what is new since {prev_date}", "refs": [ids]}}],
  "revisions": [{{"claim_id": "c3", "earlier": "what was said and when", "now": "what the evidence shows",
                  "why": "why it differs", "refs": [ids]}}],
  "claims": [{{"id": "c3 for an existing claim, or new", "section": "national|west|ventura",
               "text": "the statement or assumption", "status": "open|confirmed|contradicted|retired",
               "note": "short reason when the status changed"}}]
}}
"claims" is the full updated ledger: every existing claim with its status, plus a "new" entry for each
new forecast or assumption this edition makes. Keep at most 25 open claims; retire stale ones.

PREVIOUS EDITION ({prev_date}):
{prev}

CLAIMS LEDGER:
{claims}

RECENT HISTORY:
{history}

DATA SNAPSHOT:
{snapshot}

NEW EVIDENCE:
{evidence}
"""

def _dash_free(t):
    return re.sub(r"\s*—\s*", ", ", t or "").replace("–", "-")

def refresh_narrative(state, series, stocks, commodities, landbanks, builders, lb_filings, news, headlines, brief):
    prev_date = state.get("as_of")
    ev, new_seen, cursor = build_evidence(state, series, stocks, commodities, landbanks, builders, lb_filings,
                                          news, headlines, brief)
    state["checked"] = TODAY.isoformat()
    print("  %d new evidence items since %s" % (len(ev), prev_date or "the start"))

    def remember():
        # Only after an edition is written (or judged unchanged) does the
        # evidence count as considered; a failed call leaves it for tomorrow.
        state["seen"] = (state.get("seen", []) + new_seen)[-1500:]
        state["brief_as_of"] = brief.get("as_of")
        state.update(cursor)

    if not ANTHROPIC_KEY:
        remember()
        return state
    if not ev and state.get("sections"):
        remember()
        return state

    if state.get("sections"):
        prev = "\n\n".join("[%s]\n%s" % (k, "\n\n".join(state["sections"].get(k, []))) for k in SECTION_NAMES)
        prev = "HEADLINE: %s\n\n%s" % (state.get("headline", ""), prev)
    else:      # first edition: start from the filing brief
        prev_date = brief.get("as_of") or TODAY.isoformat()
        prev = ("This is the first edition. Start from this filing-based brief written %s.\n\n"
                "HEADLINE: %s\n\n[national]\n%s\n\n[west]\n%s\n\n[ventura]\n%s\n\n[takeaways]\n%s"
                % (prev_date, brief.get("headline", ""), "\n\n".join(brief.get("national", [])),
                   "\n\n".join(brief.get("socal", [])), "\n\n".join(brief.get("ventura", [])),
                   "\n\n".join(brief.get("takeaway", []))))
    claims = state.get("claims", [])
    ledger = "\n".join("%s [%s, %s, made %s] %s" % (c["id"], c["section"], c["status"], c["made"], c["text"])
                       for c in claims if c["status"] == "open" or c.get("updated", "") >= (TODAY - datetime.timedelta(days=14)).isoformat()) or "(none yet)"
    history = "\n".join("%s: %s | %s" % (h["date"], h["headline"], " ".join(h.get("changes", [])[:4]))
                        for h in state.get("history", [])[-7:]) or "(none yet)"
    snap = []
    for sid in KEY_SERIES:
        sr = series.get(sid) or {}
        if sr.get("obs"):
            y = _yoy(sr["obs"])
            snap.append("%s: %s (%s)%s" % (sr.get("name", sid), _fmt_val(sr["obs"][-1]["v"], sr.get("units")),
                                            sr["obs"][-1]["d"], ", %+.1f%% YoY" % y if y is not None else ""))
    if stocks:
        snap.append("Public builders average YTD: %+.1f%%" % (sum(r.get("ytd_pct") or 0 for r in stocks) / len(stocks)))
    evidence = "\n".join("[%d] %s | %s | %s | %s%s" % (e["id"], e["date"], e["kind"], e["source"], e["title"],
                                                      ("\n     " + e["detail"]) if e["detail"] else "") for e in ev)
    prompt = NARRATIVE_PROMPT.format(today=TODAY.strftime("%B %d, %Y"), prev_date=prev_date, prev=prev,
                                     claims=ledger, history=history, snapshot="\n".join(snap), evidence=evidence or "(none)")
    reply = ""
    try:
        reply = anthropic(prompt, 12000, timeout=720)
        print("  model reply: %s, %d chars" % (LAST_REPLY_META, len(reply)))
        out = parse_json_reply(reply)
        if not isinstance(out, dict):
            raise ValueError("reply was not a JSON object")
    except Exception as e:
        state["fail_count"] = state.get("fail_count", 0) + 1
        print("  narrative update failed: %s" % e)
        if reply:
            print("  reply began: %r" % reply[:600])
            print("  reply ended: %r" % reply[-300:])
        # A credit or key problem already shows as its own alert on the pages.
        if state["fail_count"] >= 2 and BILLING["anthropic"]["status"] not in ("out", "limit", "rejected"):
            note("The daily narrative could not be updated for %d runs: %s" % (state["fail_count"], e))
        return state
    state["fail_count"] = 0
    remember()
    if out.get("unchanged") and state.get("sections"):
        print("  nothing material; narrative unchanged")
        return state

    # registry of sources the text may cite: earlier ones plus today's evidence
    reg = {int(k): v for k, v in (state.get("sources") or {}).items()}
    for e in ev:
        reg[e["id"]] = {"title": e["title"][:200], "url": e["url"], "source": e["source"], "date": e["date"], "kind": e["kind"]}
    valid = set(reg)
    def clean(t):
        t = _dash_free(str(t))
        t = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if int(m.group(1)) in valid else "", t)
        return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", t)).strip()
    secs = {}
    for k in SECTION_NAMES:
        paras = [clean(p) for p in (out.get("sections") or {}).get(k, []) if isinstance(p, str) and p.strip()]
        secs[k] = paras[:5] or state.get("sections", {}).get(k, [])
    if not any(secs.values()):
        note("The daily narrative reply had no usable text; kept the previous edition.")
        return state
    refs = lambda r: [int(x) for x in (r or []) if str(x).isdigit() and int(x) in valid]
    changes = [{"text": clean(c.get("text", "")), "refs": refs(c.get("refs"))} for c in out.get("changes", []) if c.get("text")][:6]
    revisions = [{"claim_id": r.get("claim_id"), "earlier": clean(r.get("earlier", "")), "now": clean(r.get("now", "")),
                  "why": clean(r.get("why", "")), "refs": refs(r.get("refs"))} for r in out.get("revisions", []) if r.get("now")][:6]

    # claims ledger: keep made dates, stamp status changes, number new claims
    old = {c["id"]: c for c in claims}
    nxt = max([int(c["id"][1:]) for c in claims if c["id"][1:].isdigit()] + [0]) + 1
    ledger_out = []
    for c in out.get("claims", []):
        if not c.get("text"):
            continue
        status = c.get("status") if c.get("status") in ("open", "confirmed", "contradicted", "retired") else "open"
        cid = c.get("id") if c.get("id") in old else None
        if cid:
            o = old[cid]
            ledger_out.append({**o, "text": clean(c["text"]) or o["text"], "status": status,
                               "updated": TODAY.isoformat() if status != o["status"] else o.get("updated", o["made"]),
                               "note": clean(c.get("note", "")) or o.get("note", "")})
        else:
            ledger_out.append({"id": "c%d" % nxt, "section": c.get("section") if c.get("section") in SECTION_NAMES else "national",
                               "text": clean(c["text"]), "status": status, "made": TODAY.isoformat(),
                               "updated": TODAY.isoformat(), "note": clean(c.get("note", ""))})
            nxt += 1
    kept_ids = {c["id"] for c in ledger_out}
    ledger_out += [c for c in claims if c["id"] not in kept_ids and c["status"] != "open"][-40:]
    for r in revisions:
        o = next((c for c in ledger_out if c["id"] == r.get("claim_id")), None)
        if o and o["status"] == "open":
            o.update({"status": "contradicted", "updated": TODAY.isoformat()})

    # keep only sources still cited
    cited = set()
    for t in [p for v in secs.values() for p in v] + [c["text"] for c in changes]:
        cited |= {int(x) for x in re.findall(r"\[(\d+)\]", t)}
    for c in changes + revisions:
        cited |= set(c["refs"])
    history = (state.get("history") or [])
    if state.get("sections"):
        history = history + [{"date": state.get("as_of"), "headline": state.get("headline", ""),
                              "changes": [c["text"] for c in state.get("changes", [])],
                              "revisions": [r["now"] for r in state.get("revisions", [])]}]
    state.update({
        "since": state.get("as_of") or prev_date,
        "as_of": TODAY.isoformat(),
        "headline": clean(out.get("headline", "")) or state.get("headline", ""),
        "sections": secs, "changes": changes, "revisions": revisions,
        "claims": ledger_out, "history": history[-30:],
        "sources": {str(i): reg[i] for i in sorted(cited) if i in reg},
        "evidence_count": len(ev),
    })
    print("  narrative rewritten: %d changes, %d revisions, %d open claims"
          % (len(changes), len(revisions), sum(1 for c in ledger_out if c["status"] == "open")))
    return state

def save_narrative(state):
    with open(NARRATIVE_PATH, "w") as fh:
        json.dump(state, fh, indent=2)

def narrative_for_page(state):
    """What the pages need, without the bookkeeping."""
    if not state.get("sections"):
        return None
    return {k: state.get(k) for k in ("as_of", "since", "checked", "headline", "sections", "changes",
                                      "revisions", "sources", "evidence_count")} | {
        "history": (state.get("history") or [])[-14:],
        "claims": [c for c in state.get("claims", []) if c["status"] == "open"
                   or c.get("updated", "") >= (TODAY - datetime.timedelta(days=21)).isoformat()]}


def main():
    if not FRED_KEY:
        print("FRED_API_KEY is not set. Add it under Settings > Secrets and variables > Actions.",
              file=sys.stderr)
        sys.exit(1)

    print("FRED series...");     series, releases = pull_fred()
    add_spread(series)
    print("Release calendar..."); calendar = pull_release_calendar(releases)
    print("Anthropic API...");    check_anthropic_key()
    print("SEC EDGAR...");        builders = pull_builders()
    print("Polymarket...");       markets  = pull_markets()
    print("Builder brief...");   brief    = refresh_brief(builders)
    print("Resale market...");   resale   = pull_resale_data()
    brief = refresh_resale_paragraph(brief, resale)
    print("Headlines...");       headlines = pull_headlines()
    print("Stock prices...");    stocks   = pull_stock_prices()
    print("Commodities...");     commodities = pull_commodities()
    print("Land bankers...");    landbanks = pull_stock_prices(LANDBANK_TICKERS, "Land banker price")
    lb_filings = pull_filings(LANDBANK_TICKERS)
    brief = refresh_landbanks(brief, lb_filings)
    try:
        with open(OUT_PATH) as fh:
            previous_news = json.load(fh).get("news")
    except Exception:
        previous_news = None
    print("News...");            news = pull_news(previous_news)
    narrative = load_narrative()
    print("X posts...");         news["x_posts"] = pull_x_posts(narrative, (previous_news or {}).get("x_posts"))
    print("Narrative...")
    narrative = refresh_narrative(narrative, series, stocks, commodities, landbanks, builders, lb_filings,
                                  news, headlines, brief)
    save_narrative(narrative)

    payload = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "series": series,
        "rates": [r for r in RATES if r in series],
        "calendar": calendar,
        "builders": builders,
        "brief": brief,
        "stocks": stocks,
        "commodities": commodities,
        "landbanks": landbanks,
        "landbank_filings": lb_filings,
        "news": news,
        "narrative": narrative_for_page(narrative),
        "billing": BILLING,
        "resale": resale,
        "headlines": headlines,
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
