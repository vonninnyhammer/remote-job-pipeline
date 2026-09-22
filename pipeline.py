#!/usr/bin/env python3
"""
Remote jobs application pipeline
================================
Aggregates remote job boards, filters to (remote + US + $60k+), and builds a
tailored, almost-one-click application kit per role - including real form
questions pre-drafted from a Greenhouse listing.

Workflow:  sync -> heat -> stage|generate -> review -> approve -> open -> submitted

  python3 scrape.py  sync                        # pull boards into postings.json
  python3 pipeline.py heat                        # ranked view of jobs (salary first)
  python3 pipeline.py status                      # current application statuses
  python3 pipeline.py stage [id|--all]            # ONE-CLICK: build kit, fetch the real form,
                                                  #   draft every answer, load clipboard, open browser
  python3 pipeline.py generate [id|--all]         # tailored kit per job (cover letter + sheet + resume)
  python3 pipeline.py review [id|--all]           # show what was generated
  python3 pipeline.py approve [id|--all]          # mark approved (gate: nothing opens until here)
  python3 pipeline.py open [id|--all]             # opens apply URL + prints paste-ready answers
  python3 pipeline.py submitted [id|--all]        # mark submitted
  python3 pipeline.py note <id> interview|offer|closed|skip <text>
  python3 pipeline.py export                      # write job tracker xlsx

Config:  owner.json (identity + paths) and profile.json (skill bullets and
cover-letter lanes) - both optional; see owner.example.json.

State lives in state.json; kits are written under kits/<id>/.
"""
import json, os, re, sys, subprocess, datetime, shutil
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
POSTINGS = os.path.join(BASE, "postings.json")
STATE = os.path.join(BASE, "state.json")
KITS = os.path.join(BASE, "kits")
PROFILE_FILE = os.path.join(BASE, "profile.json")
OWNER_FILE = os.path.join(BASE, "owner.json")
RESUME_PDF = os.environ.get("RESUME_PDF") or os.path.join(BASE, "JordanGuison_Resume.pdf")
TRACKER_OUT = os.environ.get("TRACKER_OUT") or os.path.join(BASE, "Job_Tracker.xlsx")

VALID = ["submitted", "interview", "offer", "closed", "skip", "responded"]

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
URL: {p['url']}
Board/source: {(p.get('board') or '?')}   |   Salary (as posted): {sal}   |   Location: {p['location']}

