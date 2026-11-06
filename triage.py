#!/usr/bin/env python3
"""
Triage — what a reply means, and what Jordan owes it.
=====================================================
The pipeline already finds replies (notify.py) and tracks per-job status
(state.json). What it could not do was tell him which of those replies are
worth waking up for. This module is that layer.

  triage.py digest            new replies needing him, then ack them (one-shot)
  triage.py digest --all      replay everything, including already-acked
  triage.py pending           same list, no writes (safe to poll)
  triage.py followup          applications that went silent past N days
  triage.py followup --draft ID   print a ready-to-send follow-up for one job
  triage.py ack               ack everything currently pending

Ledger: responses.json. One record per inbound reply, keyed by Message-ID
(falling back to IMAP UID), holding the classification, any extracted
deadline, and `alerted_at`. Once alerted, a record never surfaces again
unless --all is passed. That is the whole point: he is told once.

Categories
  auto       system/ticket/OOO noise            -> never alerts
  human      a person wrote, nothing wanted     -> never alerts
  action     a person wrote and wants something -> alerts, with the ask
  rejection  the door closed                    -> alerts, no ask

Follow-ups are acked separately (followup:<pid> in the ledger) and re-surface
only when the application crosses its next 7-day milestone, so a silent job
nudges weekly rather than every morning.

Reads state.json / postings.json / seen.json / kits/.kitmap.json.
Writes responses.json only.
"""
import argparse
import datetime
import email.utils
import json
import os
import re
import sys
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
RESPONSES = os.path.join(BASE, "responses.json")
STATE = os.path.join(BASE, "state.json")
POSTINGS = os.path.join(BASE, "postings.json")
SEEN = os.path.join(BASE, "seen.json")
KITMAP = os.path.join(BASE, "kits", ".kitmap.json")

FOLLOWUP_AFTER_DAYS = 7
# Phone pushes. A lead digest is shown, not buzzed.
ALERT_CATEGORIES = ("action", "rejection")
DIGEST_CATEGORIES = ALERT_CATEGORIES + ("lead",)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
MONTH_ABBR = {m[:3]: i for m, i in MONTHS.items()}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday"]

# Explicit automation markers. These are safe to fire before the rejection
# rules, because a real rejection from a person never says "do not reply to
# this message" or carries a ticket reference.
AUTO_BODY = re.compile(
    r"do not (?:reply|respond)|do not reply to this|"
    r"this (?:message|email) (?:was|has been) (?:sent |auto(?:matically)? )?(?:generated|sent)|"
    r"automated (?:email|message|notification)|sent (?:automatically|by an automated)|"
    r"ticket (?:resolved|updated?|created|closed|#[0-9]+)|"
    r"your (?:support )?ticket\b|"
    r"this is an automated", re.I)
AUTO_SUBJECT = re.compile(
    r"^(?:automatic reply|auto-?reply|out of (?:office|office)|ooo\b|"
    r"auto-?submitted|undelivered|delivery (?:status|failure)|"
    r"mail delivery (?:subsystem|problem)|mailer-daemon|"
    r"acknowledgement|acknowledgment)\b|"
    r"\bticket (?:resolved|updated?|created|closed)\b|\[#?req-?\d{3,}\]", re.I)
AUTO_SENDER = re.compile(
    r"^(?:[a-z0-9._-]+\+)?(?:noreply|no-?reply|donotreply|do-?not-?reply|"
    r"automated|mailer|notifications?|system|helpdesk|servicedesk|ticketing)@", re.I)

REJECTION = re.compile(
    r"regret to inform|not moving forward|not be moving forward|"
    r"will not be (?:moving forward|proceeding|continuing)|"
    r"moving forward with (?:next steps|another|the)|"
    r"another candidate|other candidates|stronger candidates|"
    r"position (?:has been|is|was) (?:filled|closed)|"
    r"will not be proceeding|not proceeding with|pursuing other|"
    r"decided to (?:go with|move forward with|proceed with) (?:another|other|the)|"
    r"no longer (?:considering|a fit|in consideration)|"
    r"we have decided (?:against|not)|"
    r"did not meet the qualifications|"
    r"thank you for your interest,? (?:however|but|unfortunately)|"
    r"thank you for applying,? (?:however|but|unfortunately)|"
    r"unable to (?:move|proceed|continue) (?:forward|with)|"
    r"at this time (?:we|the team) (?:are|is) not|"
    r"we(?:'|’)ll keep your (?:application|resume) (?:on file|for future)|"
    r"wish you (?:the best|luck)(?: of luck)?(?: in| with| your| future)?", re.I)

