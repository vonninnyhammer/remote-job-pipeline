#!/usr/bin/env python3
"""
Remote jobs application pipeline
================================
Aggregates remote job boards, filters to (remote + US + $60k+), and builds a
tailored, almost-one-click application kit per role - including real form
questions pre-drafted from a Greenhouse listing.

Workflow:  sync -> heat -> stage|generate -> review -> approve -> open -> submitted

Targets:  an id (rd03a97a), a kit name ("Banyan"), or any unique part of one.

  python3 scrape.py  sync                        # pull boards into postings.json
  python3 pipeline.py heat                        # ranked view of jobs (salary first)
  python3 pipeline.py status                      # current application statuses
  python3 pipeline.py stage [name|--all]          # ONE-CLICK: build kit, fetch the real form,
                                                  #   draft every answer, load clipboard, open browser
  python3 pipeline.py generate [name|--all]       # tailored kit per job (cover letter + sheet + resume)
  python3 pipeline.py review [name|--all]         # show what was generated
  python3 pipeline.py approve [name|--all]        # mark approved (gate: nothing opens until here)
  python3 pipeline.py open [name|--all]           # opens apply URL + prints paste-ready answers
  python3 pipeline.py submitted [name|--all]      # mark submitted - run this AFTER you actually send it
  python3 pipeline.py note <name> interview|offer|closed|skip <text>
  python3 pipeline.py kits                       # your kit list - same as `ls kits/`
  python3 pipeline.py shortlist                  # the focused hunt: remote IT generalist /
                                                  #   BizTech / sysadmin roles, vetoing MSP/NOC/
                                                  #   call-center shapes
  python3 pipeline.py export                      # write job tracker xlsx

Config:  owner.json (identity + paths) and profile.json (skill bullets and
cover-letter lanes) - both optional; see owner.example.json.

State lives in state.json; kits are written under kits/<Employer> - <Title> - $ask/.
Every command takes an id, a kit name, or any unique part of one.
"""
import json, os, re, sys, subprocess, datetime, shutil
import requests

try:
    from scrape import LANE_RE as SCRAPE_LANE_RE
except Exception:
    SCRAPE_LANE_RE = []

BASE = os.path.dirname(os.path.abspath(__file__))
POSTINGS = os.path.join(BASE, "postings.json")
STATE = os.path.join(BASE, "state.json")
KITS = os.path.join(BASE, "kits")
PROFILE_FILE = os.path.join(BASE, "profile.json")
OWNER_FILE = os.path.join(BASE, "owner.json")
RESUME_PDF = os.environ.get("RESUME_PDF") or os.path.join(BASE, "JordanGuison_Resume.pdf")
TRACKER_OUT = os.environ.get("TRACKER_OUT") or os.path.join(BASE, "Job_Tracker.xlsx")

VALID = ["submitted", "interview", "offer", "closed", "skip", "responded", "staged"]

# ---------------------------------------------------------------------------
# Kit directory naming
# ---------------------------------------------------------------------------
# Kits used to be kits/<posting id>/ (e.g. kits/rd03a97a/) which meant opening
# a kit - or asking the agent - to know what it was. They are now named
# "<Employer> - <Title> - <salary to ask>" so `ls kits/` is the tracker.
#
# The salary figure is a RECOMMENDATION, derived in this order:
#   1. posting publishes a verified range -> ask the top of that range
#   2. otherwise -> seniority tier table (SENIORITY_ASKS, overridable in
#      owner.json so the policy can be edited without touching code)
#   3. otherwise -> "salary TBD" (never invent a number)
# Provenance for every kit name is written to kits/.kitmap.json.