## Hot skills this role asked for (mirror in your resume summary section when applying)
{'; '.join(p.get('hot_skills', [])) or 'n/a'}
"""

def gen_kit(p, owner):
    d = os.path.join(KITS, p["id"])
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
                f"1. Review posting: {p['url']}\n"
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
    d = os.path.join(KITS, p["id"])
    if os.path.exists(d):
        print(f"   kit: {d}")
        for fn in os.listdir(d):
            print(f"     - {fn}")
    else:
        print("   (kit not generated: run 'generate')")

# ---------- one-click staging (Greenhouse forms) ----------
GH_RE = re.compile(r"greenhouse\.io/([^/]+)/jobs/(\d+)")

def gh_board_jid(p):
    m = GH_RE.search(p.get("url", ""))
    if m:
        return m.group(1), m.group(2)
    return None, None

def fetch_questions(p):
    """Pull the real application questions for a Greenhouse job, cached per kit."""
    board, jid = gh_board_jid(p)
    if not board:
        return None
    cache = os.path.join(KITS, p["id"], "questions.json")
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
    # normalize field types once; same-labelled duplicates (multi-profile boards) collapse
    out = []
    seen_lab = set()
    for q in qs:
        fields = q.get("fields") or []
        lab = q.get("label") or ""
        if lab in seen_lab:
            continue
        for f in fields:
            opts = []
            for v in f.get("values") or []:
                opts.append(v.get("label") if isinstance(v, dict) else str(v))
            out.append({"label": lab, "name": f.get("name"),
                        "type": f.get("type"), "options": opts,
                        "required": bool(q.get("required"))})
            seen_lab.add(lab)
    os.makedirs(os.path.join(KITS, p["id"]), exist_ok=True)
    with open(cache, "w") as f:
        json.dump(out, f, indent=2)
    return out

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

    def val(label):
        exact = _pick(opts, [label])
        return exact if exact is not None else label

    if "first name" in lab:
        return owner["name"].split()[0], "input"
    if "last name" in lab:
        return owner["name"].split()[-1], "input"
    if "email" in lab and "confirm" not in lab:
        return owner["email"], "input"
    if "phone" in lab:
        return owner["phone"], "input"
    if "resume" in lab or "cv" in lab:
        return "(upload file) " + owner.get("resume_pdf", "JordanGuison_Resume.pdf"), "upload"
    if "cover letter" in lab:
        return "(paste from cover_letter.txt - already drafted for this role)", "paste"
    if "linkedin" in lab:
        return "None - portfolio guison.net (LinkedIn not maintained)", "input"
    if any(k in lab for k in ("website", "portfolio", "github", "url")):
        return owner.get("portfolio", "https://guison.net"), "input"
    if "name you'd prefer" in lab or "preferred name" in lab or "preferred first" in lab:
        return "Jordan", "input"
    if "pronoun" in lab:
        return _pick(opts, ["He/him", "he", "He"]) or "Prefer not to say", "select"
    if "country" in lab and ("residence" in lab or "located" in lab or "current" in lab or "choose" in lab):
        return val("United States"), "select"
    if "gender" in lab or "sex" in lab:
        return _pick(opts, ["Decline", "prefer not", "Do not"]) or "[leave blank / decline]", "select"
    if "veteran" in lab:
        return _pick(opts, ["No", "Decline", "not"]) or "[decline]", "select"
    if "disability" in lab:
        return _pick(opts, ["No", "Decline", "not"]) or "[decline]", "select"
    if "race" in lab or "ethnic" in lab:
        return _pick(opts, ["Decline", "prefer not"]) or "[decline]", "select"
    if "sponsor" in lab or "visa" in lab:
        return _pick(opts, ["No"]) or "No", "select"
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
    # generic fallback: textfields get a draft, selects get a neutral pick
    if text:
        return "Full details on resume + portfolio (guison.net); happy to expand in interview. - Jordan", "review"
    return "[review - pick one]", "review"

def write_form_map(p, qs, owner):
    d = os.path.join(KITS, p["id"])
    os.makedirs(d, exist_ok=True)
    lines = [f"# {p['employer']} - {p['title']}",
             f"Apply URL: {p['url']}",
             f"Board: Greenhouse ({qs['board']}) | Job id: {qs['jid']}",
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

def copy_clipboard(path):
    try:
        subprocess.run(["xclip", "-selection", "clipboard", path],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def cmd_stage(ids, data, owner):
    """One command to ship an application: kit + real form questions + drafted answers
    + clipboard + browser open. Status -> submitted."""
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
                qs = {"board": board, "jid": jid, "questions": fetched}
        if qs:
            sheet = write_form_map(p, qs, owner)
            ok = copy_clipboard(sheet)
            print(f"\n*** STAGED {p['id']}  {p['employer']} - {p['title']}")
            print(f"    form map: {sheet}  (answers {'copied to clipboard' if ok else 'in file'})")
        else:
            print(f"\n*** STAGED {p['id']}  {p['employer']} - {p['title']}  (no Greenhouse form - manual apply)")
            sheet = os.path.join(KITS, p["id"], "application_sheet.md")
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
        set_status(p, st, "submitted", note="staged (one-click)")
    save_state(st)
    print("\nStaged. After the interview loop: pipeline.py note <id> interview|offer|closed")

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
        d = os.path.join(KITS, p["id"])
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

def cmd_note(id_, text, data):
    st = load_state()
    ids = [p["id"] for p in data["postings"]]
    if id_ not in ids:
        print(f"unknown id {id_}")
        return
    status = text if text in VALID else "responded"
    p = next(x for x in data["postings"] if x["id"] == id_)
    _, prev_notes = get(p, st)
    note = text if text not in VALID else (prev_notes + "").strip()
    set_status(p, st, status, note=note if note else None)
    save_state(st)
    print(f"{id_}: status={status}")

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

    ws3 = wb.create_sheet("How To Use")
    lines = [
        "REMOTE JOB PIPELINE - HOW TO USE",
        "",
        "0. Sync boards:   python3 scrape.py sync   (repeat a few times/day via cron)",
        "1. Scan:          python3 pipeline.py heat",
        "2. ONE-CLICK:      python3 pipeline.py stage <id>  (builds kit, drafts the REAL form",
        "                     answers, copies to clipboard, opens the apply page)",
        "3. Review:         python3 pipeline.py review <id>  /  view kits/<id>/form_map.md",
        "4. (optional gate) python3 pipeline.py approve <id> then open <id>",
        "5. Outcomes:       python3 pipeline.py note <id> interview|offer|closed",
        "6. Tracker:        python3 pipeline.py export",
        "",
        "Only postings with a posted salary >= $60k are kept unless the lane plausibly",
        "clears $60k and salary is unposted (flag '?' in heat / SalVer N in tracker).",
    ]
    for i, ln in enumerate(lines, 1):
        ws3.cell(row=i, column=1, value=ln)
    ws3.column_dimensions["A"].width = 95
    wb.save(out)
    print("exported", out)

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    data = load_postings()
    owner = load_owner(data)
    cmd = args[0]
    ids = set(a for a in args[1:] if a != "--all") or None
    all_ = "--all" in args
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
        cmd_note(args[1], " ".join(args[2:]), data)
    elif cmd == "export":
        cmd_export(data, owner)
    else:
        print(__doc__)

if __name__ == "__main__":
    main()