ACTION = re.compile(
    r"\binterview\b|\binterviewing\b|schedule(?: a| an| your| the)?|"
    r"next step|assessment|take[- ]?home|coding (?:challenge|exercise)|"
    r"phone screen|screen(?:ing)? call|technical (?:screen|round|call)|"
    r"please (?:reply|respond|confirm|provide|send|share|let us know)|"
    r"reply (?:to confirm|with|by|before)|respond (?:by|before|with)|"
    r"could you|can you|would you (?:be able|mind|please)|"
    r"we(?:'|’)d like to (?:talk|speak|schedule|connect)|"
    r"excited to (?:speak|move|connect|chat)|"
    r"your availability|confirm (?:your|the) (?:availability|time|slot)|"
    r"we have (?:a|some) (?:quick )?questions?|"
    r"please (?:complete|submit|upload|provide us with)|"
    r"refer(?:red)? you to|pass(?:ing)? you (?:along|on) to|"
    r"recruiter|hiring manager|talent (?:team|partner)|calendly|"
    r"let(?:'|’)s (?:connect|chat|talk)|follow(?:-| )?up with you", re.I)

# Recruitment scams. They look like an interview invitation, so without this
# they get pushed to Jordan's phone as "reply to schedule the interview" - a
# false alarm that costs him time and can cost him credibility if he replies.
# On 2026-10-14 an email from hr.innovativetech@hey.com did exactly that:
# a menu of generic roles, one flat rate, no company, no requisition number.
FREEMAIL_DOMAINS = (
    "hey.com", "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "aol.com", "mail.ru", "protonmail.com", "gmx.com", "zoho.com",
)
SCAM_MENU = re.compile(
    r"office assistant|data entry|chat support|customer service representative|"
    r"virtual assistant|data analyst|appointment setter|"
    r"(?:one of the following|any of the (?:following|roles|positions))", re.I)
SCAM_SIGNAL = re.compile(
    r"remote recruitment|remote recruitment process|"
    r"regardless of (?:your )?experience|no experience (?:required|necessary)|"
    r"start date:?\s*flexible|upon completion of training|"
    r"flexible start|training provided|"
    r"microsoft teams|via teams|"
    r"compensation:\s*\$?\d+.*per hour|"
    r"we(?:'|’)ve been (?:hiring|expanding)|our growing team|"
    r"shortlisted for the next stage", re.I)

# Social and marketing mail. None of it is a recruiter, but it lands in the
# same inbox and used to reach his phone as "action" - a LinkedIn "someone you
# may know" is not a job lead, and a buzz he cannot trust is a buzz he stops
# reading. These are filed and searchable, just never pushed.
SOCIAL_DOMAINS = (
    "linkedin.com", "facebookmail.com", "instagram.com", "twitter.com",
    "meetup.com", "eventbrite.com", "youtube.com", "discord.com",
)
PROMO_DOMAINS = (
    "substack.com", "medium.com", "canva.com", "engage.canva.com",
    "choiceofgames.com", "mailchimp.com", "hubspot.com", "doordash.com",
    "grubhub.com", "paypal.com", "amazon.", "dropbox.com", "googlemail.com",
    "shopify.com", "twitch.tv", "spotify.com",
)
# Job boards and job newsletters. Their mail is a lead worth reading, but a
# daily digest should never buzz the phone.
NEWSLETTER_DOMAINS = (
    "weworkremotely.com", "m.weworkremotely.com", "swooped.com",
    "remotive.com", "jobicy.com", "remoteok.com", "workingnomads.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "smartrecruiters.com", "indeed-email.com", "dice.com", "ziprecruiter.com",
    "linkedin.com", "hired.com", "wellfound.com", "angel.co",
)
NOISE_SUBJECT = re.compile(
    r"posted:|is open to work|you may know|"
    r"^\s*[👤]|\bopen to work\b", re.I)