DEFAULT_SENIORITY_ASKS = [
    (("head of", "chief", "cto", "ceo", "founder"), 220000),
    (("principal", "distinguished", "fellow"), 200000),
    (("staff",), 195000),
    (("director", "vp", "vice president"), 190000),
    (("lead", "architect"), 165000),
    (("manager",), 145000),
    (("senior", "sr.", "sr "), 150000),
    (("entry level", "entry-level", "entrylevel", "graduate", "trainee",
      "junior", "jr.", "intern", "apprentice"), 70000),
]
DEFAULT_TIER_ASK = 110000          # no seniority keyword found
BAD_NAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _clean_name_part(s, limit):
    s = BAD_NAME_CHARS.sub("", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    return s[:limit].strip(" .,-")


def _fmt_money(n):
    n = int(round(n))
    return f"${n // 1000}k" if n % 1000 == 0 else f"${n:,}"


def _round_ask(n):
    """Round up to a clean $5k. A posted range top of $235,400 is a real
    number but a bad ask; $240k is the same money and reads deliberate."""
    n = int(round(n))
    return int(-(-n // 5000) * 5000)


MONEY_RE = re.compile(r"\$\s?([\d,]{4,9})")
_WORD_RE = re.compile(r"[^a-z0-9]+")


def _norm_words(s):
    """Lowercase, collapse every non-alphanumeric run to a single space.
    Substring matching on raw titles is how 'cto' ended up matching
    'Public Sector' and promoting 91 roles to the $220k tier."""
    return " " + _WORD_RE.sub(" ", (s or "").lower()).strip() + " "


def _tier_hit(norm_title, table):
    for keys, amount in table:
        for k in keys:
            if f" {_WORD_RE.sub(' ', k.lower()).strip()} " in norm_title:
                return k, amount
    return None, None


def recommended_ask(p, owner):
    """(amount_or_None, basis) for the salary to request on this role."""
    # a number Jordan deliberately set for THIS posting wins over every heuristic
    ca = p.get("comp_answer") or ""
    if ca:
        nums = [int(x.replace(",", "")) for x in MONEY_RE.findall(ca)]
        nums = [n for n in nums if 10000 <= n <= 2000000]
        if nums:
            return _round_ask(max(nums)), "your comp_answer for this role"
    lo, hi = p.get("salary_min"), p.get("salary_max")
    # a range whose top is wildly above its floor is a scraping artifact, not a
    # band (e.g. "$52,000 - $353,000" for a remote office assistant) - asking the
    # top of it would be nonsense, so fall through to the tier instead
    sane = (p.get("salary_verified") and hi and float(hi) > 0
            and (not lo or float(hi) <= 4 * float(lo or hi)))
    if sane:
        exact = int(float(hi))
        return _round_ask(exact), f"top of posted range ({_fmt_money(exact)} exact)"
    floor = owner.get("target_salary_min") or 0
    table = owner.get("seniority_asks") or [[list(k), a] for k, a in DEFAULT_SENIORITY_ASKS]
    key, amount = _tier_hit(_norm_words(p.get("title")), table)
    if key is not None:
        return max(_round_ask(amount), int(floor)), f"seniority tier ({key})"
    if floor:
        return max(_round_ask(DEFAULT_TIER_ASK), int(floor)), "default tier"
    return None, "no basis"


def kit_name(p, owner):
    """Human-readable kit directory name: Employer - Title - $ask."""
    emp = _clean_name_part(p.get("employer") or p.get("company") or "Unknown", 48)
    title = _clean_name_part(p.get("title") or "Untitled", 90)
    amt, _basis = recommended_ask(p, owner)
    pay = _fmt_money(amt) if amt else "salary TBD"
    name = f"{emp} - {title} - {pay}"
    # keep the whole path inside the 255-byte filename limit
    budget = 255 - len(os.path.join(KITS, "").encode()) - 1
    while len(name.encode()) > budget and len(title) > 12:
        title = title[: int(len(title) * 0.85)].rstrip(" .,-")
        name = f"{emp} - {title} - {pay}"
    return name


def _owner_cached():
    if os.path.exists(OWNER_FILE):
        with open(OWNER_FILE) as f:
            return json.load(f)
    return {}


def kit_dir(p, owner=None):
    """Absolute kit path. Uses the posting's recorded kit_name when present so
    call sites that only have the posting (fetch_questions) still resolve."""
    owner = owner if owner is not None else _owner_cached()
    name = p.get("kit_name") or kit_name(p, owner)
    return os.path.join(KITS, name)


def load_kitmap():
    fp = os.path.join(KITS, ".kitmap.json")
    if os.path.exists(fp):
        with open(fp) as f:
            return json.load(f)
    return {}


def save_kitmap(m):
    os.makedirs(KITS, exist_ok=True)
    with open(os.path.join(KITS, ".kitmap.json"), "w") as f:
        json.dump(m, f, indent=2, sort_keys=True)


def record_kit(p, owner):
    """Pin the kit name onto the posting and record its provenance."""
    name = kit_name(p, owner)
    p["kit_name"] = name
    amt, basis = recommended_ask(p, owner)
    p["recommended_ask"] = amt
    p["recommended_ask_basis"] = basis
    m = load_kitmap()
    m[p["id"]] = {"kit_name": name, "employer": p.get("employer"),
                  "title": p.get("title"), "ask": amt, "ask_basis": basis}
    save_kitmap(m)
    return name


# Neutral defaults for a public checkout. Your personal copy lives in profile.json
# (skill_bullets / lane_openers) and owner.json (identity, override everything below).
DEFAULT_SKILL_BULLETS = {
    "Linux": "Daily Linux administration plus Windows enterprise environments - comfortable owning a mixed stack unattended.",
    "Cloud/Virtualization": "VMware, cloud and containerized deployments (Docker Compose) run like production services with tunneled/zero-inbound networking.",
    "Desktop/Endpoint": "End-to-end desktop/endpoint support: imaging, deployment, identity, inventory, and secure disposal.",
    "Windows/AD/SCCM": "Active Directory, SCCM/Intune, Exchange/365, BitLocker and EDR across enterprise environments.",
    "Networking (TCP/IP/VLAN)": "OSI/TCP-IP troubleshooting, DNS/DHCP, VLAN, firewall, VPN/IPsec and VoIP/SIP; hands-on switch/server installation.",
    "Security/InfoSec": "Security-conscious operations: EDR, BitLocker, least-privilege access, VPN hardening, privacy-first architecture.",
    "SQL/Databases": "SQL scripting for task automation and data work that requires exacting accuracy.",
    "AI/ML/OCR": "Production document-intelligence pipelines: OCR engines under a consensus layer, RAG, local LLMs - architecture published.",
    "DevOps/Containers": "Docker Compose multi-container stacks, workflow orchestration, repeatable build/deploy tooling.",
    "App Support/Implementation": "Tier-2 application support, deployment facilitation and vendor/approval workflows.",
    "Bilingual": "Fluent in a second language; comfortable with international customers.",
    "Remote Discipline": "Proven autonomous remote operator: self-hosted production systems, async-first collaboration, documented work, on-call ownership.",
}

DEFAULT_LANE_OPENERS = {
    "IT Ops": "I have run IT operations end-to-end - endpoints, networks, identity, security, inventory - and can own this scope from day one, independently.",
    "DevOps/Cloud": "I run production containerized infrastructure and networking like a real service - the ownership this role needs.",
    "AI/ML": "I have shipped real machine-learning systems in production, not just studied them - engineering credibility, not enthusiasm.",
    "QA/Test": "I build quality gates into my own software and ship work that must be exact - QA discipline is second nature.",
    "Data": "I turn messy inputs into structured, verifiable outputs - SQL, pipelines, and workflows where one wrong row is one wrong result.",
    "Customer Support": "Years on support front lines taught me fast, empathetic, script-compliant-but-human resolution.",
    "Business Ops": "I run multi-track operations by myself - autonomous, organized, comfortable owning cross-functional outcomes.",
    "Accounting/Finance": "I keep exacting records and reconcile real money with discipline.",
    "Sales/BD": "I communicate comfortably with technical and nontechnical people and own a pipeline end-to-end.",
    "Legal/Compliance": "Comfortable in compliance-heavy, audited environments - careful, documented, low-drama work.",
}

def _load_profile():
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE) as f:
            return json.load(f)
    return {}

PROFILE_JSON = _load_profile()
PROFILE = dict(DEFAULT_SKILL_BULLETS, **PROFILE_JSON.get("skill_bullets", {}))
LANE_OPENERS = dict(DEFAULT_LANE_OPENERS, **PROFILE_JSON.get("lane_openers", {}))

# ---------------------------------------------------------------------------
# Target-fit filter: remote IT OR dev/engineering roles, $60k+.
#   target   = titled as sysadmin/IT Ops/endpoint/SaaS/GWS/MDM/Intune/BizTech
#              OR titled as a dev/engineering role (AI/ML, DevOps/Cloud, Data,
#              QA/Test, or generic software/platform/cloud engineering)
#   watch    = weak IT-ops signal that needs a human look (generic support,
#              helpdesk, service desk, desktop, technician, analyst)
#   veto     = metered / surveilled or staffing shapes (MSP, NOC, call center,
#              tiered call-center support, staffing agencies) -> hard pass
#   other    = nothing to do with this hunt
# ---------------------------------------------------------------------------
# Every alternative is \b-anchored on purpose. Unanchored, `it\s+operations`
# matched INSIDE "Cr-ed-it Operations Manager" and put Stripe's credit-operations
# role in the shortlist; the same defect class as the `disabilit` stem bug in
# _eeo_self_id. A short fragment like "it ..." or "intune" embedded in an
# unrelated word is exactly how a finance or revenue role ends up filed as IT.
TARGET_TITLE_RE = re.compile(r"""
    \bsysadmin|\bsystem(?:s)?\s*admin|\bit\s+admin(?:istrator)?\s*$|
    \bit\s+operations|\bit\s+ops(?:eration)?s?\b|\bit\s+generalist|\bit\s+business\s+partner|
    \bcorporate\s+it|\bbusiness\s+technology|\bbiztech|\bit\s+manager|\bit\s+director|
    \bendpoint|\bend\s?user\s?computing|\beuc\b|\bmdm\b|\bintune|\bsaas\s+(?:admin(?:istrator)?|ops|operations)|
    \bgoogle\s?workspace|\bm365\s?admin|\bmicrosoft\s?365\s?admin|\bidentity(?:&|\s*and\s*)access|\biam\s+admin|
    \bazure\s+admin(?:istrator)?|\bcloud\s+admin(?:istrator)?|\binfrastructure\s+admin(?:istrator)?|
    \bit\s+infrastructure|\bit\s+services\s*admin|\bsystems\s+administrator|\bnetwork\s+administrator|
    \bnetwork\s+admin|\bit\s+asset\s+manager|\bit\s+procurement|\bit\s+coordinator|\bit\s+specialist
""", re.I | re.X)
# weaker signals - target-ish, but needs human eyes to confirm scope
WATCH_TITLE_RE = re.compile(r"""
    it\s+support|it\s+technician|it\s+analyst|it\s+services|it\s+general|
    technical\s+support|desktop\s+support|desktop\s+technician|help\s?desk|
    service\s+desk|computer\s+technician|it\s+technology|technology\s+specialist
""", re.I | re.X)
# Jordan ruled 2026-09-27, both of these are in-lane for him. They are checked
# BEFORE title_lane() on purpose: SCRAPE_LANE_RE is first-match-wins and the
# AI/ML lane swallows "AI Success Manager" (his Glean kit) before Customer
# Support ever sees it - the lane-ordering trap, third instance.
# FP&A is in because he built kits for Luxury Presence "Senior Financial
# Analyst, GTM" and treats the data-analyst family as his lane.
IN_LANES_TITLE_RE = re.compile(r"""
    (?:ai|technical| solutions?)\s+success\s+(?:manager|engineer)|
    \bfp\s*&\s*a\b|financial\s+analyst|financial\s+planning\s+analyst|
    \bsr\.?\s+financial\b|revenue\s+operations
""", re.I | re.X)
VETO_RE = re.compile(r"""
    \bmsp\b|managed\s+service|network\s+operations|\bnoc\b|call\s*center|contact\s*center|
    telemarketing|telesales|outbound|tier\s*(?:[12]\b|i{1,2}\b|iv\b)|\bt2\b|\bt1\b|technical\s+support\s+specialist\s*\d|
    customer\s+(support|service)\s*(?:rep|agent)|staffing|temporary|temp-to|outsourcing|
    workforce\s+solutions|recruiting\s+services|field\s+engineer(?:ing)?|pre-sales|
    solutions?\s+engineer|technical\s+consultant|arcgis|\brepresentative\b|
    premier|live\s+technical
    # Jordan ruled 2026-09-27, against the kit he had already built for Unio
    # Digital "Tier III Service Desk Engineer": service desk IS his lane.
    # `\bshift\b` came out for the same reason - he picked GitLab's
    # "Intermediate Support Engineer (SHIFT)", nights and all. `technical
    # support engineer` is still vetoed and is his call, not mine.
""", re.I | re.X)
# Gig annotation / microtask "QA" is not QA engineering. TELUS Digital's
# "Quality Assurance Rater - German" and iMerit's "Video Data Annotator" both
# match the QA/Test and Data lanes and were landing in target, which is worse
# than useless: it is a language-rater gig that pays nothing. QA/Test and Data
# promote on lane name alone, so this has to veto the shape by title.
GIG_RE = re.compile(r"\b(?:qa|quality\s+assurance|quality)\s+rater\b|\brater\b|"
                    r"\b(?:video|data|audio|image)\s+(?:data\s+)?annotator\b|"
                    r"\bannotation\s+worker\b|\bmicrotask\b|\bclickworker\b|"
                    r"\btranscription(?:ist| reviewer)?\b|\bcontent\s+moderator\b", re.I)

# employer-name heuristic for a stability hint in the shortlist (a nudge, not a verdict)
STABLE_EMPLOYER_RE = re.compile(r"""
    health|hospital|clinic|medical|university|college|school|county|metro|state\s+of|
    city\s+of|government|transit|electric|utility|bank|credit\s+union|insurance|
    healthcare|energy|laborator|manufactur|distribution|logistics|food|service\s+center
""", re.I)

# Lanes that match Jordan's actual 15-year track record. He holds kits in every
# one of these (GitLab Support, Unio Digital Tier III desk, Ace IT QA tester,
# Chime/Clover data analyst, CentralReach quality, Canonical Linux systems).
REAL_LANES = {"IT Ops", "Customer Support", "QA/Test", "Data", "Admin Ops", "DevOps/Cloud"}
# Dev/ML is a documented STRETCH and must never bucket as "target". The HIRE-EASE
# note lower in this file says it outright: self-taught dev/ML is "real but NOT a
# professional full-stack / ML engineer's resume", with "no formal degree, no
# commercial software-engineering employment history". fit_bucket used to promote
# every AI/ML title and every DEV_GENERIC hit to "target" anyway, which is how 47
# GitLab + 24 Chime senior-SWE reqs landed in the same bucket as a Tier III
# service-desk req. A stretch posting is still shown, still stageable, and is
# excluded from autosend and from the default sweep.
STRETCH_LANES = {"AI/ML"}
# Dev-shaped titles carrying no lane at all, e.g. "Staff Backend Engineer".
# Same reasoning: product-engineering reqs, not infrastructure. "systems" and
# "data" are deliberately NOT here: they stole "Desktop Linux Systems Engineer"
# (Canonical) and "Senior Data Engineer" (Socure), both of which Jordan has a
# kit for and both of which are squarely his lane.
DEV_GENERIC_RE = re.compile(r"""\b(software|web|full-?stack|front-?end|back-?end|application|
    platform|api|mobile)\s*(engineer|developer|architect)\b|
    \b(site.?reliability|\bsre\b|devops)\s+engineer\b|\bbuild\s+engineer\b""", re.I | re.X)
# "Customer Success Manager" is a quota/revenue role, not IT support, but both
# SUPPORT_RE and the Customer Support lane claim it. Narrowed to the revenue
# family only, so Jordan's own "AI Success Manager" (Glean) still lands as a
# target the way his kit says it should.
CSM_RE = re.compile(r"customer\s+success|sales\s+(?:account|development)|\baccount\s+executive\b|"
                    r"business\s+development|\bpartnerships?\b", re.I)
# The REAL_LANES fallback below promotes any title the scraper filed under a
# "real" lane, and a lane is a TOPIC, not a job function. "Data" filed Data
# Analyst / Analytics Manager / Product Manager; "DevOps/Cloud" filed Solutions
# Architect and Golang Kubernetes Engineer. That put 71 of 85 shortlisted posts
# in the queue on the lane alone, with no title match at all - which is why the
# queue read as DevOps/security/analytics noise. These shapes are excluded from
# the FALLBACK ONLY: TARGET_TITLE_RE and IN_LANES_TITLE_RE are checked earlier
# and still win, so a title Jordan deliberately put in-lane is never lost here.
# Deliberately NOT excluded: "data engineer", "systems engineer", "support
# engineer", "cloud support engineer" - he has kits for Senior Data Engineer
# (Socure), Database Support Engineer (Supabase) and Cloud Support Engineer
# (Canonical), and those three arrived through this fallback.
NON_FUNCTION_RE = re.compile(r"""
    \b(?:data|business|product|revenue|gtm|marketing|compensation|internal|
       people|finance|financial|security|network|cloud|web)\s+analyst\b
  | \banalytics\b | \bbusiness\s+intelligence\b
  | \bproduct\s+manager\b
  | \b(?:solutions?|pre-?sales|enterprise)\s+architect\b
  | \b(?:client|customer)\s+success\b
  | \baccount\s+(?:executive|manager)\b
  | \b(?:sr\.?|snr\.?|senior)\s+(?:manager|director)\b
  | \b(?:manager|director|vp|vice\s+president|head\s+of|chief)\b
  | \b(?:golang|rust|java|python|kotlin)\s+(?:software\s+)?engineer\b
  | \bkubernetes\s+engineer\b | \bplatform\s+engineer\b
  | \bcore\s+devops\b | \bci/cd\b
  | \bsoftware\s+(?:development\s+)?(?:engineer|developer)\b
  | \b(?:services?|cloud)\s+architect\b
  | \bcompliance\b
""", re.I | re.X)
# Left in deliberately, because Jordan has not ruled on them and they are real
# adjacent careers rather than obvious noise: security engineering (Cloud/Infrastructure
# Security Engineer), QA/Test (Quality Engineer, QA Tester), and staff/principal
# IC cloud work. They are listed in the sweep output so he can judge per role
# instead of the classifier silently deciding for him.

def title_lane(title):
    for name, rx in SCRAPE_LANE_RE:
        if rx.search(title or ""):
            return name
    return ""

def fit_bucket(p):
    title = p.get("title", "")
    text = f"{title} {p.get('employer', '')}"
    if VETO_RE.search(text) or GIG_RE.search(title):
        return "veto"
    if TARGET_TITLE_RE.search(title):
        return "target"
    if IN_LANES_TITLE_RE.search(title):
        return "target"
    if WATCH_TITLE_RE.search(title):
        return "watch"
    # Product-engineering shapes are checked BEFORE the lane lookup on purpose.
    # The lane regexes are first-match-wins, and the DevOps/Cloud lane is broad
    # enough to swallow "Senior Backend Engineer, Core DevOps" - so a lane-first
    # check silently promoted GitLab staff-SWE reqs to target. A title that is
    # unambiguously product engineering is a stretch no matter which lane it was
    # assigned.
    if DEV_GENERIC_RE.search(title) or CSM_RE.search(title):
        return "stretch"
    lane = title_lane(title)
    if lane in REAL_LANES:
        # A topic lane plus a non-IT job function is not an IT Ops role.
        if NON_FUNCTION_RE.search(title):
            return "other"
        return "target"
    if lane in STRETCH_LANES:
        return "stretch"
    return "other"

# ---------------------------------------------------------------------------
# Pursuance: is a role worth Jordan's time at all?
#
# Jordan, 2026-09-27: "whether they are in my lane depends on to what extent
# I am able to automate the job duties countered with how high the pay is."
# Lane membership is therefore necessary but not sufficient, and pay was used
# only as a sort key before - which meant a $385k security role and a $60k
# support role scored identically. The trade-off is an explicit table rather
# than a formula so a human can read it and overrule it.
#
# automate: 3 = mostly hand-off-able to agents, 2 = about half,
#           1 = mostly human judgement/accountability, 0 = none.
# Checked in order, so the most automatable match wins.
AUTOMATE_3_RE = re.compile(r"""
    \bsysadmin|\bsystems?\s+(?:admin|administrator)|\bit\s+(?:admin|administrator|technician|specialist|coordinator)\b|
    \bendpoint|\beuc\b|\bmdm\b|\bintune|\bgoogle\s?workspace|\bm365\b|\bmicrosoft\s?365\b|
    \bnetwork\s+(?:admin|administrator|engineer)\b|\bhelp\s?desk|\bservice\s+desk|
    \bit\s+support|\bdesktop\s+support|\btechnical\s+support|\bcomputer\s+technician|
    \bsupport\s+engineer|\btechnical\s+account\s+manager|
    \bsystems?\s+engineer|\binfrastructure\s+(?:engineer|specialist)\b|
    \bcloud\s+support\s+engineer|\bdatabase\s+support\s+engineer|
    \bidentity\s+(?:and|&)\s+access|\biam\s+admin|\bworkday\b|\berp\b|
    \bbusiness\s+systems?\s+admin|\bsalesforce\b
""", re.I | re.X)
AUTOMATE_2_RE = re.compile(r"""
    \bfp\s*&\s*a\b|\bfinancial\s+(?:analyst|planning)|\bquality\s+engineer\b|
    \b(?:ai|technical)\s+success|\bdata\s+engineer|
    \btechnical\s+account\s+manager|\bimplementations?\s+(?:engineer|consultant)\b
""", re.I | re.X)
AUTOMATE_1_RE = re.compile(r"""
    \bsecurity\s+(?:engineer|analyst|architect)|\bqa\b|\btester\b|\bquality\b|\bqms\b|
    \bsolutions?\s+(?:specialist|consultant)|\brevenue\b|\bsuccess\b|
    \baccount\s+executive|\bgis\b|\btechnical\s+recruiter\b
""", re.I | re.X)

# Pay tiers, from Jordan's ruling: three tiers, $200k / $120k.
# 3 = >=200k, 2 = 120k-200k, 1 = <120k, 0 = no pay data anywhere.
#
# Pay is backfilled from kits/.kitmap.json when a posting has no range of its
# own. That file records the ASK Jordan set when he built each kit, which is
# NOT the same number as what the employer posted, so provenance is kept and
# surfaced rather than silently merged - "posted" and "ask" are different
# claims and the table below only ever sorts on real posted ranges when one
# exists. Without this backfill 37 of 45 shortlist roles looked "unpriced",
# including roles he had already engaged with and written a kit for.
def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def load_kit_pay(root=None):
    """(employer, title) -> {"ask": int, "basis": str}, from kits/.kitmap.json."""
    root = root or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, "kits", ".kitmap.json")
    out = {}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return out
    for rec in (data.values() if isinstance(data, dict) else data):
        try:
            emp, title = _norm(rec.get("employer")), _norm(rec.get("title"))
            ask = rec.get("ask")
            if emp and title and ask:
                out[(emp, title)] = {"ask": int(ask), "basis": rec.get("ask_basis", "")}
        except Exception:
            continue
    return out

_KIT_PAY = None
def kit_pay(p):
    """Jordan's own ask for this role, if he already built a kit for it."""
    global _KIT_PAY
    if _KIT_PAY is None:
        _KIT_PAY = load_kit_pay()
    emp, title = _norm(p.get("employer")), _norm(p.get("title"))
    if not emp or not title:
        return None
    hit = _KIT_PAY.get((emp, title))
    if hit:
        return hit
    # Kit titles drift from posting titles ("Senior Data Analyst, GTM Analytics"
    # vs "Senior Financial Analyst, GTM"), so fall back to a token-overlap
    # match within the same employer rather than losing the pay entirely.
    best = None
    for (e, t), v in _KIT_PAY.items():
        if e != emp:
            continue
        a, b = set(t.split()), set(title.split())
        if not a or not b:
            continue
        jac = len(a & b) / len(a | b)
        if jac >= 0.5 and (best is None or jac > best[0]):
            best = (jac, v)
    return best[1] if best else None

def pay_source(p):
    """("posted"|"ask"|None, value) - what the number is and where it came from."""
    v = p.get("salary_max")
    if v and v > 0:
        return "posted", int(v)
    k = kit_pay(p)
    if k:
        return "ask", k["ask"]
    return None, 0

def pay_tier(p):
    _src, v = pay_source(p)
    if not v or v <= 0:
        return 0
    if v >= 200_000:
        return 3
    if v >= 120_000:
        return 2
    return 1

def automate_score(title):
    t = title or ""
    if GIG_RE.search(t):
        return 3          # gig/annotated QA is the most hand-off-able work there is
    for score, rx in ((3, AUTOMATE_3_RE), (2, AUTOMATE_2_RE), (1, AUTOMATE_1_RE)):
        if rx.search(t):
            return score
    return 1              # unlisted shapes default to human judgement, not to agent-doable

def pursuit(p):
    """pursue / maybe / skip / unpriced - see the table above."""
    a, pay = automate_score(p.get("title", "")), pay_tier(p)
    if pay == 0:
        return "unpriced"          # no data: Jordan's call, never auto-applied
    if pay == 3:
        return "pursue"            # >=200k applies regardless of automability
    if pay == 2:
        return "pursue" if a >= 2 else "maybe"
    return "pursue" if a >= 3 else "skip"   # under 120k needs to be highly automatable

def stability_hint(p):
    if not STABLE_EMPLOYER_RE.search(p.get("employer", "")):
        return ""
    sectors = [
        "health", "hospital", "clinic", "medical", "healthcare",
        "university", "college", "school", "county", "metro", "government", "transit",
        "electric", "utility", "bank", "credit union", "insurance",
        "energy", "laborator", "manufactur", "distribution", "logistics", "food",
    ]
    for s in sectors:
        if re.search(s, p.get("employer", ""), re.I):
            return s
    return "stable-sector (see employer)"

# ---------------------------------------------------------------------------
# HIRE-EASE RATING (A = apply now, B = apply soon, C = stretch, D = skip)
# Based on Jordan's ACTUAL track record (from resume + guison.net + github):
#   * 15+ yrs IT: sole admin of 200-user facility, county courthouse enterprise
#     tech, SCCM/AD/M365/Citrix/Intune/EDR, WMS, helpdesk -> strong.
#   * Dev/ML: self-taught, shipped OCR/HTR pipeline, RAG, Docker, Python, local
#     LLMs -> real but NOT a professional full-stack / ML engineer's resume.
#   * No formal degree, no commercial software-engineering employment history,
#     no public GitHub org recognition; LinkedIn not maintained.
# This is a heuristic - it flags likely-fit vs stretch, not a guarantee.
# ---------------------------------------------------------------------------
SENIOR_RE = re.compile(r"senior|sr\.|\bstaff\b|principal|lead|architect|head of|director|"
                       r"\bvp\b|expert|champion|\d+\+\s*years", re.I)
JUNIOR_RE = re.compile(r"junior|jr\.|entry|\bassociate\b|graduate|\bnew grad\b|\bintern\b|"
                       r"trainee|early.?career|\b(?:level|tier)\s*i{1,3}\b", re.I)

# precise, self-contained signals (the shared scrape lane regexes are too broad:
# \bai\b eats 'Success Manager', bare 'uat' eats 'Situation')
CORE_IT_RE = re.compile(r"sysadmin|system administr|it (admin|generalist|technician|specialist|coordinator|"
                        r"support|services)|network (admin|technician|admin|engineer|administrator)|"
                        r"desktop( support| technician| engineer)|\bendpoint\b|\beuc\b|\bmdm\b|"
                        r"intune|site (admin|administrator|technician)|infrastructure admin|"
                        r"cloud admin|azure admin|m365 admin|google workspace|\bsaas admin\b|"
                        r"it business partner|biztech|corporate it|help ?desk|it operations administrator|"
                        r"(linux|ubuntu|windows) (systems? |infrastructure )?engineer|"
                        r"linux (admin|administrator|systems|sysadmin)|linux engineer", re.I)
QA_RE = re.compile(r"\bqa\b|quality (assurance|analyst|engineer|tester)|sdet|"
                   r"test (engineer|automation|analyst)|automation tester|regression", re.I)
SUPPORT_RE = re.compile(r"customer support|support engineer|technical support|tier[ -]?[1i]?|"
                        r"situation|success manager|client support|\bdesk\b|front[- ]line", re.I)
DATA_ANALYST_RE = re.compile(r"data analyst|business (intelligence|analyst)|bi analyst|"
                             r"reporting analyst|sql (developer|analyst)|power bi|tableau", re.I)
DEV_ML_RE = re.compile(r"ml engineer|machine learning engineer|ai engineer|llm|nlp|rag|"
                       r"data engineering|backend engineer|software engineer|devops|sre|"
                       r"site reliability|full.?stack|front.?end|back.?end|platform engineer|"
                       r"systems engineer|infrastructure engineer|cloud engineer", re.I)
DEV_ROLE_RE = re.compile(r"engineer|developer|architect|programmer|scientist|researcher", re.I)
DATA_SCI_RE = re.compile(r"data scientist|ml scientist|research scientist|ml researcher", re.I)

def rate_posting(p):
    """Return (grade, reasons[]) for hire-ease given Jordan's real background."""
    title = (p.get("title") or "").lower()
    reasons = []

    senior = bool(SENIOR_RE.search(title))
    junior = bool(JUNIOR_RE.search(title))
    mgmt = bool(re.search(r"manager|director|head of|supervisor|principal|architect|lead\b", title))
    is_eng = bool(DEV_ROLE_RE.search(title))

    # ---- hard caps first ------------------------------------------------
    if re.search(r"rater|annotator|moderat|labeler|grader|curat|scaler\b|transcrib", title):
        return "D", ["content rating/labeling - not QA, low-value"]
    if re.search(r"(sales|account executive|growth|bdr|sdr)", title) and "support" not in title:
        return "D", ["sales/BD track - not your background"]
    if DATA_SCI_RE.search(title):
        return "D", ["data/ML science needs formal stats credentials"]
    if re.search(r"(ceo|cfo|cto|president|founder)", title):
        return "D", ["exec role"]
    if mgmt and is_eng and not CORE_IT_RE.search(title):
        # eng/PM manager - still an engineering-management role
        return "C", ["engineering/PM management - you're IC+supervisor, not eng mgr"]

    # ---- core IT / admin track (your 15-yr strong suit) -------------------
    if CORE_IT_RE.search(title) and not is_eng:
        if re.search(r"(engineer|architect|manager)", title):
            return ("B"), ["core IT/admin but titled engineer/senior - strong but higher bar"]
        return "A", ["core IT/admin - direct 15-yr match"]
    if CORE_IT_RE.search(title) and is_eng:
        return ("B" if senior else "B"), ["core-IT variant of an eng title - real match"]

    # ---- QA -------------------------------------------------------------
    if QA_RE.search(title):
        return "A", ["QA discipline real (CER gate, human-in-loop)"]

    # ---- support (your Comcast track) -------------------------------------
    if SUPPORT_RE.search(title):
        if salary_below_p6k(p):
            return "B", ["support OK but posted under $60k - verify"]
        return "B", ["support track - 8 yrs Comcast + supervision, very hireable"]

    # ---- data analyst (SQL + ballot SQL + pipelines are real) --------------
    if DATA_ANALYST_RE.search(title):
        return ("C" if senior else "B"), ["SQL + pipelines real; " +
                ("senior data needs more" if senior else "mid data analyst/BI plausible")]

    # ---- dev/ML -----------------------------------------------------------
    if DEV_ML_RE.search(title):
        if junior:
            return "B", ["junior dev/ML - worth a shot, Python + Docker real"]
        if senior:
            return "C", [f"{title.strip()} - senior dev/ML bar higher than your resume"]
        return "D", [f"{title.strip()} - product-engineering/ML role, not your track"]

    # ---- catch-all: what's left in the hunt -------------------------------
    if junior:
        return "B", ["junior-level title - viable if topic overlaps your stack"]
    if support_like(title):
        return "C", ["support-adjacent title - judge scope"]
    return "D", ["falls outside your demonstrated track"]

def support_like(title):
    return bool(re.search(r"support|help|customer|client|specialist|associate|representative|coordinator", title))

def lane_qa(tl):
    return tl == "QA/Test"

def salary_below_p6k(p):
    mx = p.get("salary_max") or 0
    return bool(p.get("salary_verified")) and 0 < mx < 60000

def load_postings():
    with open(POSTINGS) as f:
        return json.load(f)

def load_owner(postings):
    if os.path.exists(OWNER_FILE):
        with open(OWNER_FILE) as f:
            return json.load(f)
    ow = postings.get("owner", {})
    return ow if isinstance(ow, dict) else {}

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {}

def save_state(st):
    with open(STATE, "w") as f:
        json.dump(st, f, indent=2)

def get(p, st):
    s = st.get(p["id"], {})
    return s.get("status", "new"), s.get("notes", "")

def set_status(p, st, status, note=None):
    entry = st.setdefault(p["id"], {})
    entry["status"] = status
    if note is not None:
        entry["notes"] = note
    entry["updated"] = datetime.datetime.now().isoformat(timespec="minutes")

# ---------- kit generation ----------
def pick_bullets(p, owner, n=4):
    prefs = p.get("hot_skills", [])
    keys = list(PROFILE.keys())
    out = []
    for sk in prefs:
        if sk in PROFILE and sk not in out:
            out.append((sk, PROFILE[sk]))
    for extra in ["Remote Discipline", "Desktop/Endpoint", "Linux",
                  "Networking (TCP/IP/VLAN)", "Security/InfoSec", "SQL/Databases"]:
        if len(out) >= n:
            break
        if extra not in [k for k, _ in out]:
            out.append((extra, PROFILE[extra]))
    return out[:n]

def gen_cover_letter(p, owner):
    lane = p.get("lane", "Other")
    openers = LANE_OPENERS
    line = openers.get(lane, "My skillset maps directly to this remote role.")
    if lane == "Customer Support" and p.get("hot_skills"):
        pass
    bullets = pick_bullets(p, owner)
    body = "\n".join(f"  - {txt}" for _, txt in bullets)
    verif = " (posted salary)" if p.get("salary_verified") else ""
    salary = f" {p['salary']}" if p.get("salary") else ""
    return f"""Subject: {p['title']} - {p['employer']} (Remote) application

{p['employer']} / {p['title']}
{owner['name']} | {owner['phone']} | {owner['email']} | Portfolio: {owner.get('portfolio', 'guison.net')}
Residence (for this application): {owner['location_use']}

Dear Hiring Team,

{line}

I am seeking fully remote work and am based in {owner['location_use'].split('-')[-1].strip() or 'the US'}
({owner['timezones']}); I am {owner['available']}.

Relevant experience:

{body}

{p['angle']}

I have attached my resume ({owner.get('resume_pdf', owner['name'].split()[-1] + '_Resume.pdf')}) and would welcome the
opportunity to speak about how I can support {p['employer']} remotely. My engineering portfolio
with architecture details and a troubleshooting log is at guison.net. I am happy to complete any
technical screen or skills assessment you use.

Respectfully,
{owner['name']}
{owner['phone']} | {owner['email']}
"""

def gen_app_sheet(p, owner):
    """Paste-ready accelerator for ATS forms."""
    sal = p.get("salary") or "not posted"
    return f"""# {p['employer']} - {p['title']}  ({p.get('board', '?')})
Full name .......... {owner['name']}
Email .............. {owner['email']}
Phone .............. {owner['phone']}
Current location ... Littlestown, PA 17340 (US)
Address (use) ...... {owner['location_use']}
Portfolio .......... {owner.get('portfolio', 'https://guison.net')}
Resume ............. JordanGuison_Resume.pdf (attached)

## Screening-answer drafts (paste/adapt)
1. Right to work in the US? -> Yes, US citizen.
2. Are you willing to work fully remote? -> Yes - currently remote-capable; based in PA, align to any US timezone.
3. Desired salary? -> ${owner.get('target_salary_min', 60000):,}+/yr (open to discussion).
4. Current/last employer? -> FlowServe / Unisys (Site Administrator IT), Taneytown MD.
5. Reason for leaving? -> Seeking stable, fully-remote full-time work aligned with my market value.
6. Degree? -> High school diploma / equivalent experience; CompTIA A+ (Feb 2025 - Feb 2028).
7. Notice period? -> Two weeks; negotiable on interview timing.

## Apply path
URL: {req_url(p)}
Board/source: {(p.get('board') or '?')}   |   Salary (as posted): {sal}   |   Location: {p['location']}

## Hot skills this role asked for (mirror in your resume summary section when applying)
{'; '.join(p.get('hot_skills', [])) or 'n/a'}
"""

def gen_kit(p, owner):
    record_kit(p, owner)
    d = kit_dir(p, owner)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cover_letter.txt"), "w") as f:
        f.write(gen_cover_letter(p, owner))
    with open(os.path.join(d, "application_sheet.md"), "w") as f:
        f.write(gen_app_sheet(p, owner))
    with open(os.path.join(d, "posting.json"), "w") as f:
        json.dump(p, f, indent=2)
    if os.path.exists(owner.get("resume_path") or RESUME_PDF):
        shutil.copy(owner.get("resume_path") or RESUME_PDF, os.path.join(d, owner.get("resume_pdf", "Resume.pdf")))
    with open(os.path.join(d, "CHECKLIST.md"), "w") as f:
        f.write(f"# {p['employer']} - {p['title']} ({p.get('lane', '?')})\n\n"
                f"1. Review posting: {req_url(p)}\n"
                f"2. Read cover_letter.txt and application_sheet.md\n"
                f"3. Approve: `python3 pipeline.py approve {p['id']}`\n"
                f"4. Submit when ready: `python3 pipeline.py open {p['id']}` (opens URL + prints answers)\n")

def print_review(p, st):
    status, notes = get(p, st)
    verif = "$" if p.get("salary_verified") else "?"
    print("=" * 80)
    print(f"[{status.upper():9}] {p['id']}  {p['employer']} - {p['title']}")
    print(f"   {p.get('lane', '?')} | {p['location']} | sal {p.get('salary') or 'n/a'} [{verif}] | {p.get('board', '?')}")
    if notes:
        print(f"   notes: {notes}")
    d = kit_dir(p)
    if os.path.exists(d):
        print(f"   kit: {d}")
        for fn in os.listdir(d):
            print(f"     - {fn}")
    else:
        print("   (kit not generated: run 'generate')")

# ---------- one-click staging (Greenhouse forms) ----------
GH_RE = re.compile(r"greenhouse\.io/([^/]+)/jobs/(\d+)")

def req_url(p):
    """The requisition you actually submit through.

    resolve_ats.py sets apply_url to the ATS req and leaves the aggregator link
    in `url` for provenance. Reading `url` here sent every runbook for an
    aggregator-sourced posting to the aggregator - which is the host that has
    been 403ing all session.
    """
    return p.get("apply_url") or p.get("url") or ""

def gh_board_jid(p):
    m = GH_RE.search(req_url(p))
    if m:
        return m.group(1), m.group(2)
    return None, None

def fetch_questions(p):
    """Pull the real application questions for a Greenhouse job, cached per kit."""
    board, jid = gh_board_jid(p)
    if not board:
        return None
    cache = os.path.join(kit_dir(p), "questions.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}?questions=true"
    try:
        d = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) jobs-helper"},
                         timeout=25).json()
    except Exception as e:
        print(f"   [questions] {e}")
        return None
    qs = d.get("questions")
    if qs is None:
        return None
    # normalize field types once; same-labelled duplicates (multi-profile boards,
    # and Greenhouse questions carrying both an input_file and a textarea field)
    # collapse to ONE row so the runbook doesn't tell you to fill a field twice
    out = []
    by_label = {}
    for q in qs:
        fields = q.get("fields") or []
        lab = q.get("label") or ""
        for f in fields:
            opts = []
            for v in f.get("values") or []:
                opts.append(v.get("label") if isinstance(v, dict) else str(v))
            entry = {"label": lab, "name": f.get("name"),
                     "type": f.get("type"), "options": opts,
                     "required": bool(q.get("required"))}
            prev = by_label.get(lab)
            # a real file-upload field beats a textarea fallback for the same label
            if prev is None or (prev["type"] != "input_file" and entry["type"] == "input_file"):
                by_label[lab] = entry
    out = list(by_label.values())
    os.makedirs(kit_dir(p), exist_ok=True)
    with open(cache, "w") as f:
        json.dump(out, f, indent=2)
    return out

