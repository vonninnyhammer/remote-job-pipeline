#!/usr/bin/env python3
"""
Email-based auto-apply
======================
Sends tailored applications by email to postings that advertise an apply
address (mailto: or 'email us at ...'). This is the only fully-legitimate
auto-submit channel: Greenhouse/Lever/SmartRecruiters forms require either
CAPTCHA, one-click manual, or a partner API credential (SmartRecruiters).

  python3 autosend.py scan           # list what would be sent (dry run)
  python3 autosend.py send [--limit N] [--id ID ...]   # actually send

What it sends per posting:
  * cover letter (tailored from profile.json lane openers + bullets)
  * resume PDF attached
  * portfolio link in the body
Then marks the posting 'submitted' in state.json and pushes an ntfy
confirmation to your phone.

Safety rails:
  * only postings that pass the shortlist fit gate (target/watch, not veto)
  * never re-send a posting already in state.json (status != new)
  * de-dupe on a per-day sent counter (state.json 'sent_today') so a buggy
    run cannot blast the whole board
Reads .env for Gmail SMTP + ntfy.
"""
import argparse, json, os, re, smtplib, sys, datetime, email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import pipeline
from conf import get_config, BASE
import notify

SENT_TODAY_KEY = "sent_today"


def candidates():
    data = pipeline.load_postings()
    owner = pipeline.load_owner(data)
    st = pipeline.load_state()
    posts = data["postings"]
    out = []
    for p in posts:
        aid = (p.get("apply_email") or "").strip()
        if not aid:
            continue
        bucket = pipeline.fit_bucket(p)
        if bucket not in ("target", "watch"):
            print(f"  skip {p['id']:<9} {p['employer'][:22]:<24} veto/out-of-scope ({bucket})")
            continue
        st_ent = st.get(p["id"], {})
        if st_ent.get("status") and st_ent.get("status") != "new":
            print(f"  skip {p['id']:<9} {p['employer'][:22]:<24} already {st_ent.get('status')}")
            continue
        out.append((p, owner, st))
    return out, st


def build_email(p, owner):
    lane = p.get("lane", "Other")
    opener = (pipeline.LANE_OPENERS.get(lane)
              or "My skillset maps directly to this remote role.")
    bullets = pipeline.pick_bullets(p, owner, n=4)
    body_lines = [f"{p['employer']} / {p['title']}",
                  f"{owner['name']} | {owner['phone']} | {owner['email']} | Portfolio: {owner.get('portfolio', 'guison.net')}",
                  f"Residence (for this application): {owner.get('location_use', 'US (remote)')}",
                  "",
                  "Dear Hiring Team,",
                  "",
                  opener,
                  "",
                  "Relevant experience:",
                  ""]
    body_lines += [f"  - {txt}" for _, txt in bullets]
    body_lines += ["", p.get("angle", ""), "",
                   f"I have attached my resume ({owner.get('resume_pdf', 'JordanGuison_Resume.pdf')}). My engineering "
                   f"portfolio with architecture details and a troubleshooting log is at {owner.get('portfolio', 'guison.net')}. "
                   f"I am happy to complete any technical screen or skills assessment you use.",
                   "",
                   "Respectfully,",
                   owner["name"], owner["phone"], owner["email"], ""]
    return "\n".join(body_lines)


def compose(p, owner):
    body = build_email(p, owner)
    subject = f"{p['title']} - {p['employer']} (Remote) application"
    msg = MIMEMultipart()
    msg["From"] = owner["email"]
    msg["To"] = p["apply_email"]
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="guison.net")
    msg["Reply-To"] = owner["email"]
    msg.attach(MIMEText(body, "plain", "utf-8"))
    resume = owner.get("resume_path") or os.path.join(BASE, "JordanGuison_Resume.pdf")
    if os.path.exists(resume):
        with open(resume, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="pdf")
            part["Content-Disposition"] = f"attachment; filename={os.path.basename(resume)}"
            msg.attach(part)
    else:
        print(f"  WARNING: resume not found at {resume} - sending without attachment")
    return msg, body


def cmd_scan(args):
    cands, _ = candidates()
    print(f"{len(cands)} posting(s) auto-sendable by email\n")
    for p, _, _ in cands:
        print(f"  {p['id']:<9} {p['employer'][:24]:<26} {p['title'][:44]}")
        print(f"            -> {p['apply_email']}   (fit={pipeline.fit_bucket(p)}, lane={p.get('lane')})")
    if not cands:
        print("(none - the email-apply channel needs postings whose description advertises an apply address)")
    return 0


def cmd_send(args):
    cfg = get_config()
    cands, st = candidates()
    if not cands:
        print("nothing to send")
        return 0

    owner = pipeline.load_owner(pipeline.load_postings())
    day = datetime.date.today().isoformat()
    sent_today = st.get(SENT_TODAY_KEY, {})
    if sent_today.get("date") != day:
        sent_today = {"date": day, "count": 0, "ids": []}
    limit = args.limit
    remaining = limit - sent_today["count"]
    pick = [c for c in cands if c[0]["id"] not in sent_today["ids"]][:max(remaining, 0)]
    if remaining <= 0:
        print(f"daily cap of {limit} reached - nothing sent today")
        return 0
    if not pick:
        print("all sendable postings already sent today")
        return 0

    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
    try:
        smtp.login(cfg["user"], cfg["pw"])
    except Exception as e:
        print("SMTP login failed:", e)
        return 1

    sent = 0
    for p, oner, _ in pick:
        msg, _ = compose(p, oner)
        try:
            smtp.sendmail(oner["email"], [p["apply_email"]], msg.as_string())
        except Exception as e:
            print(f"  FAIL {p['id']} {p['employer']}: {type(e).__name__}: {e}")
            continue
        entry = st.setdefault(p["id"], {})
        entry["status"] = "submitted"
        entry["sent_to"] = p["apply_email"]
        entry["sent_msgid"] = msg["Message-ID"]
        entry["notes"] = f"auto-sent by email to {p['apply_email']}"
        entry["updated"] = datetime.datetime.now().isoformat(timespec="minutes")
        sent_today["count"] += 1
        sent_today["ids"].append(p["id"])
        sent += 1
        print(f"  SENT {p['id']} {p['employer']} -> {p['apply_email']} ({p['title'][:44]})")
        code, txt = notify.ntfy(cfg, f"Application sent: {p['employer']}",
                                f"{p['title']}\n-> {p['apply_email']}\nKit: kits/{p.get('kit_name') or p['id']}",
                                priority="default", tags=["rocket"])
        if code != 200:
            print(f"    ntfy push problem: {txt}")
    smtp.quit()
    st[SENT_TODAY_KEY] = sent_today
    pipeline.save_state(st)
    print(f"\ndone: {sent} application(s) emailed today; daily remaining = {limit - sent_today['count']}")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="autosend", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s_scan = sub.add_parser("scan")
    s_scan.set_defaults(fn=cmd_scan)
    s_send = sub.add_parser("send")
    s_send.add_argument("--limit", type=int, default=20)
    s_send.set_defaults(fn=cmd_send)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())