# Digest markers only. A recruiter saying "we are hiring" must never be
# demoted to a lead - that would silence a real phone call.
LEAD_SUBJECT = re.compile(
    r"^\s*🔥|new (?:remote )?jobs|job openings|"
    r"job (?:insights|alerts|digest)|salary at \d+ job|"
    r"wants (?:software |senior |junior )?(?:engineers|developers|analysts)|"
    r"is hiring for a remote role|meet this week|"
    r"\$\d+K[–-]\$\d+K|\$\d+K\+ and", re.I)

DEADLINE_TRIGGER = re.compile(
    r"\b(?:by|before|no later than|due(?:\s+by)?|deadline|expires?|"
    r"respond(?:\s+by)?|reply(?:\s+by)?|rsvp(?:\s+by)?|submit(?:\s+by)?)\b", re.I)
DATE_PATTERNS = [
    re.compile(r"\b(%s)\.? \d{1,2}(?:st|nd|rd|th)?(?:,? \d{2,4})?\b"
               % "|".join(list(MONTHS) + list(MONTH_ABBR)), re.I),
    re.compile(r"\b\d{1,2}\s+(%s)(?:,? \d{2,4})?\b"
               % "|".join(list(MONTHS) + list(MONTH_ABBR)), re.I),
    re.compile(r"\b(%s)\b" % "|".join(WEEKDAYS), re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
]
RELATIVE_DAYS = re.compile(
    r"\bwithin\s+(\d{1,3})\s+(business\s+|working\s+)?days?\b", re.I)


# --------------------------------------------------------------------------
def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def load_ledger():
    return _read_json(RESPONSES, {})


def save_ledger(ledger):
    with open(RESPONSES, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)


def load_state():
    return _read_json(STATE, {})


def load_seen():
    return _read_json(SEEN, {})


def _load_index(path):
    data = _read_json(path, {})
    if isinstance(data, dict) and "postings" in data:
        return {p["id"]: p for p in data["postings"] if isinstance(p, dict) and "id" in p}
    return data if isinstance(data, dict) else {}


def load_kitmap():
    return _read_json(KITMAP, {})


# Boards, not employers. A slug-hosted label from one of these tells you which
# site scraped the posting, never who is hiring — so the sender domain wins.
AGGREGATOR_HOSTS = {
    "jobicy", "remoteok", "weworkremotely", "remotive", "remote.co",
    "himalayas", "authenticjobs", "we-remote", "remoteworker", "jobspresso",
    "arc", "powerboard", "startupjobs", "weworkanywhere", "dynamite",
    "greenhouse", "lever", "smartrecruiters", "workable", "ashbyhq",
}

# Spelled the way they spell themselves, so the digest never says "esri".
KNOWN_EMPLOYERS = {
    "esri": "Esri", "gitlab": "GitLab", "canonical": "Canonical",
    "launchdarkly": "LaunchDarkly", "tenable": "Tenable",
    "dataiku": "Dataiku", "warehance": "Warehance",
    "caylent": "Caylent", "netdocuments": "NetDocuments",
}


def employer_from_address(addr):
    """'hr@launchdarkly.com' -> 'LaunchDarkly'. None for personal mailboxes
    and for boards that receive applications on their own domain."""
    m = re.search(r"@([A-Za-z0-9.-]+)", addr or "")
    if not m:
        return None
    domain = m.group(1).lower()
    host = domain.split(".")[0]
    if host in AGGREGATOR_HOSTS:
        return None
    if host in {"gmail", "outlook", "hotmail", "yahoo", "protonmail",
                "proton", "icloud", "aol", "guison"}:
        return None
    return KNOWN_EMPLOYERS.get(host, host[:1].upper() + host[1:])


def job_label(pid):
    """Human name for a posting id: kit -> board -> URL, worst case the id."""
    if not pid:
        return "(unmatched reply)"
    km = load_kitmap()
    if pid in km:
        e = km[pid]
        return " — ".join(x for x in (e.get("employer"), e.get("title")) if x)
    posts = _load_index(POSTINGS)
    if pid in posts:
        p = posts[pid]
        return " — ".join(x for x in (p.get("employer"), p.get("title")) if x)

    # Not in either index (boards rotate postings out). Rebuild from what is
    # left: the posting URL for the title, the address it was sent to for the
    # employer, since an aggregator hostname is never the hiring company.
    state = load_state()
    entry = state.get(pid) if isinstance(state.get(pid), dict) else {}
    employer = employer_from_address(entry.get("sent_to", ""))
    title = ""
    url = load_seen().get(pid)
    if url:
        t = title_from_url(url)
        if t:
            title = t
    if employer and title and employer.lower() in title.lower():
        title = re.sub(re.escape(employer), "", title, flags=re.I).strip(" -—,")
    if employer and title:
        return f"{employer} — {title}"
    return employer or title or pid


def title_from_url(url):
    try:
        parts = urlparse(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().removeprefix("www.")
    segs = [s for s in parts.path.split("/") if s]
    # greenhouse.io/employer/jobs/<id> — the company name is in the path, the
    # title is not, so return nothing here and let the employer come from the
    # address it was sent to rather than misreading 'jobs' as a title.
    if host.endswith("greenhouse.io") and len(segs) >= 3 and segs[1] == "jobs":
        return ""
    slug = segs[-1] if segs else ""
    slug = re.sub(r"[-_]?\d{3,}$", "", slug)     # trailing job id
    slug = re.sub(r"^\d+[-_]", "", slug)         # leading job id
    if not slug or slug.isdigit() or slug in ("jobs", "careers", "view", "new"):
        return ""
    return _display_title(slug.replace("-", " ").replace("_", " ").strip())


# Words that are acronyms in practice. Slugs arrive lowercase, so without
# this the morning digest says "Data Analyst Sql Databricks".
ACRONYMS = {
    "sql", "dba", "aws", "gcp", "api", "apis", "qa", "ui", "ux", "seo",
    "crm", "erp", "ml", "ai", "devops", "sre", "cto", "cio", "cta", "seo",
    "k8s", "linux", "ios", "android", "ar", "vr", "b2b", "b2c", "saas",
    "paas", "iaas", "e2e", "sla", "etl", "elt", "bi", "hr", "it", "gdpr",
    "hipaa", "soc", "iso", "pci", "nist", "cis", "aws", "vm", "vpn", "dns",
    "tcp", "udp", "http", "https", "json", "yaml", "ssh", "tls", "ssl",
}


def _display_title(text):
    """'senior qa engineer sql databricks' -> 'Senior QA Engineer SQL Databricks'."""
    small = {"and", "or", "of", "the", "for", "in", "a", "an", "to", "at"}
    out = []
    for i, w in enumerate(text.split()):
        low = w.lower()
        if i and low in small:
            out.append(low)
        elif low in ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


# --------------------------------------------------------------------------
def sender_address(frm):
    """Pull the bare address out of a From header."""
    if not frm:
        return ""
    m = re.search(r"<([^>]+)>", frm)
    return (m.group(1) if m else frm).strip().lower()


def _collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def gist(body, limit=190):
    """First slice of the body, for showing him what it actually says."""
    text = _collapse(body)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > limit // 2 else cut).rstrip(" ,;:-") + "…"


def parse_deadline(text, base=None):
    """Pull a calendar deadline out of a reply. Returns YYYY-MM-DD or None.

    Only fires when a due-word sits within 40 characters ahead of a date, so
    "I joined in March" and "since 10/2021" never read as deadlines.
    """
    if not text:
        return None
    base = base or datetime.datetime.now()
    if base.tzinfo:
        base = base.replace(tzinfo=None)

    # "within 5 business days" is a deadline on its face — no trigger word
    # needed, unlike a bare date which could be anything.
    rel = RELATIVE_DAYS.search(text)
    if rel:
        n = int(rel.group(1))
        if rel.group(2):
            added, cursor = 0, base
            while added < n:
                cursor += datetime.timedelta(days=1)
                if cursor.weekday() < 5:
                    added += 1
            return cursor.date().isoformat()
        return (base + datetime.timedelta(days=n)).date().isoformat()

    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            window = text[max(0, m.start() - 40):m.start()]
            if not DEADLINE_TRIGGER.search(window):
                continue
            d = _resolve_date(m, base)
            if d and d >= base.date() - datetime.timedelta(days=1):
                return d.isoformat()
    return None


def _resolve_date(m, base):
    token = m.group(0).strip().lower()
    weekday = next((w for w in WEEKDAYS if token.startswith(w[:4])), None)
    if weekday:
        target = WEEKDAYS.index(weekday)
        d = base.date()
        for _ in range(8):
            d += datetime.timedelta(days=1)
            if d.weekday() == target:
                return d
        return None

    # MM/DD or MM/DD/YYYY — US order, which is what recruiters write.
    if "/" in token:
        nums = [int(n) for n in re.findall(r"\d+", token)]
        if len(nums) >= 2 and 1 <= nums[0] <= 12 and 1 <= nums[1] <= 31:
            year = nums[2] if len(nums) > 2 else base.year
            if year < 100:
                year += 2000
            try:
                d = datetime.date(year, nums[0], nums[1])
            except ValueError:
                return None
            if len(nums) < 2 + 1 and d < base.date() - datetime.timedelta(days=180):
                d = datetime.date(year + 1, nums[0], nums[1])
            return d
        return None

    num = re.search(r"\d{1,2}", token)
    mon = next((v for k, v in MONTHS.items() if token.startswith(k[:3])), None)
    if not num or not mon:
        return None
    day = int(num.group(0))
    if not 1 <= day <= 31:
        return None
    year_m = re.search(r"\b(20\d{2})\b", token)
    year = int(year_m.group(1)) if year_m else base.year
    try:
        d = datetime.date(year, mon, day)
    except ValueError:
        return None
    if not year_m and d < base.date() - datetime.timedelta(days=180):
        d = datetime.date(year + 1, mon, day)
    return d


def is_scam(subject, body, frm=""):
    """True for recruitment scams, not for real recruiters.

    Requires a freemail sender AND a generic role menu AND a scam
    signal, so a genuine recruiter on a company domain never trips it.
    """
    addr = sender_address(frm)
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    if domain not in FREEMAIL_DOMAINS:
        return False
    text = (subject or "") + "\n" + (body or "")
    if not (SCAM_MENU.search(text) and SCAM_SIGNAL.search(text)):
        return False
    # A real posting names the employer and usually a requisition number.
    if re.search(r"\b(?:job (?:id|requisition|code)|req(?:uisition)?[- ]?(?:id|#)|posting id)\b",
                 text, re.I):
        return False
    return True


def is_noise(subject, body, frm=""):
    """True for social notifications and marketing mail - never a recruiter."""
    addr = sender_address(frm)
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    if any(d in domain for d in PROMO_DOMAINS):
        return True
    if any(d in domain for d in SOCIAL_DOMAINS):
        # A LinkedIn job digest is a lead; a LinkedIn "you may know" is not.
        return not LEAD_SUBJECT.search(subject or "")
    return bool(NOISE_SUBJECT.search(subject or ""))


def is_newsletter(frm):
    """True when the mail comes from a job board or job-alert newsletter."""
    addr = sender_address(frm)
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    return any(d in domain for d in NEWSLETTER_DOMAINS)


def is_lead(subject, body, frm=""):
    """True for a job-board digest or job-alert newsletter."""
    addr = sender_address(frm)
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    if any(d in domain for d in NEWSLETTER_DOMAINS):
        return True
    return bool(LEAD_SUBJECT.search(subject or ""))


def classify(subject, body, frm="", when=None):
    """-> {"category", "action", "deadline", "says"}."""
    subject = subject or ""
    body = body or ""
    frm = frm or ""
    addr = sender_address(frm)
    hay_s, hay_b = subject, body
    when = when or datetime.datetime.now()

    def is_auto():
        local = addr.split("@")[0]
        if AUTO_SENDER.match(local + "@"):
            return True
        if AUTO_SUBJECT.search(hay_s):
            return True
        return bool(AUTO_BODY.search(hay_b) or AUTO_BODY.search(hay_s))

    if is_scam(hay_s, hay_b, frm):
        category = "scam"
    elif is_noise(hay_s, hay_b, frm):
        category = "noise"
    elif is_auto():
        category = "auto"
    elif REJECTION.search(hay_b) or REJECTION.search(hay_s):
        category = "rejection"
    elif is_newsletter(frm):
        category = "lead"
    elif ACTION.search(hay_b) or ACTION.search(hay_s):
        category = "action"
    elif LEAD_SUBJECT.search(hay_s) or LEAD_SUBJECT.search(hay_b):
        category = "lead"
    else:
        category = "human"

    deadline = None
    action = None
    if category == "action":
        deadline = parse_deadline(hay_b, when) or parse_deadline(hay_s, when)
        action = action_request(hay_b, hay_s, deadline)
    elif category == "scam":
        action = "None — recruitment scam. Do not reply."
    elif category == "noise":
        action = "None — social notification, not a lead."
    elif category == "lead":
        action = "None — job digest lead. Read in the morning digest."
    elif category == "rejection":
        action = "None — application closed."
    elif category == "auto":
        action = "None — automated."
    else:
        action = "None — no ask detected."

    return {
        "category": category,
        "action": action,
        "deadline": deadline,
        "says": gist(body) or gist(subject, 190),
    }


def action_request(body, subject, deadline=None):
    """Short imperative: the one line telling him what to do."""
    text = f"{subject}\n{body}"
    by = f" by {deadline}" if deadline else ""
    if re.search(r"interview|phone screen|screen(?:ing)? call|technical (?:screen|round)",
                 text, re.I):
        return f"Reply to schedule the interview{by}."
    if re.search(r"assessment|take[- ]?home|coding (?:challenge|exercise)|homework",
                 text, re.I):
        return f"Complete the assessment{by}."
    if re.search(r"confirm (?:your|the) (?:availability|time|slot)|your availability|"
                 r"let(?:'|’)s (?:connect|chat|talk)|calendly", text, re.I):
        return f"Confirm a time{by}."
    if re.search(r"please (?:complete|submit|upload|provide|send|share)|"
                 r"could you (?:send|provide|share)|we need (?:your|the)", text, re.I):
        return f"Send what they asked for{by}."
    if re.search(r"question", text, re.I):
        return f"Answer their question{by}."
    if deadline:
        return f"Reply{by}."
    return "Reply to them."


# --------------------------------------------------------------------------
def record(pid=None, uid=None, msgid=None, frm="", subject="", date=None,
           body="", extra=None):
    """File one reply into the ledger. Idempotent: an existing record keeps
    its alerted_at so re-scanning the same mail never re-notifies him."""
    ledger = load_ledger()
    key = (msgid or "").strip() or f"uid:{uid}"
    if not key or key == "uid:":
        raise ValueError("record() needs a msgid or a uid")

    parsed = None
    if date:
        try:
            parsed = email.utils.parsedate_to_datetime(date)
        except (TypeError, ValueError, IndexError):
            parsed = None
    if parsed is None:
        parsed = datetime.datetime.now()
    if parsed.tzinfo:
        parsed = parsed.replace(tzinfo=None)

    verdict = classify(subject, body, frm, when=parsed)
    existing = ledger.get(key, {})
    entry = {
        "pid": pid or existing.get("pid"),
        "uid": uid if uid is not None else existing.get("uid"),
        "msgid": msgid or existing.get("msgid"),
        "from": frm or existing.get("from", ""),
        "subject": subject or existing.get("subject", ""),
        "date": parsed.isoformat(timespec="seconds"),
        "category": verdict["category"],
        "action": verdict["action"],
        "deadline": verdict["deadline"],
        "says": verdict["says"],
        "body": _collapse(body)[:2000] or existing.get("body", ""),
        "alerted_at": existing.get("alerted_at"),
        "recorded_at": existing.get("recorded_at")
                       or datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        entry.update(extra)
    ledger[key] = entry
    save_ledger(ledger)
    return key, entry


def pending(ledger=None, categories=ALERT_CATEGORIES):
    """Un-acked replies worth his attention, newest first.
    Returns [(key, entry), ...] so callers can ack without re-matching."""
    ledger = ledger if ledger is not None else load_ledger()
    out = [(k, v) for k, v in ledger.items()
           if not str(k).startswith("followup:")
           and v.get("category") in categories
           and not v.get("alerted_at")]
    out.sort(key=lambda kv: kv[1].get("date") or "", reverse=True)
    return out


def ack(keys, when=None):
    ledger = load_ledger()
    stamp = (when or datetime.datetime.now()).isoformat(timespec="seconds")
    changed = False
    for key in keys:
        if key in ledger and not ledger[key].get("alerted_at"):
            ledger[key]["alerted_at"] = stamp
            changed = True
    if changed:
        save_ledger(ledger)
    return changed


# --------------------------------------------------------------------------
def human_replied(pid):
    """True only when a person actually came back — auto/ticket mail does not
    count, which is what stopped Dataiku/Esri being treated as answered."""
    if not pid:
        return False
    return any(v.get("pid") == pid
               and v.get("category") in ("action", "rejection", "human")
               for v in load_ledger().values())


def _age_days(updated):
    if not updated:
        return None
    try:
        d = datetime.datetime.fromisoformat(updated)
    except ValueError:
        return None
    return (datetime.datetime.now() - d).days


def followups(threshold=FOLLOWUP_AFTER_DAYS):
    """Sent applications a person has not answered in N days, oldest first."""
    state = load_state()
    rows = []
    for pid, e in state.items():
        if not isinstance(e, dict) or not e.get("sent_to"):
            continue
        if e.get("status") in ("skip", "closed", "offer", "interview"):
            continue
        if human_replied(pid):
            continue
        age = _age_days(e.get("updated"))
        if age is None or age < threshold:
            continue
        rows.append({
            "pid": pid,
            "age": age,
            "to": e.get("sent_to"),
            "status": e.get("status", "?"),
            "milestone": age // FOLLOWUP_AFTER_DAYS,
        })
    rows.sort(key=lambda r: -r["age"])
    return rows


def pending_followups(threshold=FOLLOWUP_AFTER_DAYS):
    """Follow-ups not yet nudged at their current 7-day milestone."""
    ledger = load_ledger()
    out = []
    for row in followups(threshold):
        seen = ledger.get(f"followup:{row['pid']}", {}).get("milestone", 0)
        if row["milestone"] > seen:
            out.append(row)
    return out


def ack_followups(rows, when=None):
    if not rows:
        return False
    ledger = load_ledger()
    stamp = (when or datetime.datetime.now()).isoformat(timespec="seconds")
    for row in rows:
        ledger[f"followup:{row['pid']}"] = {
            "milestone": row["milestone"],
            "alerted_at": stamp,
        }
    save_ledger(ledger)
    return True


def followup_draft(pid):
    """A ready-to-send follow-up for one application."""
    state = load_state()
    e = state.get(pid) or {}
    label = job_label(pid)
    parts = label.split(" — ")
    employer = parts[0] if len(parts) > 1 else label
    title = parts[-1] if len(parts) > 1 else ""
    to = e.get("sent_to", "")
    age = _age_days(e.get("updated")) or 0
    sent = (e.get("updated") or "")[:10]

    applied = f" on {sent}" if sent else ""
    for_role = f" for {title}" if title else ""
    subject = f"Following up — {title or label}"
    body = (
        f"Hi {employer} team,\n\n"
        f"I applied{applied} and wanted to follow up on my application"
        f"{for_role}. It has been about {age} days, and I remain very "
        f"interested.\n\n"
        f"I'd welcome the chance to talk about how my background in platform "
        f"engineering, infrastructure automation, and Linux systems maps to "
        f"the role. Happy to work around your schedule.\n\n"
        f"Thanks for your time,\nJordan"
    )
    return {"pid": pid, "to": to, "subject": subject, "body": body, "age": age}


# --------------------------------------------------------------------------
def _fmt_date(iso):
    if not iso:
        return ""
    try:
        return datetime.date.fromisoformat(iso).strftime("%b %d")
    except ValueError:
        return iso


def _print_alerts(items, ledger, replay=False):
    print(f"  New replies needing you: {len(items)}")
    for _, e in items:
        tag = "ACTION" if e.get("category") == "action" else "REJECT"
        when = (e.get("date") or "")[:10]
        print(f"    [{tag}] {job_label(e.get('pid'))}  ({when})")
        print(f"      from: {e.get('from', '')[:80]}")
        if e.get("says"):
            print(f"      says: {e['says']}")
        if e.get("action"):
            print(f"      do:   {e['action']}")
        if e.get("deadline"):
            print(f"      by:   {_fmt_date(e['deadline'])}")
        print()
    if replay:
        acked = sum(1 for k, v in ledger.items()
                    if not str(k).startswith("followup:")
                    and v.get("category") in ALERT_CATEGORIES
                    and v.get("alerted_at"))
        print(f"  (replaying; {acked} already acked earlier)")


def _print_followups(rows, acked=False):
    if not rows:
        return
    print(f"  Silent past {FOLLOWUP_AFTER_DAYS}d: {len(rows)}")
    for r in rows:
        print(f"    {job_label(r['pid'])}  {r['age']}d, no reply  -> {r['to']}")
    print(f"    draft one:  python3 triage.py followup --draft {rows[0]['pid']}")
    if acked:
        print("  (replaying; these were acked earlier)")
    print()


def cmd_digest(args):
    ledger = load_ledger()
    if args.all:
        items = [(k, v) for k, v in ledger.items()
                 if not str(k).startswith("followup:")
                 and v.get("category") in ALERT_CATEGORIES]
        items.sort(key=lambda kv: kv[1].get("date") or "", reverse=True)
        # --all is the "unless he asks again" path, so it replays the silent
        # applications too rather than only the replies.
        fol = followups()
    else:
        items = pending(ledger)
        fol = pending_followups()

    if not items and not fol:
        return 0

    print()
    print("JOB REPLIES" + (" (replay)" if args.all else ""))
    print("-" * 62)
    if items:
        _print_alerts(items, ledger, replay=args.all)
    if fol:
        _print_followups(fol, acked=args.all)

    if not args.no_ack:
        # Ack only what was genuinely new. On --all that is the subset of the
        # replay that had never been shown, so a replay still consumes an
        # alert he had not yet seen without re-stamping the old ones.
        ack([k for k, v in ledger.items()
             if not str(k).startswith("followup:")
             and v.get("category") in ALERT_CATEGORIES
             and not v.get("alerted_at")])
        ack_followups(pending_followups())
    return 0


def cmd_pending(args):
    ledger = load_ledger()
    items = pending(ledger)
    fol = pending_followups()
    if not items and not fol:
        return 0
    print()
    print("JOB REPLIES (not acked)")
    print("-" * 62)
    if items:
        _print_alerts(items, ledger)
    if fol:
        _print_followups(fol)
    return 0


def cmd_followup(args):
    if args.draft:
        d = followup_draft(args.draft)
        print(f"To:      {d['to']}")
        print(f"Subject: {d['subject']}")
        print()
        print(d["body"])
        return 0
    rows = pending_followups()
    if not rows:
        print(f"No application has been silent {FOLLOWUP_AFTER_DAYS}+ days "
              f"without an ack.")
        return 0
    print()
    _print_followups(rows)
    if not args.no_ack:
        ack_followups(rows)
    return 0


def cmd_ack(args):
    ledger = load_ledger()
    keys = [k for k, v in ledger.items()
            if not str(k).startswith("followup:")
            and v.get("category") in ALERT_CATEGORIES
            and not v.get("alerted_at")]
    ack(keys)
    fol = pending_followups()
    ack_followups(fol)
    print(f"acked {len(keys)} repl{'y' if len(keys) == 1 else 'ies'} "
          f"and {len(fol)} follow-up{'s' if len(fol) != 1 else ''}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="triage", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("digest", help="alerts needing Jordan, then ack them")
    p.add_argument("--all", action="store_true", help="replay every alert")
    p.add_argument("--no-ack", action="store_true", help="print only")

    p = sub.add_parser("pending", help="same list, no writes")
    p.add_argument("--no-ack", action="store_true")

    p = sub.add_parser("followup", help="applications that went silent")
    p.add_argument("--draft", metavar="ID", help="print a ready-to-send draft")
    p.add_argument("--no-ack", action="store_true")

    sub.add_parser("ack", help="clear everything currently pending")

    args = ap.parse_args()
    cmd = args.cmd or "digest"
    if not hasattr(args, "all"):
        args.all = False
    if not hasattr(args, "no_ack"):
        args.no_ack = False
    if not hasattr(args, "draft"):
        args.draft = None
    return {"digest": cmd_digest, "pending": cmd_pending,
            "followup": cmd_followup, "ack": cmd_ack}[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