# ---------- rendered form extraction (Ashby / Lever) ----------
# Ashby builds its application form in JavaScript. The raw HTML is ~29KB with
# zero `name=` attributes and no "First name", so bs4/lxml see nothing at all -
# there is no server-rendered form to parse. A real browser is the only way to
# read it. Lever renders inline but is fetched the same way for uniformity.
CHROME_CANDIDATES = [
    p for p in (
        # Jordan's own Chromium first - it is newer than the cached build and
        # is the one he actually uses. Verified driveable by Playwright.
        "/snap/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        os.path.expanduser("~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome"),
        os.path.expanduser("~/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"),
    ) if os.path.exists(p)
]
# Both Ashby and Lever hide the real form behind a button; Greenhouse and
# Workday render it inline. Capitalisation varies ("Apply for this Job" vs
# "Apply for this job"), so each ATS lists candidates and we take the first hit.
GATE_TEXT = {
    "ashby": ("Apply for this Job", "Apply for this job"),
    "lever": ("Apply for this job", "Apply for this Job", "Apply"),
}
# DOM input types -> the names fetch_questions() emits, so draft_answer()
# branches identically regardless of which extractor produced the row.
TYPE_MAP = {
    "text": "input_text", "email": "input_text", "tel": "input_text",
    "url": "input_text", "number": "input_text", "password": "input_text",
    "textarea": "textarea", "file": "input_file",
    "select": "select", "checkbox": "input_checkbox",
    "radio": "multi_value_select",
}
# Never real questions: bot traps and invisible bookkeeping fields.
# NB: do NOT skip Ashby's `_systemfield_name` / `_systemfield_email` - those are
# the real Name and Email inputs, and draft_answer() keys off the label.
SKIP_NAME = ("recaptcha", "honeypot")


