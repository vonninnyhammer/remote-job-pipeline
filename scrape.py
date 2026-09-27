#!/usr/bin/env python3
"""
remote job aggregator
=====================
Pulls US-remote job postings from several free/public sources
(all official APIs or plain HTML - no scraping of ToS-restricted
sites like Indeed/LinkedIn/ZipRecruiter):

  - Remotive            https://remotive.com/api/remote-jobs
  - RemoteOK            https://remoteok.com/api
  - WeWorkRemotely      https://weworkremotely.com  (HTML, category pages)
  - Jobicy              https://jobicy.com/dashboard/api
  - Working Nomads      https://www.workingnomads.com/api/exposed_jobs
  - Greenhouse boards   https://boards-api.greenhouse.io/v1/boards/<co>/jobs
  - Lever boards        https://api.lever.co/v0/postings/<co>?mode=json
  - SmartRecruiters     https://api.smartrecruiters.com/v1/companies/<co>/postings

Filters:  remote = yes AND (US-based OR worldwide) AND salary >= $60k/yr
(where the salary is posted).  Roles without a posted salary are kept only
when the role type plausibly clears $60k - flagged salary_verified=false.

Usage:
  python3 scrape.py probe            # test which boards/companies respond
  python3 scrape.py sync             # fetch, filter, dedupe, merge into postings.json
  python3 scrape.py stats            # show counts per board
"""
import json, os, re, sys, hashlib, datetime, time, html
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
POSTINGS = os.path.join(BASE, "postings.json")
FRESH = os.path.join(BASE, "fresh.json")
SEEN = os.path.join(BASE, "seen.json")
BOARD_STATUS = os.path.join(BASE, "board_status.json")
LOGS = os.path.join(BASE, "logs")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 20

# --------------------------------------------------------------------------
# employer boards to sweep (best-effort; failures are skipped and logged)
# --------------------------------------------------------------------------
GREENHOUSE_BOARDS = [
    "gitlab", "datadog", "dropbox", "hashicorp", "pulumi", "stripe",
    "notion", "airtable", "instacart", "databricks", "confluent",
    "vercel", "esri", "chime",
]
LEVER_BOARDS = [
    "automattic", "grammarly", "webflow", "mercury", "contentful",
    "sticker-mule", "signal", "planet",
]
SMARTRECRUITERS_BOARDS = ["wework", "uber", "wix", "roku"]

# --------------------------------------------------------------------------
# US-remote heuristics
# --------------------------------------------------------------------------
REMOTE_KW = re.compile(r"remote|anywhere|virtual|100%|distributed|flexible-home", re.I)
US_KW = re.compile(r"\bus[ab\.]?\b|\busa\b|united\s?states|north\s?americ|americas?\b", re.I)
BAD_REGION = re.compile(
    r"\bemea\b|europe|uk\b|london|ireland|india|bengaluru|apac|asia|japan|australia|"
    r"\bcanada\b|brazil|latin\b|latam|germany|france|spain|netherlands|poland|"
    r"mexico|philippines|south africa|scandinav|nordic|portugal|sweden|norway|"
    r"denmark|finland|dubai|kenya|egypt|nigeria", re.I)
GLOBAL_KW = re.compile(r"worldwide|global|everywhere|all over the world", re.I)
# location strings that look like a specific on-site city with no remote signal
CITY_ONLY = re.compile(r"\b(ny\b|new york|san francisco|seattle|chicago|austin|boston|"
                       r"los angeles|atlanta|denver|phoenix|houston|dallas|"
                       r"washington dc|remote-?us)\b", re.I)

def us_remote_ok(location: str) -> bool:
    """True if a posting can be worked by someone resident in the US."""
    loc = location or ""
    if not loc.strip():
        return True  # no location info -> treat as open
    if BAD_REGION.search(loc) and not US_KW.search(loc):
        return False
    if US_KW.search(loc):
        return True
    if GLOBAL_KW.search(loc):
        return True  # worldwide remote: fully workable from the US
    if REMOTE_KW.search(loc):
        return True
    # bare city names without a remote signal are on-site roles
    return False

REMOTE_ONLY_BOARDS = {"remotive", "remoteok", "jobicy", "workingnomads"}