def _chromium(pw):
    """A cached build if one exists, else None to use Playwright's own."""
    return CHROME_CANDIDATES[0] if CHROME_CANDIDATES else None


def ats_board_jid(url):
    """(ats, board token, req id) from a direct req URL, for the runbook header."""
    u = url or ""
    if "ashbyhq" in u:
        ats = "ashby"
    elif "lever.co" in u:
        ats = "lever"
    elif "greenhouse" in u:
        ats = "greenhouse"
    else:
        ats = "unknown"
    tok = re.search(r"(?:ashbyhq\.com|lever\.co|greenhouse\.io)/([^/?]+)", u)
    jid = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", u) \
        or re.search(r"/(\d{5,})", u)
    return ats, (tok.group(1) if tok else "-"), (jid.group(1) if jid else "-")


def _field_label(pg, el):
    eid = el.get_attribute("id")
    if eid:
        lab = pg.locator('label[for="%s"]' % eid)
        if lab.count():
            t = lab.first.inner_text().strip()
            if t:
                return re.sub(r"\s+", " ", t)
    t = (el.evaluate("e => e.closest('label') ? e.closest('label').innerText : ''") or "").strip()
    if t:
        return re.sub(r"\s+", " ", t)
    ph = el.get_attribute("placeholder") or ""
    aria = el.get_attribute("aria-label") or ""
    return re.sub(r"\s+", " ", aria or ph).strip()


def _group_label(el):
    """The question a checkbox/radio belongs to, or None if it stands alone.

    Ashby renders every choice of a group as its own <input>, so reading labels
    per input produced one bogus "question" per option ("Male", "Female",
    "Decline to self-identify" as separate entries with no options list). The
    question is the containing fieldset's legend. The `name` attribute is NOT
    usable as the key here - Ashby reuses one generated name across the Gender,
    Race and Veteran Status groups.
    """
    for sel in ('fieldset', '[role=radiogroup]', '[role=group]', '[role=radiogroup]'):
        try:
            g = el.evaluate(
                "e => { const f = e.closest('fieldset') || e.closest('[role=radiogroup]')"
                " || e.closest('[role=group]'); if (!f) return null;"
                " const l = f.querySelector('legend, [role=heading], .ashby-field-label,"
                " label, [class*=label]'); return l ? l.innerText : ''; }")
        except Exception:
            g = None
        if g and g.strip():
            return re.sub(r"\s+", " ", g.strip())
    return None


def dom_type(el):
    """Raw HTML type of a live input/select/textarea element.

    Returns the untranslated type ("select", "text", "file", ...) so callers can
    both skip non-inputs and run it through TYPE_MAP themselves. <select> and
    <textarea> carry no `type` attribute, so reading it blindly types every
    dropdown as a text box - ask the tag first.
    """
    tag = (el.evaluate("e => e.tagName") or "").lower()
    if tag == "select":
        return "select"
    if tag == "textarea":
        return "textarea"
    return (el.get_attribute("type") or "text").lower()


def dom_options(el):
    """Visible option texts of a live <select>, else []."""
    if (el.evaluate("e => e.tagName") or "").lower() != "select":
        return []
    return [o.strip() for o in
            (el.evaluate("e => Array.from(e.options).map(o => o.textContent)") or [])
            if o.strip()]


def _render_fields(pg):
    out, seen, groups = [], set(), {}
    loc = pg.locator("input, select, textarea")
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            # <select> and <textarea> carry no `type` attribute, so reading it
            # blindly types every dropdown as a text box. Ask the tag first.
            ty = dom_type(el)
            nm = el.get_attribute("name") or ""
            if any(s in nm.lower() or s in ty for s in SKIP_NAME):
                continue
            if ty in ("hidden", "submit", "button"):
                continue
            if ty in ("checkbox", "radio", "multi_value_select", "input_checkbox"):
                # One question per checkbox/radio GROUP, options collected from
                # its members - not one bogus question per choice.
                gl = _group_label(el)
                opt = _field_label(pg, el)
                if gl:
                    grp = groups.get(gl)
                    if grp is None:
                        grp = groups[gl] = {"label": gl, "name": None, "type":
                                            "multi_value_select" if ty != "radio" else "select",
                                            "options": [], "required": False}
                    if opt and opt not in grp["options"]:
                        grp["options"].append(opt)
                    if el.get_attribute("required") is not None:
                        grp["required"] = True
                    continue
                # No group: keep the old per-input behaviour but type it as a
                # checkbox so the consent guard can see it.
                label = opt
                if not label:
                    continue
                out.append({"label": label, "name": nm or None,
                            "type": "input_checkbox", "options": [],
                            "required": el.get_attribute("required") is not None})
                continue
            label = _field_label(pg, el)
            if not label:
                continue
            if label.rstrip().endswith("..."):
                continue          # bare placeholder ("Start typing...") = combobox
            key = label.lower()
            if key in seen:      # Ashby mirrors some fields for mobile
                continue
            seen.add(key)
            opts = []
            if ty == "select":
                opts = dom_options(el)
                # Lever wraps the whole field group in one <label>, so the
                # accessible text runs straight on into the option list
                # ("Gender Select ... Male Female Decline to self-identify").
                # Options are already captured separately, so cut the label
                # back at the first real choice.
                cut = len(label)
                for o in opts:
                    if o.lower().rstrip(".") in ("select", "choose", "choose an option", ""):
                        continue
                    i = label.find(o)
                    if i > 0:
                        cut = min(cut, i)
                label = label[:cut].strip()
            label = re.sub(r"\s*[✱*]\s*$", "", label).strip()
            if not label:
                continue
            out.append({"label": label, "name": nm or None,
                        "type": TYPE_MAP.get(ty, "input_text"),
                        "options": opts,
                        "required": el.get_attribute("required") is not None
                                   or el.get_attribute("aria-required") == "true"})
        except Exception:
            continue
    # Emit the checkbox/radio groups last, one question each, options intact.
    for gl, g in groups.items():
        if g["options"]:
            out.append(g)
    return out


def render_form(p, board=None, timeout=45000):
    """Extract the real application fields by rendering the req in Chromium.

    Returns the same row shape as fetch_questions(), so write_form_map() and
    draft_answer() work unchanged. Cached per kit like the Greenhouse path.
    Returns None (never raises) if Playwright is absent or the page won't load.
    """
    url = req_url(p)
    if not url:
        return None
    cache = os.path.join(kit_dir(p), "questions.json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("   [render] playwright not installed - pip install playwright")
        return None
    ats = board or ("ashby" if "ashbyhq" in url else
                    "lever" if "lever.co" in url else
                    "greenhouse" if "greenhouse" in url else None)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=_chromium(pw),
                                   args=["--no-sandbox"])
            pg = b.new_page()
            # NOT wait_until="networkidle": several ATS pages hold a long-lived
            # connection (analytics, chat widget) so the network never goes
            # idle and goto() burns the full timeout. Load the DOM, then wait
            # for the thing we actually want.
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout)
            # The gate button is React-rendered, so it does not exist yet at
            # domcontentloaded - wait for it to become visible before clicking,
            # or the query returns zero and the form never opens.
            for text in GATE_TEXT.get(ats, ()):
                try:
                    btn = pg.get_by_text(text, exact=False).first
                    btn.wait_for(state="visible", timeout=8000)
                    btn.click()
                    break
                except Exception:
                    continue
            try:
                pg.wait_for_selector("input, select, textarea", timeout=12000)
            except Exception:
                pass                      # no form on the page; fall through
            pg.wait_for_timeout(1500)      # let React finish mounting
            fields = _render_fields(pg)
            b.close()
    except Exception as e:
        print(f"   [render] {type(e).__name__}: {str(e)[:90]}")
        return None
    if not fields:
        return None
    os.makedirs(kit_dir(p), exist_ok=True)
    with open(cache, "w") as f:
        json.dump(fields, f, indent=2)
    return fields

def _pick(opts, wanted):
    oi = {str(o).lower(): o for o in opts}
    for w in wanted:
        wl = (w or "").lower()
        if not wl:
            continue
        if wl in oi:
            return oi[wl]
        for o, orig in oi.items():
            if wl in o or o in wl:
                return orig
    return None