def is_remote_signal(location: str, board: str) -> bool:
    """Sense 'this is a remote role'. Boards that only carry remote work treat
    any US/global location + empty location as remote by definition."""
    loc = (location or "").strip()
    if not loc:
        return board in REMOTE_ONLY_BOARDS
    if REMOTE_KW.search(loc) or GLOBAL_KW.search(loc):
        return True
    if US_KW.search(loc):
        if board in REMOTE_ONLY_BOARDS:
            return True
        if not CITY_ONLY.fullmatch(loc):
            return True
    return False

def remote_and_us(location: str, board: str) -> bool:
    """Engine-room check: must read as remote AND workable from the US."""
    return is_remote_signal(location, board) and us_remote_ok(location)

# --------------------------------------------------------------------------
# salary parsing -> (min_annual_usd|None, max_annual_usd|None, verified:bool)
# --------------------------------------------------------------------------
def _num(t):
    return float(t.replace(",", ""))

def parse_salary(text):
    if not text:
        return None, None, False
    t = str(text).strip()
    if not t or t.lower() in ("n/a", "na", "-", "tbd", "competitive", "negotiable"):
        return None, None, False
    fx = 1.0
    if "€" in t or re.search(r"\beur\b", t, re.I):
        fx = 1.08
    elif "£" in t or "gbp" in t.lower():
        fx = 1.30
    elif "cad" in t.lower():
        fx = 0.74
    elif "aud" in t.lower():
        fx = 0.67

    lo = hi = None
    verified = False

    # hourly: $120 - $170 / hour | $35/hr, maybe a range
    hrs = re.findall(r"([¥$€£]?\s*[\d,]+(?:\.\d+)?)\s*/?\s*(?:hour|hr)\b", t, re.I)
    if hrs:
        hourly = []
        for tok in re.findall(r"[\d][\d,]*(?:\.\d+)?", t):
            v = _num(tok)
            if 3 <= v <= 500 and v not in hourly:
                hourly.append(v)
        if hourly:
            lo = min(hourly) * 2080 * fx
            hi = max(hourly) * 2080 * fx
            verified = True
    else:
        mo = re.search(r"([¥$€£]?\s*[\d,]+(?:\.\d+)?)\s*/?\s*(?:mo|month)\b", t, re.I)
        if mo:
            val = _num(re.sub(r"[^\d.,]", "", mo.group(1)))
            lo = hi = val * 12 * fx
            verified = True
        else:
            # year figures: $70,000; 70k; 70 - 90k; 70k-90k USD
            nums = re.findall(r"(?:[¥$€£]|\b)\s*([\d][\d,]*(?:\.\d+)?)\s*(k|k/yr|/yr|per year|/year|\b)", t, re.I)
            vals = []
            this_sym = True if re.search(r"[¥$€£]|\b(?:usd|us)\b", t, re.I) else False
            for v, suffix in nums:
                n = _num(v)
                if suffix.lower().startswith("k"):
                    n *= 1000
                elif suffix.lower() in ("per year", "/year", "/yr", "k/yr"):
                    n *= 1000 if suffix.lower() != "/yr" else 1
                if n < 20000:
                    continue  # unlikely to be an annual salary number
                if n < 99999 and not ("k" in suffix.lower() or "," in v) and len(v) <= 4:
                    continue
                vals.append(int(n))
            if len(vals) >= 2:
                lo, hi = min(vals), max(vals)
            elif len(vals) == 1:
                lo = hi = vals[0]
            verified = bool(vals)

    # sanity cap: refuse absurd figures ($750k for a course director) - mark unverified
    if verified and lo and hi and (lo > 420000 or hi > 420000):
        verified = False
        lo = hi = None

    return lo, hi, verified

# --------------------------------------------------------------------------
# role 'lane' classification (drives cover-letter angle + salary whitelist)
# --------------------------------------------------------------------------
LANES = [
    ("AI/ML", r"\bai\b|\bai/|machine learning|\bml\b|llm|nlp|prompt|computer vision|\bocr\b|"
              r"data scienti|mlops|\bmodels?\b|deep learning|genai|ai[-/ ]agent|ai[-/ ]native|"
              r"\bcto\b|chief technology"),
    ("Data", r"data (analyst|entry|engineer|ingest|processor|annotator)|sql\b|"
             r"database|etl|bi ?developer|analytics|power bi|tableau|reporting|"
             r"(product|technical|revenue systems) analyst"),
    ("QA/Test", r"\bqa\b|test(ing|er)?|quality (assurance|engineer|analyst|management)|"
                r"\bqms\b|sdet|uat"),
    ("DevOps/Cloud", r"devops|sre|site reliability|platform engineer|cloud|kubernetes|"
                     r"docker|terraform|infrastructure( engineering)?|build engineer|ci/cd"),
    ("IT Ops", r"\bit\b support|help ?desk|service ?desk|desktop|sysadmin|system admin|technician|noc|"
               r"network admin|network engineer|it admin|msp|workday|systems? engineer|"
               r"(?<!data )support engineer|tier [12]|endpoint|intune|sccm|field ?engineer"),
    ("Customer Support", r"customer (support|success|service)|support specialist|"
                         r"(?!data )support (agent|rep)|client success|cs specialist|concierge"),
    ("Business Ops", r"operations (specialist|coordinator|manager|associate)|project (manager|coordinator)|"
                     r"program manager|business (operations|analyst)|consultant|strategy|"
                     r"product (manager|owner|ops)|technical consultant"),
    ("Accounting/Finance", r"bookkeep|accountant|payroll|finance|controller|tax |"
                           r"financial (analyst|advisor)|\bfo\b|underwrit|actuar"),
    ("Sales/BD", r"sales|account (executive|manager)|business development|bdr|sdr|"
                r"partner manager"),
    ("Design", r"designer|design (engineer|lead)|\bux\b|\bui\b|graphic|visual design|illustrat"),
    ("Marketing", r"marketing|marketer|\bseo\b|\bsem\b|social (media|comms)|brand|"
                  r"growth (marketing|hacker)|content strategist|performance market"),
    ("Content/Creative", r"writer|editor|content (creator|writer)|copywriter|video|podcast|journal"),
    ("Admin Ops", r"virtual assistant|administrative|executive assistant|office (manager|admin)|"
                  r"reception"),
    ("HR", r"\bhr\b|recruit|talent|people (ops|partner)|coordinator people"),
    ("Legal/Compliance", r"paralegal|counsel|attorney|legal |compliance|risk|trust,? safety"),
]
LANE_RE = [(name, re.compile(pat, re.I)) for name, pat in LANES]

# Description-fallback eligibility. The second pass may only assign a lane whose
# signal is a ROLE NOUN, never a bare technology. Without this gate, a role whose
# title carries no lane signal ("Senior Performance Marketer" at an AI company)
# fell through to the description and inherited whatever the employer's
# boilerplate bragged about, so every opening at an AI shop got filed under AI/ML
# regardless of what the person would actually be doing. Deriving this by
# subtracting the buzzwords out of the alternation is a trap: it leaves empty
# branches, and an empty branch matches every string.
DESC_UNSAFE = {"AI/ML", "Data", "QA/Test", "DevOps/Cloud"}
LANE_DESC_RE = [(n, rx) for n, rx in LANE_RE if n not in DESC_UNSAFE]

# Talent marketplaces, not employers. They post invented "Senior Independent X
# Engineer" listings with real-looking bands that pollute the top of any
# salary-sorted pool.
MARKETPLACES = {"A.Team", "A.Team (Marketplace)", "Toptal", "Crossover", "Gun.io"}

# lanes plausibly worth >= $60k even without a posted salary
SALARY_UNVERIFIED_OK = {
    "AI/ML", "Data", "QA/Test", "DevOps/Cloud", "IT Ops",
    "Customer Support", "Business Ops", "Accounting/Finance",
    "Sales/BD", "Legal/Compliance",
}

def classify_lane(title, text=""):
    """Classify on the TITLE first. Only fall back to the description when the
    title alone has no lane signal, and on that second pass use role nouns
    only - never the generic tech buzzwords - so a company that mentions AI in
    its boilerplate cannot claim every opening it advertises."""
    for name, rx in LANE_RE:
        if rx.search(title):
            return name
    for name, rx in LANE_DESC_RE:
        if rx.search(f"{title} {text}"):
            return name
    return "Other"