def draft_answer(q, p, owner):
    """Map a Greenhouse question -> draft answer. Returns (answer, note)."""
    lab = q["label"].lower()
    opts = q["options"]
    text = q["type"] in ("input_text", "textarea", None)

    # Jordan's canonical answers win over every heuristic below. Checked first so
    # a deliberate answer is never overridden by a guess.
    hit = match_screening(q["label"])
    if hit is not None:
        val, key = hit
        if val == DECLINE_SENTINEL and not _eeo_self_id(q["label"]):
            # A loose key ("disability") substring-matches a skills question
            # ("...disability management software"). Not self-identification,
            # so ignore the key entirely and let the ordinary rules answer it.
            hit = None
    if hit is not None:
        val, key = hit
        if val == DECLINE_SENTINEL:
            # Voluntary self-identification. Answering "decline" is always
            # valid and asserts nothing, so it is safe to automate. Anything
            # that is a real claim about Jordan (and there are heuristics
            # further down that will happily assert "I am not a veteran")
            # must not be reached unless he supplies an explicit answer.
            for phrase in ("decline to self-identify", "decline to self identify",
                           "i don't wish to answer", "i do not wish to answer",
                           "prefer not to say", "not to self-identify"):
                if opts:
                    got = _pick(opts, [phrase])
                    if got:
                        return got, "select"
                elif q["type"] in ("input_text", "textarea", None):
                    return "Decline to self-identify", "input"
            return ("[review - self-identification: pick an option yourself]", "flag")
        if val == "__work_history__":
            # "Have you ever been employed by X (or any of its subsidiaries)?"
            m = re.search(r"(?:employed|worked|work)(?:\s+by|\s+for|\s+with|\s+at)?\s+(.+?)"
                          r"(?:\s+or any|\s+or its|\s+or affiliated|\?|$)", q["label"], re.I)
            co = m.group(1) if m else q["label"]
            prior = work_history_hit(co)
            if prior:
                return (f"[review - '{co.strip()}' IS in your work history "
                        f"({prior}) - answer Yes or No yourself]", "flag")
            return (_pick(opts, ["No"]) or "No", "select" if opts else "input")
        if opts:
            got = _pick(opts, [val]) or next(
                (o for o in opts if _norm_label(o) == _norm_label(val)), None)
            if got:
                return got, "select"
        return val, "input"

    def val(label):
        exact = _pick(opts, [label])
        return exact if exact is not None else label

    # Consent / privacy / terms checkboxes are never auto-ticked. Agreeing to
    # something on Jordan's behalf is not a formatting decision, and a
    # mis-ticked consent box is a real legal exposure - so this sits above the
    # heuristics and can only be overridden by an explicit canonical answer.
    if q["type"] in ("input_checkbox", "checkbox") and re.search(
            r"\b(consent|agree|terms|privacy|policy|attest|certif|"
            r"electronic signature|signature)\b", q["label"], re.I):
        return "[review - consent box: you must tick this yourself]", "flag"

    # Structural catch-all for voluntary self-identification, ahead of every
    # heuristic. The screening.json keys only fire when their wording matches,
    # and a phrasing that misses a key used to reach the generic heuristics -
    # which is how "Veteran status" got answered "I am not a veteran".
    if _eeo_self_id(q["label"]):
        for phrase in ("decline to self-identify", "decline to self identify",
                       "i don't wish to answer", "i do not wish to answer",
                       "prefer not to say", "not to self-identify"):
            if opts:
                got = _pick(opts, [phrase])
                if got:
                    return got, "select"
            elif q["type"] in ("input_text", "textarea", None):
                return "Decline to self-identify", "input"
        # Every phrasing missed, so the form does not offer a decline option.
        return ("[review - self-identification: pick an option yourself]", "flag")

    if "first name" in lab:
        return owner["name"].split()[0], "input"
    if "last name" in lab:
        return owner["name"].split()[-1], "input"
    # Ashby and Lever ask for ONE name field ("Name", "Full name"). The rules
    # above only cover the split first/last form Greenhouse uses, so a bare name
    # field used to fall through to the generic [review] stub. Exact-match a
    # short list so "name you'd prefer" / "preferred name" still reach their
    # own rules further down.
    if lab.strip() in ("name", "full name", "your name", "legal name",
                       "candidate name", "name as it appears on your resume"):
        return owner["name"], "input"
    if "current location" in lab or lab.strip() in ("location", "city", "current city"):
        return owner.get("location_use", "Remote - Littlestown, PA 17340 (US)"), "input"
    if any(k in lab for k in ("current company", "current employer",
                              "most recent employer", "present employer")):
        return owner.get("current_employer", "FlowServe / Unisys"), "input"
    if "email" in lab and "confirm" not in lab:
        return owner["email"], "input"
    if "phone" in lab:
        return owner["phone"], "input"
    if "resume" in lab or "cv" in lab:
        return "(upload file) " + owner.get("resume_pdf", "JordanGuison_Resume.pdf"), "upload"
    if "cover letter" in lab:
        return "(paste from cover_letter.txt - already drafted for this role)", "paste"
    if any(k in lab for k in ("why do you want to work", "why do you want to join",
                              "why are you interested", "why do you want",
                              "what interests you", "tell us why", "why this role",
                              "why do you think you")):
        # Never auto-answer these - it is the one answer that has to be written
        # for this company. Point at the drafted letter instead of a bare
        # placeholder so the runbook tells you where the text already is.
        return ("[review - ANSWER THIS] open-ended 'why us'. Use the first 2 "
                "paragraphs of cover_letter.txt, cut to ~120 words. Do not "
                "paste the whole letter."), "paste"
    if "linkedin" in lab:
        return "None - portfolio guison.net (LinkedIn not maintained)", "input"
    if "twitter" in lab or "/x handle" in lab or "x handle" in lab:
        return "None", "input"
    if "github" in lab:
        return owner.get("github", "None"), "input"
    if any(k in lab for k in ("website", "portfolio", "github", "url")):
        return owner.get("portfolio", "https://guison.net"), "input"
    if "name you'd prefer" in lab or "preferred name" in lab or "preferred first" in lab:
        return "Jordan", "input"
    if re.search(r"\b(zip|postal)\s*(code)?\b", lab) and any(
            k in lab for k in ("zip", "postal")):
        z = str(owner.get("zip_code") or "").strip()
        return (z, "input") if z else ("[review - your zip code]", "flag")
    if "country" in lab and ("residence" in lab or "located" in lab or "current" in lab or "choose" in lab):
        return val("United States"), "select"
    # The old bare-noun EEO rules (gender/sex, veteran, disability, race) are
    # gone. They matched on substring alone, so "disability management
    # software" and "gender-neutral design" were declined as self-identification,
    # and `_pick(opts, ["No", ...])` fuzzy-matched "No" onto the option "I am
    # not a veteran" - asserting a status Jordan never gave. Real self-ID
    # questions are handled once, above, by _eeo_self_id().
    if "sponsor" in lab or "visa" in lab:
        return _pick(opts, ["No"]) or "No", "select"
    if "legally authorized" in lab or "work authorization" in lab or "right to work" in lab:
        return _pick(opts, ["Yes"]) or "Yes", "select"
    if "previously worked" in lab or "have you worked" in lab or ("consulted" in lab and "gitlab" in lab):
        return _pick(opts, ["No"]) or "No", "select"
    if "employment agreement" in lab or "post-employment" in lab or "non-compete" in lab or "restrictions" in lab:
        return _pick(opts, ["No"]) or "No", "select"
    if "accessib" in lab or "adjustment" in lab or "accommodat" in lab:
        if text:
            return "None needed.", "input"
        return _pick(opts, ["No", "None needed.", "No adjustments"]) or "No", "select"
    # country-restricted postings - never guess; flag loudly
    if "select locations" in lab or "none of the above" in lab or "indicate your location" in lab:
        us = _pick(opts, ["United States", "USA", "US"])
        if us:
            return us, "select"
        return (f"! NO US OPTION - only [{', '.join(opts[:6])}] - verify posting allows US",
                "flag")
    # rating scales (Poor..Excellent etc.)
    if any(k in lab for k in ("rate your", "your confidence", "your comfort", "level of",
                              "proficiency", "how would you rate")):
        scale = {"poor", "fair", "average", "good", "excellent",
                 "beginner", "intermediate", "advanced", "expert",
                 "very confident", "confident", "not confident", "somewhat"}
        if opts and set(o.lower() for o in opts) <= scale:
            level = None
            for kw, lv in (("excellent", ["linux", "git version", "tcp", "network",
                                          "active directory", "windows", "support",
                                          "troubleshoot", "customer", "macos"]),
                           ("good", ["cloud", "database", "sql", "scripting",
                                     "python", "security", "firewall", "docker"]),
                           ("average", ["mvc", "rails", "django", "laravel",
                                        "frontend", "front end", "devops", "java",
                                        "javascript", "typescript"])):
                if any(w in lab for w in lv):
                    level = kw
                    break
            pick = _pick(opts, [level or "Good"]) or "Good"
            return pick, "select" + (" [review]" if not level else "")
    if "how did you hear" in lab or "referral source" in lab or "source" in lab:
        return _pick(opts, ["Job board", "Indeed", "Other"]) or "Job board / candidate", "select"
    if "willing to work" in lab or "available to work" in lab or "availability" in lab:
        if any(k in lab for k in ("schedule", "shift", "time", "hour", "weekend")):
            return _pick(opts, ["Yes", "Flexible", "I"]) or "Yes", "select"
    if "located in" in lab and q["name"] and "city" in lab:
        # e.g. 'Are you currently located in Bangalore?' -> US-based
        city = q["label"].split("in", 1)[-1].strip()
        return _pick(opts, ["No"]) or "No - located in United States", "select"
    if re.search(r"\bcity\b", lab) or lab.strip() in ("location", "where are you located"):
        # A city box wants a city. It must not inherit the cover-letter pointer
        # below, which is only ever right for a free-text bio field.
        city = (owner.get("city") or "Littlestown, PA").strip()
        return city, "input"
    if "certified" in lab or "certification" in lab:
        if "comp" in lab.lower() or "a+" in lab.lower():
            return "Yes - CompTIA A+ (Feb 2025 - Feb 2028)", "select"
        if text:
            return "CompTIA A+ (Feb 2025); see resume for vendor certs.", "input"
    if "years" in lab or "proficiency" in lab or "experience with" in lab or "familiar with" in lab:
        if text:
            return "15+ years total IT; strong in Windows/Linux/AD/M365/networking; Python, SQL, Docker hands-on.", "input"
        return _pick(opts, ["Yes", "5+", "Expert"]) or "Yes", "select"
    if "select all" in lab or "choose all" in lab or "which of the following" in lab:
        skill = " ".join(o.lower() for o in opts)
        chosen = [o for o in opts
                  if any(k in o.lower() for k in ("python", "sql", "linux", "windows", "active directory",
                                                  "docker", "cloud", "azure", "networking", "tcp/ip",
                                                  "macos", "bash", "powershell", "help desk", "customer"))]
        return ", ".join(chosen) if chosen else "[pick from list]", "select-multi"
    if "year" in lab and ("university" in lab or "education" in lab or "highest level" in lab):
        return _pick(opts, ["High school", "Some college", "Associate", "trade"]) or "High school diploma + 15 yrs IT", "select"
    if "how many" in lab or "frequency" in lab:
        return _pick(opts, ["0", "None", "No"]) or "[review]", "select"
    if lab.strip() and ("start date" in lab or "earliest date" in lab or
                        "available date" in lab or "begin date" in lab):
        return "[fill date] " + datetime.date.today().isoformat(), "input"
    # Compensation is never auto-answered: a wrong number is worse than a
    # blank, and the right number is role-specific (owner.json's floor is a
    # pipeline filter, not an ask). Flag it and surface the stated floor.
    if any(k in lab for k in ("compensation", "salary", "pay range", "desired pay",
                              "expected pay", "rate of pay", "remuneration")):
        # a per-posting comp answer, set deliberately for THIS role, wins;
        # there is deliberately no global default (see note above)
        if p.get("comp_answer"):
            return p["comp_answer"], "input"
        floor = owner.get("target_salary_min")
        hint = f" (your stated floor in owner.json: ${floor:,})" if isinstance(floor, (int, float)) else ""
        return (f"[review - ANSWER THIS] your target total comp for THIS role{hint}. "
                f"Do not reuse a number from another application."), "flag"
    if text and any(k in lab for k in ("portfolio", "website", "blog", "github",
                                       "linkedin", "twitter", "x handle",
                                       "tell us about yourself", "about you",
                                       "additional information", "cover letter",
                                       "why do you want", "why are you",
                                       "why this role", "why quilter", "why")):
        # Deliberately narrow. This pointer is right for a bio/links field and
        # nonsense for a city box, a demographic field, or a yes/no question, so
        # anything unrecognised falls through to "needs a human" instead.
        return "Full details on resume + portfolio (guison.net); happy to expand in interview. - Jordan", "review"
    if text or q["type"] == "select":
        return "[review - answer this yourself]", "flag"
    return "[review - pick one]", "review"