def hot_skills_for(lane, tags_text=""):
    base = {
        "AI/ML": ["AI/ML/OCR"],
        "Data": ["SQL/Databases", "AI/ML/OCR"],
        "QA/Test": ["SQL/Databases", "App Support/Implementation"],
        "DevOps/Cloud": ["Linux", "Cloud/Virtualization", "DevOps/Containers"],
        "IT Ops": ["Desktop/Endpoint", "Windows/AD/SCCM", "Networking (TCP/IP/VLAN)"],
        "Customer Support": ["Desktop/Endpoint", "Networking (TCP/IP/VLAN)", "Bilingual"],
        "Business Ops": ["Desktop/Endpoint"],
        "Accounting/Finance": ["SQL/Databases"],
        "Sales/BD": [],
        "Design": [],
        "Marketing": [],
        "Content/Creative": [],
        "Admin Ops": [],
        "HR": [],
        "Legal/Compliance": [],
    }
    lst = base.get(lane, [])
    t = (tags_text or "").lower()
    extra = []
    if any(k in t for k in ("linux", "ubuntu", "debian")) and "Linux" not in lst:
        extra.append("Linux")
    if any(k in t for k in ("docker", "kubernetes", "aws", "gcp", "azure", "cloud")):
        extra.append("Cloud/Virtualization")
    return lst + [e for e in extra if e not in lst]