def write_form_map(p, qs, owner):
    record_kit(p, owner)
    d = kit_dir(p, owner)
    os.makedirs(d, exist_ok=True)
    lines = [f"# {p['employer']} - {p['title']}",
             f"Apply URL: {req_url(p)}",
             f"Board: {qs.get('ats', 'greenhouse').title()} ({qs['board']}) | Job id: {qs['jid']}",
             "",
             "ONE-CLICK RUNBOOK: answers are on your clipboard. For each field paste/type the value,",
             "upload resume PDF, then hit Submit. Screening answers are drafted; flag '[review]' ones.",
             "", "## Form fields (in order as they appear)", ""]
    for q in qs["questions"]:
        ans, kind = draft_answer(q, p, owner)
        lines.append(f"{q['label']}")
        lines.append(f"    -> {ans}   [{kind}]")
    with open(os.path.join(d, "form_map.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.join(d, "form_map.md")

DECLINE_SENTINEL = "__decline__"
WORK_HISTORY_SENTINEL = "__work_history__"

# Screening questions get a different voice from the cover letter, on purpose.
# Jordan's rule: a hiring manager expects copy/paste prose in the cover letter,
# but in the extra questions they are checking whether a real person answered.
# Pasting a polished paragraph into "why do you want to work here" is the single
# clearest tell that an application was mass-generated.
SCREENING_STYLE = (
    "Short, blunt, quasi-argumentative. 2-4 sentences. No cover-letter prose, "
    "no 'I am excited to apply', no stacked adjectives. Answer the actual "
    "question and then stop. Say something a person would actually say, "
    "including a concrete specific that proves you read the posting."
)


def needs_human(ans, kind):
    """True when draft_answer() declined to decide and a person must write it."""
    if kind in ("review", "flag"):
        return True
    s = str(ans or "").strip()
    return s.startswith("[") or s.startswith("!")


# ---------- canonical screening answers (screening.json) ----------
# Recurring questions get answered once, centrally, instead of per kit. Keys are
# matched as substrings of the normalised question label, longest key first, so
# a specific rule beats a generic one. Jordan can add a key here without any
# code change - which is the point, since new questions keep appearing.
_SCREEN = None


def _norm_label(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def screening_answers():
    global _SCREEN
    if _SCREEN is None:
        path = os.path.join(BASE, "screening.json")
        if os.path.exists(path):
            with open(path) as f:
                doc = json.load(f)
        else:
            doc = {}
        items = list((doc.get("answers") or {}).items())
        # longest key first: "veteran status" must beat a bare "status"
        items.sort(key=lambda kv: -len(_norm_label(kv[0])))
        _SCREEN = {"answers": items,
                   "work_history": [w.lower() for w in (doc.get("work_history") or [])]}
    return _SCREEN


def work_history_hit(company):
    """True if `company` matches an employer on Jordan's resume.

    Word-boundary-ish matching, not substring: "Polycom" must not match a
    company called "PolyComposite", and "Comcast" must not match "Comcastal".
    Guards against a false Yes on a rehire/never-employed question.
    """
    c = _norm_label(company)
    if not c:
        return None
    for w in screening_answers()["work_history"]:
        if _norm_label(w) and re.search(r"\b%s\b" % re.escape(_norm_label(w)), c):
            return w
    return None


def _eeo_self_id(label):
    """True when a question is about Jordan, not about the job.

    EEO questions are optional by law and Jordan's standing instruction is to
    decline them, so over-matching is the safe direction - declining costs
    nothing. The one exception is job-relevant phrasing that merely contains an
    EEO noun: "have you worked with disability management software" and
    "describe your approach to gender-neutral design" are skills questions, and
    declining those could cost him a role.
    """
    if re.search(r"\b(years?|experience|approach|worked with|working with|"
                 r"work with|familiar|skills?|used|using|knowledge of|"
                 r"background in|how would you (approach|handle)|coworker|"
                 r"colleague|team|client|customer)\b", label or "", re.I):
        return False
    # Accommodation requests are self-identification under another name: the
    # ADA question is a question about Jordan's medical status, and answering
    # it from the ordinary rules would assert a diagnosis on his behalf. The
    # job-relevance filter above already returned for phrasings like "made
    # reasonable accommodations for your team", so what reaches here is a
    # question about him.
    if re.search(r"accommodation|reasonable adjustment|\bADA\b|"
                 r"\bmedical\b|\bepilepsy\b|\bchronic\b", label or "", re.I):
        return True
    # NOTE: no trailing \b on the stems below, deliberately. `\bdisabilit\b`
    # cannot match "disability" - it needs a word boundary right after the
    # stem, which only the bare stem has. That bug meant EVERY inflected form
    # of the single most consequential category slipped through to the
    # ordinary answer rules, and the pipeline answered "Do you require a
    # reasonable accommodation?" with "None needed." Prefixes get \w* instead.
    return bool(re.search(
        r"\b(gender|gender identity|sex\b|race|racial|ethnicity|ethnic|"
        r"hispanic|latino|nationality|veteran\w*|disabilit\w*|chronic\b|"
        r"sexual orientation|trans\w*gender|trans[- ]?sexual|pronoun\w*|"
        # Gender is routinely asked in the personal noun rather than the
        # category word ("Do you identify as a woman?"), and that phrasing
        # contains none of the words above.
        r"woman|women|man|men|female|male|non-?binary|genderqueer)\b",
        label or "", re.I))


def match_screening(label):
    """The canonical answer for this question, or None."""
    lab = _norm_label(label)
    if not lab:
        return None
    for key, val in screening_answers()["answers"]:
        # An empty value means "not decided yet" - fall through to the
        # heuristics rather than typing an empty string into the box.
        if not str(val).strip():
            continue
        if _norm_label(key) and _norm_label(key) in lab:
            return val, key
    return None


def write_screening_answers(p, qs, owner):
    """answers.json - the human-written overrides autofill.py will type.

    An empty string means UNANSWERED, and autofill leaves that box empty rather
    than inventing an answer. Never clobbers a file that already has answers.
    """
    d = kit_dir(p, owner)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "answers.json")
    if os.path.exists(path):
        return path
    stubs = {}
    for q in qs["questions"]:
        ans, kind = draft_answer(q, p, owner)
        if needs_human(ans, kind):
            stubs[q["label"]] = ""
    with open(path, "w") as f:
        json.dump({"_style": SCREENING_STYLE,
                   "_rule": ("Fill these in yourself, in the style above. "
                             "Leave blank and autofill.py will skip the box."),
                   "answers": stubs}, f, indent=2, ensure_ascii=False)
    return path

def copy_clipboard(path):
    try:
        subprocess.run(["xclip", "-selection", "clipboard", path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def cmd_stage(ids, data, owner):
    """One command to ship an application: kit + real form questions + drafted answers
    + clipboard + browser open.

    Staging PREPARES an application, it does not submit one. Status is left at
    'staged' so state.json never claims an application landed that Jordan never
    sent; `pipeline.py submitted <id>` is the explicit, human-acted step."""
    st = load_state()
    for p in data["postings"]:
        if ids and p["id"] not in ids:
            continue
        gen_kit(p, owner)
        board, jid = gh_board_jid(p)
        qs = None
        if board:
            fetched = fetch_questions(p)
            if fetched is not None:
                qs = {"ats": "greenhouse", "board": board, "jid": jid,
                      "questions": fetched}
        if qs is None:
            # Greenhouse had no public form for this req (Ashby and Lever never
            # do). Render it in a real browser instead of giving up.
            fetched = render_form(p)
            if fetched is not None:
                ats, tok, jid = ats_board_jid(req_url(p))
                qs = {"ats": ats, "board": tok, "jid": jid,
                      "questions": fetched}
        if qs:
            sheet = write_form_map(p, qs, owner)
            scr = write_screening_answers(p, qs, owner)
            ok = copy_clipboard(sheet)
            print(f"\n*** STAGED {p['id']}  {p['employer']} - {p['title']}")
            print(f"    form map: {sheet}  (answers {'copied to clipboard' if ok else 'in file'})")
            print(f"    your turn: {scr}  (blank = autofill skips that box)")
        else:
            print(f"\n*** STAGED {p['id']}  {p['employer']} - {p['title']}  (no form found - manual apply)")
            sheet = os.path.join(kit_dir(p), "application_sheet.md")
            if os.path.exists(sheet):
                copy_clipboard(sheet)
        # review card preview
        if os.path.exists(sheet):
            print("    ----- form_map / sheet -----")
            with open(sheet) as f:
                print("\n".join(x for x in f.read().splitlines() if x.strip())[:1400])
        url = p.get("apply_url") or p["url"]
        print(f"    opening: {url}")
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"    (could not open browser: {e})")
        set_status(p, st, "staged", note="staged (one-click) - prepared, NOT yet submitted")
    save_state(st)
    print("\nStaged. Submit it yourself, THEN record it: pipeline.py submitted <id>")
    print("After the interview loop: pipeline.py note <id> interview|offer|closed")

# ---------- commands ----------
def cmd_heat(data):
    posts = sorted(data["postings"],
                   key=lambda p: (not bool(p.get("salary_verified")), -(p.get("salary_max") or 0)))
    print("REMOTE HEAT - verified salary first, unverified flagged '?'")
    print(f"{'ID':<9} {'Salary':<32} {'Lane':<18} {'Employer':<22} Title")
    print("-" * 130)
    for p in posts:
        mark = "$" if p.get("salary_verified") else "?"
        sal = f"{p.get('salary') or 'not posted'} {mark}"[:32]
        print(f"{p['id']:<9} {sal:<32} {p.get('lane','?')[:18]:<18} {p['employer'][:22]:<22} {p['title'][:45]}")
    print("\nTip: `pipeline.py review <id>` then `approve <id>` then `open <id>` for any you like.")

def cmd_status(data):
    st = load_state()
    print(f"{'ID':<9} {'Status':<10} {'Employer':<24} {'Lane':<16} {'Salary':<30} Notes")
    print("-" * 130)
    for p in data["postings"]:
        status, notes = get(p, st)
        print(f"{p['id']:<9} {status:<10} {p['employer'][:24]:<24} {p.get('lane','?')[:16]:<16} "
              f"{(p.get('salary') or '')[:30]:<30} {notes}")

def cmd_generate(ids, data, owner):
    st = load_state()
    for p in data["postings"]:
        if ids and p["id"] not in ids:
            continue
        gen_kit(p, owner)
        print(f"generated kit: {p['id']}  ({p['employer']} - {p['title']})")

def cmd_review(ids, data):
    st = load_state()
    for p in data["postings"]:
        if ids and p["id"] not in ids:
            continue
        print_review(p, st)

def cmd_approve(ids, data):
    st = load_state()
    for p in data["postings"]:
        if ids and p["id"] not in ids:
            continue
        status, _ = get(p, st)
        if status in ("submitted", "interview", "offer", "closed"):
            print(f"skip {p['id']} (already {status})")
            continue
        set_status(p, st, "approved")
        print(f"approved: {p['id']}")
    save_state(st)

def cmd_open(ids, data, owner):
    st = load_state()
    for p in data["postings"]:
        if ids and p["id"] not in ids:
            continue
        status, _ = get(p, st)
        if status not in ("approved", "submitted"):
            print(f"BLOCKED {p['id']} (status={status}) - must 'approve' first")
            continue
        url = p.get("apply_url") or p["url"]
        print(f"OPEN {p['id']}  {p['employer']} - {p['title']}")
        print(f"     {url}")
        try:
            subprocess.Popen(["xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"     (could not auto-open: {e})")
        d = kit_dir(p)
        sheet = os.path.join(d, "application_sheet.md")
        if os.path.exists(sheet):
            print("\n----- application_sheet.md -----")
            with open(sheet) as f:
                print(f.read().strip())
        elif not os.path.exists(d):
            print("\n(kit missing - run 'generate' to get cover letter + answers)")
        set_status(p, st, "submitted", note="opened for submission")
    save_state(st)
    print("\nMarked 'submitted'. After interviews use: pipeline.py note <id> interview|offer|closed")

def cmd_submitted(ids, data):
    st = load_state()
    for p in data["postings"]:
        if ids and p["id"] not in ids:
            continue
        set_status(p, st, "submitted")
        print(f"marked submitted: {p['id']}")
    save_state(st)

def cmd_note(id_, text, data, owner=None):
    st = load_state()
    owner = owner if owner is not None else _owner_cached()
    resolved = resolve_ids([id_], data, owner)
    if not resolved or len(resolved) > 1:
        print(f"could not resolve '{id_}' to exactly one posting")
        return
    p = next(x for x in data["postings"] if x["id"] in resolved)
    id_ = p["id"]
    # `note <target> <status> [free text]` sets a status AND records why;
    # `note <target> <free text>` on its own just records a response note.
    # The status is only honoured as a leading whole word, so a note that
    # merely starts with a status-like word is not silently eaten.
    status, rest = None, text.strip()
    head = rest.split(None, 1)[0] if rest else ""
    if head in VALID:
        status = head
        rest = rest.split(None, 1)[1].strip() if len(rest.split(None, 1)) > 1 else ""
    if status is None:
        status = "responded"
    _, prev_notes = get(p, st)
    if status == "responded" and not rest:
        note = text
    elif rest:
        note = ((prev_notes + " | ") if prev_notes else "") + rest
    else:
        note = prev_notes or None
    set_status(p, st, status, note=note)
    save_state(st)
    print(f"{id_}  {kit_name(p, owner)}\n  status={status}"
          + (f"\n  note: {rest}" if rest else ""))

def cmd_shortlist(data):
    """The focused hunt: remote IT / dev-engineering roles,
    vetoing metered shapes (MSP, NOC, call center, staffing)."""
    posts = data["postings"]
    buckets = {"target": [], "watch": [], "stretch": [], "veto": [], "other": []}
    for p in posts:
        buckets[fit_bucket(p)].append(p)
    for b in ("target", "watch", "stretch"):
        buckets[b].sort(key=lambda p: (not bool(p.get("salary_verified")), -(p.get("salary_max") or 0)))
    vetoed = {p["id"] for p in buckets["veto"]}
    print("SHORTLIST - remote IT roles (target, then watch, then stretch)")
    print(f"{'Fit':<8} {'ID':<9} {'Salary':<30} {'Employer':<24} Title")
    print("-" * 130)
    for b in ("target", "watch", "stretch"):
        for p in buckets[b]:
            mark = "$" if p.get("salary_verified") else "?"
            sal = f"{p.get('salary') or 'not posted'} {mark}"[:30]
            print(f"{b:<8} {p['id']:<9} {sal:<30} {p['employer'][:24]:<24} {p['title'][:45]}")
    print(f"\nvetoed (MSP/NOC/call-center/staffing shapes): {len(vetoed)}")
    print(f"stretch (product-eng/ML - real but not your lane): {len(buckets['stretch'])}")
    print(f"other (out of scope): {len(buckets['other'])}")
    print("\nstability hints are in the exported tracker Shortlist sheet - verify manually.")
    print("Tip: `python3 pipeline.py rate` for hire-ease A-C list, then `stage <id>`.")

def cmd_rate(data):
    """Hire-ease rated view: A = apply now, B = apply soon, C = stretch, D = skip.
    Based on Jordan's real track record (resume + portfolio + github)."""
    posts = [p for p in data["postings"] if fit_bucket(p) in ("target", "watch")]
    rows = []
    for p in posts:
        grade, reasons = rate_posting(p)
        rows.append((grade, p, "; ".join(reasons)))
    rows.sort(key=lambda r: (r[0], not bool(r[1].get("salary_verified")), -(r[1].get("salary_max") or 0)))
    print("RATED - hire-ease for YOUR background (A apply now / B apply soon / C stretch / D skip)")
    print(f"{'Grade':<6} {'ID':<9} {'Salary':<30} {'Employer':<22} Title")
    print("-" * 130)
    for g, p, why in rows:
        mark = "$" if p.get("salary_verified") else "?"
        sal = f"{p.get('salary') or 'not posted'} {mark}"[:30]
        print(f"{g:<6} {p['id']:<9} {sal:<30} {p['employer'][:22]:<22} {p['title'][:44]}")
        if args_verbose():
            print(f"      -> {why}")
    from collections import Counter
    print("\nby grade:", dict(Counter(g for g, _, _ in rows)))
    print("Tip: `pipeline.py review <id>` then `stage <id>` for any A/B.")

def args_verbose():
    return "-v" in sys.argv or "--verbose" in sys.argv

def cmd_export(data, owner):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    st = load_state()
    out = owner.get("tracker_out") or TRACKER_OUT
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tracker"
    headers = ["ID", "Status", "Employer", "Title", "Lane", "Salary", "SalVer",
               "Location", "Board", "First Seen", "URL", "Notes"]
    hf = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="4472C4")
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = hf
        c.fill = fill
    color_map = {
        "new": "DDEBF7", "approved": "FFF2CC", "submitted": "E2EFDA",
        "interview": "C6EFCE", "offer": "A9D08E", "responded": "C6EFCE",
        "closed": "F2F2F2", "skip": "F2F2F2",
    }
    for r, p in enumerate(data["postings"], start=2):
        status, notes = get(p, st)
        row = [p["id"], status, p["employer"], p["title"], p.get("lane", ""),
               p.get("salary") or "", "Y" if p.get("salary_verified") else "N",
               p["location"], p.get("board", ""), p.get("first_seen", "")[:10],
               p["url"], notes]
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=v)
            if c == 2:
                cell.fill = PatternFill("solid", fgColor=color_map.get(status, "FFFFFF"))
            cell.alignment = Alignment(vertical="top", wrap_text=(c in (4, 11, 12)))
    widths = [8, 11, 26, 34, 16, 34, 7, 22, 14, 10, 48, 20]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Lane Heat")
    from collections import Counter
    cs = Counter(p.get("lane", "?") for p in data["postings"])
    ws2.cell(row=1, column=1, value="Lane").font = hf
    ws2.cell(row=1, column=2, value="Count").font = hf
    for i, (lane, n) in enumerate(cs.most_common(), start=2):
        ws2.cell(row=i, column=1, value=lane)
        ws2.cell(row=i, column=2, value=n)
    ws2.column_dimensions["A"].width = 22

    ws3 = wb.create_sheet("Shortlist")
    ws3.cell(row=1, column=1, value="FOCUSED HUNT - remote IT / dev-engineering roles").font = Font(bold=True)
    ws3.cell(row=2, column=1,
             value=("target = strong title match (sysadmin/IT Ops/endpoint/MDM/SaaS/BizTech); "
                    "watch = plausible, eyeball it. Hard vetoes (MSP/NOC/call-center/staffing) are excluded. "
                    "Stability hint is a sector heuristic - verify the employer yourself.")).alignment = Alignment(wrap_text=True)
    ws3.merge_cells("A2:J2")
    ws3.row_dimensions[2].height = 30
    h3 = ["Fit", "ID", "Employer", "Title", "Salary", "Location", "Board", "First Seen", "Stability hint", "URL"]
    for i, h in enumerate(h3, 1):
        c = ws3.cell(row=4, column=i, value=h)
        c.font = hf
        c.fill = fill
    fit_fill = {"target": "C6EFCE", "watch": "FFF2CC", "stretch": "E4E0EC"}
    fit_rows = []
    for p in data["postings"]:
        b = fit_bucket(p)
        if b in ("target", "watch", "stretch"):
            fit_rows.append((b, p))
    fit_rows.sort(key=lambda x: (("target", "watch", "stretch").index(x[0]),
                                 not bool(x[1].get("salary_verified")),
                                 -(x[1].get("salary_max") or 0)))
    for r, (b, p) in enumerate(fit_rows, start=5):
        row = [b, p["id"], p["employer"], p["title"], p.get("salary") or "not posted",
               p["location"], p.get("board", ""), p.get("first_seen", "")[:10],
               stability_hint(p), p["url"]]
        for c, v in enumerate(row, 1):
            cell = ws3.cell(row=r, column=c, value=v)
            cell.alignment = Alignment(vertical="top", wrap_text=(c in (4, 5, 6, 10)))
            if c == 1:
                cell.fill = PatternFill("solid", fgColor=fit_fill[b])
    w3 = [6, 9, 26, 40, 32, 26, 14, 10, 18, 60]
    for i, w in enumerate(w3, 1):
        ws3.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws3.freeze_panes = "A5"

    ws4 = wb.create_sheet("How To Use")
    lines = [
        "REMOTE JOB PIPELINE - HOW TO USE",
        "",
        "0. Sync boards:   python3 scrape.py sync   (repeat a few times/day via cron)",
        "1. Focused hunt:  python3 pipeline.py shortlist   <- the one to work first",
        "2. Scan broad:     python3 pipeline.py heat",
        "3. Your kit list:   python3 pipeline.py kits      (or just: ls kits/)",
        "4. ONE-CLICK:      python3 pipeline.py stage \"Banyan\"  (builds kit, drafts the REAL",
        "                     form answers, copies to clipboard, opens the apply page)",
        "5. Review:         python3 pipeline.py review \"Banyan\"  /  open the kit's form_map.md",
        "6. (optional gate) python3 pipeline.py approve \"Banyan\" then open \"Banyan\"",
        "7. AFTER you send it:  python3 pipeline.py submitted \"Banyan\"",
        "8. Outcomes:       python3 pipeline.py note \"Banyan\" interview|offer|closed",
        "9. Tracker:        python3 pipeline.py export",
        "",
        "Shortlist sheet = remote IT / dev-engineering hunt:",
        "  target = strong title match (sysadmin, IT Ops, endpoint, MDM, Intune, SaaS,",
        "           Google Workspace, BizTech, IT business partner, cloud admin).",
        "  watch  = plausible but eyeball it (generic IT support / helpdesk / desktop).",
        "  vetoed = MSP, NOC, call center, tiered helpdesk, staffing - excluded.",
        "  Stability hint is a sector heuristic (healthcare/edu/gov/utility/etc - verify",
        "  the employer yourself).",
        "",
        "Only postings with a posted salary >= $60k are kept unless the lane plausibly",
        "clears $60k and salary is unposted (flag '?' in heat / SalVer N in tracker).",
    ]
    for i, ln in enumerate(lines, 1):
        ws4.cell(row=i, column=1, value=ln)
    ws4.column_dimensions["A"].width = 95
    wb.save(out)
    print("exported", out)

def resolve_ids(tokens, data, owner):
    """Accept posting ids, kit directory names, or any unique substring of
    either. Lets you type `stage "Banyan"` instead of hunting for rd03a97a."""
    if not tokens:
        return None
    posts = data["postings"]
    by_id = {p["id"]: p for p in posts}
    out, unknown = set(), []
    for t in tokens:
        if t in by_id:
            out.add(t)
            continue
        low = t.lower()
        hits = [p for p in posts
                if low in (p.get("kit_name") or "").lower()
                or low in (p.get("title") or "").lower()
                or low in (p.get("employer") or "").lower()
                or low in kit_name(p, owner).lower()]
        exact = [p for p in hits if kit_name(p, owner).lower() == low]
        if exact:
            out.update(p["id"] for p in exact)
        elif len(hits) == 1:
            out.add(hits[0]["id"])
        elif len(hits) > 1:
            print(f"  '{t}' is ambiguous ({len(hits)} matches):")
            for p in sorted(hits, key=lambda x: -(x.get('salary_max') or 0))[:8]:
                print(f"      {p['id']}  {kit_name(p, owner)}")
        else:
            unknown.append(t)
    for t in unknown:
        print(f"  no posting or kit matches '{t}'")
    # An empty set here means "you named something and it matched nothing".
    # Callers MUST distinguish that from "no filter given" (None = all) so a
    # typo or an ambiguous name can never fall through to a bulk action.
    return out


def cmd_kits(data, owner):
    """List kits by their human-readable name - the same view as `ls kits/`,
    plus the status and the provenance of each salary figure."""
    posts = data["postings"]
    km = load_kitmap()
    rows = []
    for name in sorted(os.listdir(KITS)) if os.path.isdir(KITS) else []:
        # directories only: kits/INDEX.md and kits/.kitmap.json live here too
        if name.startswith(".") or not os.path.isdir(os.path.join(KITS, name)):
            continue
        pid = next((i for i, v in km.items() if v.get("kit_name") == name), None)
        p = next((x for x in posts if x["id"] == pid), None)
        if p is None:
            rows.append((name, "?", "?", "orphan (no matching posting)", ""))
            continue
        status, _ = get(p, load_state())
        ask = p.get("recommended_ask")
        basis = p.get("recommended_ask_basis") or ""
        rows.append((name, p["id"], status, basis, _fmt_money(ask) if ask else "-"))
    if not rows:
        print("no kits yet - run: python3 pipeline.py generate <id>")
        return
    w = max(len(r[0]) for r in rows)
    print(f"{'KIT DIRECTORY'.ljust(w)}  {'STATUS':<10} {'ASK':<9} BASIS")
    for name, pid, status, basis, ask in rows:
        print(f"{name.ljust(w)}  {status:<10} {ask:<9} {basis}")
    print(f"\n{len(rows)} kit(s).  Open one:  cd kits/{rows[0][0]!r}")
    print("Act on one by name, no id needed:  python3 pipeline.py stage \"<part of the name>\"")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    data = load_postings()
    owner = load_owner(data)
    cmd = args[0]
    all_ = "--all" in args
    # `note` reads args[1] as the target and the rest as free text, so it only
    # resolves the first token; every other id-taking command resolves them all
    raw = [a for a in args[1:] if a != "--all"]
    if cmd == "note":
        raw = raw[:1]
    ids = resolve_ids(raw, data, owner)
    if not all_ and raw and not ids:
        # named something, matched nothing (typo or ambiguous) - do nothing at
        # all rather than treating it as "no filter" and hitting every posting
        print("\nnothing matched - no action taken. "
              "Try: python3 pipeline.py kits   (to see valid names)")
        return
    if cmd == "heat":
        cmd_heat(data)
    elif cmd == "status":
        cmd_status(data)
    elif cmd == "stage":
        cmd_stage(None if all_ else ids, data, owner)
    elif cmd == "generate":
        cmd_generate(None if all_ else ids, data, owner)
    elif cmd == "review":
        cmd_review(None if all_ else ids, data)
    elif cmd == "approve":
        cmd_approve(None if all_ else ids, data)
    elif cmd == "open":
        cmd_open(None if all_ else ids, data, owner)
    elif cmd == "submitted":
        cmd_submitted(None if all_ else ids, data)
    elif cmd == "note" and len(args) >= 3:
        cmd_note(args[1], " ".join(args[2:]), data, owner)
    elif cmd == "shortlist":
        cmd_shortlist(data)
    elif cmd == "kits":
        cmd_kits(data, owner)
    elif cmd == "rate":
        cmd_rate(data)
    elif cmd == "export":
        cmd_export(data, owner)
    else:
        print(__doc__)

if __name__ == "__main__":
    main()