# --------------------------------------------------------------------------
# fetching helpers
# --------------------------------------------------------------------------
def fetch_json(session, url, headers=None, params=None):
    r = session.get(url, headers=headers, params=params,
                    timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.json()

def norm(x):
    return re.sub(r"\W+", " ", (x or "").lower()).strip()

def posting_id(url):
    h = hashlib.sha256(norm(url).encode()).hexdigest()[:7]
    return f"r{h}"

_TEXTCLEAN = re.compile(r"<[^>]+>")

def clean_html(x):
    x = html.unescape(x or "")
    return re.sub(r"\s+", " ", _TEXTCLEAN.sub(" ", x)).strip()

def now():
    return datetime.datetime.now().isoformat(timespec="minutes")

MAILTO_RE = re.compile(r"mailto:\s*([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})", re.I)
EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
APPLY_HINT_RE = re.compile(r"apply|send (?:your |a )?(?:resume|cv|application)|"
                           r"e-?mail (?:your |a )?(?:resume|cv|application)|"
                           r"submit (?:your )?(?:resume|cv|application)|"
                           r"(?:submit|send|apply).{0,40}(?:resume|cv|application)", re.I)
# addresses that are clearly NOT an application destination
NOISE_EMAIL = re.compile(
    r"^(no[-_]?reply|donotreply|do[-_]?not[-_]?reply|notifications?|alerts?|"
    r"mailer[-_]?daemon|postmaster|webmaster|sitemap|unsubscribe|newsletter|"
    r"marketing@?|sales@?|billing@?|payments@?|abuse@?|security@?|privacy@?|"
    r"legal@?|root@?|admin@?" + r")", re.I)
# EEO / ADA / accessibility-request and recruiting-fraud-reporting mailboxes.
# These sit in a "contact us" clause next to the word "apply", so the
# proximity heuristic used to pick them up and mail real applications to a
# restricted box nobody in recruiting ever reads.
NOISE_EMAIL_LOCALPART = re.compile(
    r"accommodation|reasonable[-_]?accommodation|accessib|"
    r"ada[-_]?request|eeo|equal[-_]?opportunity|"
    r"fraud|scam|phish|report[-_]?phish|"
    r"interview[-_]?accommodat", re.I)
NOISE_DOMAIN = re.compile(r"^(?:example|sample|test|localhost|invalid)\.|<\.local>|"
                          r"\.(?:png|jpg|gif)$", re.I)
# Some employers state outright that email/unsolicited applications are not
# accepted and that the ATS is the only reviewed channel. No address in such
# a description is an apply channel, so suppress the whole posting.
NO_EMAIL_APPLY_RE = re.compile(
    r"does not accept unsolicited (?:resumes|applications)|"
    r"will not (?:be )?(?:review|consider)[^.]{0,60}(?:outside|directly|outside of)[^.]{0,40}applicant tracking|"
    r"applications? submitted (?:outside|directly|via (?:email|LinkedIn))[^.]{0,80}(?:not (?:be )?review|not considered)|"
    r"must apply (?:through|via|on) (?:our|the)[^.]{0,40}(?:careers|site|portal|ats|application)", re.I)

def _clean_email_candidate(raw):
    e = (raw or "").strip().lower().rstrip(").,;") if raw else ""
    if not e:
        return ""
    if NOISE_EMAIL.search(e) or NOISE_EMAIL_LOCALPART.search(e) \
            or NOISE_DOMAIN.search(e.split("@")[-1]):
        return ""
    return e

def extract_apply_email(description):
    """Best-effort apply-address for a posting description. Accepts ANY real
    email found (mailto: links first, then the one nearest to apply-wording,
    else any email at all) - wide net on purpose - while dropping obvious
    noise (noreply/notification/marketing-style localparts, placeholder
    domains). Review `autosend.py scan` before trusting a batch."""
    t = clean_html(description or "")
    if not t:
        return ""
    if NO_EMAIL_APPLY_RE.search(t):
        return ""
    m = MAILTO_RE.search(t)
    if m:
        e = _clean_email_candidate(m.group(1))
        if e:
            return e
    hits = []
    hint_positions = [h.start() for h in APPLY_HINT_RE.finditer(t)]
    for m in EMAIL_RE.finditer(t):
        e = _clean_email_candidate(m.group(0))
        if e:
            hits.append((m.start(), e))
    if not hits:
        return ""
    if hint_positions:
        best = min(hits, key=lambda h: min(abs(h[0] - p) for p in hint_positions))
        return best[1]
    return hits[0][1]

# --------------------------------------------------------------------------
# board fetchers - each returns list of raw dicts with common keys
#   board, title, company, location, url, salary_raw, published_raw, tags, description
# --------------------------------------------------------------------------
def fetch_remotive(session):
    out = []
    d = fetch_json(session, "https://remotive.com/api/remote-jobs", params={"limit": 200})
    for j in d.get("jobs", []):
        out.append({
            "title": j.get("title"), "company": j.get("company_name"),
            "location": j.get("candidate_required_location"),
            "url": j.get("url"), "salary_raw": j.get("salary"),
            "tags": " ".join(j.get("tags", [])),
            "published_raw": (j.get("publication_date", "")[:10]),
            "description": j.get("short_description") or "",
        })
    return out

def fetch_remoteok(session):
    d = fetch_json(session, "https://remoteok.com/api",
                   headers={"User-Agent": UA, "Accept": "application/json"})
    out = []
    for j in d:
        if not isinstance(j, dict) or not isinstance(j.get("slug"), str):
            continue
        out.append({
            "title": j.get("position"), "company": j.get("company"),
            "location": j.get("location"), "url": j.get("url"),
            "salary_raw": (f"${j.get('salary_min')} - ${j.get('salary_max')}"
                           if j.get("salary_max") else None),
            "tags": " ".join(j.get("tags", [])),
            "published_raw": (j.get("date", "")[:10]),
            "description": clean_html(j.get("description", "")),
        })
    return out

def fetch_weworkremotely(session):
    from bs4 import BeautifulSoup
    out = []
    cats = ["", "programming", "sysadmin", "devops", "customer-support", "data",
            "admin", "sales", "product", "marketing", "writing", "design", "account",
            "accounting-and-finance", "legal"]
    for c in cats:
        url = ("https://weworkremotely.com/remote-jobs"
               if not c else f"https://weworkremotely.com/categories/remote-{c}-jobs")
        try:
            r = session.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            for li in soup.select("li.new-listing-container"):
                a = None
                for x in li.select("a[href^='/remote-jobs/']"):
                    if re.fullmatch(r"/remote-jobs/[a-z0-9-]+", x.get("href", "")):
                        a = x
                        break
                if not a:
                    continue
                t = li.select_one(".new-listing__header__title__text")
                co = li.select_one(".new-listing__company-name")
                loc = li.select_one(".new-listing__company-headquarters")
                out.append({
                    "title": t.get_text(strip=True) if t else None,
                    "company": co.get_text(strip=True) if co else None,
                    "location": loc.get_text(strip=True) if loc else "",
                    "url": "https://weworkremotely.com" + a["href"],
                    "salary_raw": None, "tags": "",
                    "published_raw": "", "description": "",
                })
        except Exception as e:
            print(f"   [weworkremotely:{c or 'root'}] {e}")
        time.sleep(0.3)
    return out

def fetch_jobicy(session):
    out = []
    d = fetch_json(session, "https://jobicy.com/api/v2/remote-jobs",
                   params={"count": 50})
    for j in d.get("jobs", []):
        geo = j.get("jobGeo") or ""
        sraw = None
        if j.get("salaryMin") or j.get("salaryMax"):
            sraw = f"{j.get('salaryMin')} - {j.get('salaryMax')} {j.get('salaryCurrency') or ''}"
        out.append({
            "title": j.get("jobTitle"), "company": j.get("companyName"),
            "location": geo, "url": j.get("url"), "salary_raw": sraw,
            "tags": " ".join(j.get("jobIndustry") or []) + " " + clean_html(j.get("jobExcerpt", "")),
            "published_raw": (j.get("pubDate", "")[:10]),
            "description": clean_html(j.get("jobDescription", "")),
        })
    return out

def fetch_workingnomads(session):
    d = fetch_json(session, "https://www.workingnomads.com/api/exposed_jobs",
                   headers={"User-Agent": UA})
    out = []
    for j in d:
        out.append({
            "title": j.get("title"), "company": j.get("company_name"),
            "location": j.get("location"), "url": j.get("url"),
            "salary_raw": None,
            "tags": " ".join(j.get("tags") or []),
            "published_raw": "",
            "description": clean_html(j.get("description", "")),
        })
    return out

def fetch_greenhouse(session):
    out = []
    for co in GREENHOUSE_BOARDS:
        try:
            d = fetch_json(session, f"https://boards-api.greenhouse.io/v1/boards/{co}/jobs",
                           params={"content": "true", "limit": 500})
            for j in d.get("jobs", []):
                loc = (j.get("location") or {}).get("name", "")
                out.append({
                    "title": j.get("title"), "company": (j.get("company_name") or co.upper()),
                    "location": loc, "url": j.get("absolute_url"), "salary_raw": None,
                    "tags": "", "published_raw": j.get("updated_at", "")[:10],
                    "description": clean_html(j.get("content", "")),
                })
            print(f"   [greenhouse:{co}] ok ({len(d.get('jobs', []))})")
        except Exception as e:
            print(f"   [greenhouse:{co}] skip ({type(e).__name__})")
    return out

def fetch_lever(session):
    out = []
    for co in LEVER_BOARDS:
        try:
            d = fetch_json(session, f"https://api.lever.co/v0/postings/{co}?mode=json")
            for j in d:
                wp = j.get("workplaceType") or []
                if wp and not any("remote" in (w or "").lower() for w in wp):
                    continue
                out.append({
                    "title": j.get("text"), "company": (j.get("company") or co.upper()),
                    "location": j.get("country") or j.get("location"),
                    "url": j.get("hostedUrl"), "salary_raw": None,
                    "tags": " ".join(j.get("tags") or []),
                    "published_raw": (j.get("createdAt", "") or "")[:10],
                    "description": clean_html(j.get("description", "")),
                })
            print(f"   [lever:{co}] ok ({len(d)})")
        except Exception as e:
            print(f"   [lever:{co}] skip ({type(e).__name__})")
    return out

def fetch_smartrecruiters(session):
    out = []
    for co in SMARTRECRUITERS_BOARDS:
        try:
            d = fetch_json(session, f"https://api.smartrecruiters.com/v1/companies/{co}/postings",
                           params={"limit": 100})
            for j in d.get("content", []):
                if not j.get("remote"):
                    continue
                loc = j.get("location") or {}
                country = loc.get("country") or ""
                out.append({
                    "title": j.get("name"), "company": (j.get("company") or co.upper()),
                    "location": f"{loc.get('city') or ''} {country}".strip(),
                    "url": j.get("ref"), "salary_raw": None,
                    "tags": "",
                    "published_raw": (j.get("releasedDate", "") or "")[:10],
                    "description": "",
                })
            print(f"   [smartrecruiters:{co}] ok ({len(d.get('content', []))})")
        except Exception as e:
            print(f"   [smartrecruiters:{co}] skip ({type(e).__name__})")
    return out

SOURCES = [
    ("remotive", fetch_remotive),
    ("remoteok", fetch_remoteok),
    ("weworkremotely", fetch_weworkremotely),
    ("jobicy", fetch_jobicy),
    ("workingnomads", fetch_workingnomads),
    ("greenhouse", fetch_greenhouse),
    ("lever", fetch_lever),
    ("smartrecruiters", fetch_smartrecruiters),
]

def probe():
    """Test connectivity and report which boards/companies respond."""
    session = requests.Session()
    session.headers["User-Agent"] = UA
    status = {"probed_at": now(), "boards": {}}
    for name, fn in SOURCES:
        try:
            n = len(fn(session))
            status["boards"][name] = {"ok": True, "count": n}
            print(f"OK   {name:<16} {n} postings")
        except Exception as e:
            status["boards"][name] = {"ok": False, "error": str(e)[:120]}
            print(f"FAIL {name:<16} {e}")
    with open(BOARD_STATUS, "w") as f:
        json.dump(status, f, indent=2)
    print("\nwrote", BOARD_STATUS)

# --------------------------------------------------------------------------
# main sync
# --------------------------------------------------------------------------
def sync():
    session = requests.Session()
    session.headers["User-Agent"] = UA
    master = load_postings()              # existing {owner, postings} master
    seen = load_seen()                    # url-hash -> id  (stability across syncs)
    pub = []                              # accepted this run
    rejected = {"not_remote_us": 0, "salary": 0, "dup": 0}
    by_board = {}

    for board, fn in SOURCES:
        try:
            raw = fn(session)
        except Exception as e:
            print(f"[{board}] FAILED: {e}"); continue
        by_board[board] = 0
        for r in raw:
            if not r.get("title") or not r.get("url"):
                continue
            loc = r.get("location") or ""
            if not remote_and_us(loc, board):
                rejected["not_remote_us"] += 1
                continue
            if (r.get("company") or r.get("employer") or "").strip() in MARKETPLACES:
                rejected["marketplace"] = rejected.get("marketplace", 0) + 1
                continue
            lane = classify_lane(r["title"], r.get("tags", "") + " " + (r.get("description") or ""))
            lo, hi, verified = parse_salary(r.get("salary_raw"))
            if not verified:
                if lane not in SALARY_UNVERIFIED_OK:
                    rejected["salary"] += 1
                    continue
            elif (hi or lo or 0) < 60000:
                rejected["salary"] += 1
                continue

            jid = posting_id(r["url"])
            if jid in seen:
                rejected["dup"] += 1
                continue
            seen[jid] = r["url"]

            salary_disp = r.get("salary_raw") or "not posted"
            if verified and lo:
                hi_s = f" - ${hi:,.0f}" if hi and hi != lo else ""
                salary_disp = f"${lo:,.0f}{hi_s}/yr (posted {r.get('salary_raw')})"
            elif not verified:
                salary_disp = "not posted (verify)"

            pub.append({
                "id": jid,
                "board": board,
                "employer": r.get("company") or "?",
                "title": r["title"],
                "location": loc,
                "remote": "US/world" if (GLOBAL_KW.search(loc) and not US_KW.search(loc)) else "US",
                "url": r["url"],
                "apply_url": r["url"],
                "salary": salary_disp,
                "salary_verified": verified,
                "salary_min": lo,
                "salary_max": hi,
                "posted": r.get("published_raw") or "",
                "description": (r.get("description") or "")[:2000],
                "apply_email": extract_apply_email(r.get("description")),
                "lane": lane,
                "hot_skills": hot_skills_for(lane, r.get("tags", "")),
                "angle": gen_angle(lane, board),
                "first_seen": now(),
                "match": lane,
            })
            by_board[board] += 1

    # merge into master (skip already-known ids statuses are preserved)
    known = {p["id"] for p in master["postings"]}
    added = [p for p in pub if p["id"] not in known]
    master["postings"].extend(added)
    save_postings(master)
    save_seen(seen)

    snap = {
        "synced_at": now(),
        "accepted_this_run": len(pub),
        "new_to_master": len(added),
        "by_board": by_board,
        "rejected": rejected,
    }
    with open(FRESH, "w") as f:
        json.dump(snap, f, indent=2)

    print("=" * 70)
    print("SYNC DONE")
    print(f"  fetched+accepted this run : {len(pub)}")
    print(f"  brand-new added to master : {len(added)}")
    print(f"  rejected (not US/remote)  : {rejected['not_remote_us']}")
    print(f"  rejected (salary <60k)    : {rejected['salary']}")
    print(f"  rejected (duplicate)      : {rejected['dup']}")
    print("  by board:")
    for b, n in sorted(by_board.items(), key=lambda x: -x[1]):
        print(f"    {b:<16} {n}")
    print(f"\nmaster now has {len(master['postings'])} postings")

# --------------------------------------------------------------------------
def gen_angle(lane, board):
    angles = _load_profile().get("lane_angles") or {}
    # neutral default angles ship with the repo; user overrides live in profile.json
    defaults = {
        "IT Ops": "I own the full stack solo and keep it running through unattended automation - "
                  "exactly the self-management a remote role demands.",
        "DevOps/Cloud": "My self-hosted production stack (Docker Compose, orchestrator, vector DB, local LLMs on Linux) "
                        "proves I can own infrastructure unattended.",
        "AI/ML": "I shipped a real multi-engine OCR + RAG pipeline with local LLMs and human-in-the-loop QA - "
                 "engineering credibility, not interest.",
        "QA/Test": "I built a regression gate into my own software for quality control; I know what "
                   "'good enough to ship' takes.",
        "Data": "I run pipelines that turn messy input into structured, verifiable output.",
        "Customer Support": "Front-line support years plus supervision taught me fast, accurate, "
                            "empathetic resolution.",
        "Business Ops": "I balance a production operation with running multiple automated pipelines - "
                        "organized, autonomous, detail-driven.",
        "Accounting/Finance": "I keep exacting records and reconcile real money with discipline.",
        "Sales/BD": "My portfolio and side-business work show I can own a pipeline end-to-end and "
                    "communicate clearly.",
        "Legal/Compliance": "I have worked public-sector and courthouse environments under audit "
                            "and compliance constraints.",
    }
    return angles.get(lane, defaults.get(lane, "I bring 15+ years of dependable, self-directed operations "
                                               "plus real engineering depth, and work comfortably fully remote."))

def _load_profile():
    p = os.path.join(BASE, "profile.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

def load_postings():
    if os.path.exists(POSTINGS):
        with open(POSTINGS) as f:
            return json.load(f)
    return {"owner_path": "./owner.json", "generated_by": "scrape.py", "postings": []}

def save_postings(m):
    with open(POSTINGS, "w") as f:
        json.dump(m, f, indent=2)

def load_seen():
    if os.path.exists(SEEN):
        with open(SEEN) as f:
            return json.load(f)
    return {}

def save_seen(s):
    with open(SEEN, "w") as f:
        json.dump(s, f, indent=2)

def stats():
    m = load_postings()
    from collections import Counter
    c = Counter(p["board"] for p in m["postings"])
    by_lane = Counter(p["lane"] for p in m["postings"])
    verified = sum(1 for p in m["postings"] if p.get("salary_verified"))
    print(f"master postings: {len(m['postings'])}  (salary verified: {verified})")
    print("\nper board:")
    for b, n in c.most_common():
        print(f"  {b:<16} {n}")
    print("\nper lane:")
    for b, n in by_lane.most_common():
        print(f"  {b:<20} {n}")

def backfill():
    """Re-fetch raw postings and patch description/apply_email onto existing
    master entries that lost them (they were fetched but never stored)."""
    session = requests.Session()
    session.headers["User-Agent"] = UA
    master = load_postings()
    by_id = {p["id"]: p for p in master["postings"]}
    patched = 0
    for board, fn in SOURCES:
        try:
            raw = fn(session)
        except Exception as e:
            print(f"[{board}] FAILED: {e}")
            continue
        for r in raw:
            if not r.get("url"):
                continue
            jid = posting_id(r["url"])
            p = by_id.get(jid)
            if p is None:
                continue
            desc = clean_html(r.get("description") or "")
            full = desc
            desc = desc[:2000]
            changed = False
            old = p.get("description") or ""
            dirty = ("&" in old and ("&lt;" in old or "&amp;" in old or "&#" in old))
            if ((not old.strip()) or dirty) and desc.strip():
                p["description"] = desc
                changed = True
            # extract from the FULL text: the apply/contact clause usually sits
            # past the 2000 chars we keep on the record
            ae = extract_apply_email(full)
            if ae != (p.get("apply_email") or "").strip():
                # empty result now clears a stale address: the extractor got
                # smarter about noise and no-unsolicited-email postings
                p["apply_email"] = ae
                changed = True
            if changed:
                patched += 1
        print(f"  [{board}] patched {patched - 0} so far")
    save_postings(master)
    emailed = sum(1 for p in master["postings"] if p.get("apply_email"))
    print(f"\nbackfill done: {patched} postings enriched; {emailed} total with apply_email")

def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "probe":
        probe()
    elif cmd == "sync":
        sync()
    elif cmd == "stats":
        stats()
    elif cmd == "backfill":
        backfill()
    else:
        print(__doc__)

if __name__ == "__main__":
    main()