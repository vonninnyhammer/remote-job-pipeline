#!/usr/bin/env python3
"""
Response watcher + phone push
=============================
notify.py ping           send a test push to your phone (does NOT touch Gmail)
notify.py watch          scan Gmail for replies to auto-sent applications,
                         push a phone notification per response, and flip the
                         matching posting to 'responded' in state.json.
notify.py status         show what the watcher currently knows

Ran from cron (e.g. every 30 min). Reads .env for Gmail + ntfy config.
"""
import argparse, datetime, email, json, os, re, sys
from email.header import decode_header, make_header
import imaplib
import requests

from conf import get_config, BASE

WATCHER_STATE = os.path.join(BASE, "watcher_state.json")

RESPONSE_KW = re.compile(
    r"interview|offer|invitation|application (status|update|received)|"
    r"(your|the) application|next step|onward (in the|in)?|assessment|coding (challenge|exercise)|"
    r"take.?home|recruiter|hiring (manager|team)|shortlist|withdrawn|consider(ing|ation)|"
    r"we (want|would love)|excited to (speak|move)|schedule|calendar invite|phone screen",
    re.I)


def ntfy(cfg, title, body, priority="default", tags=None):
    topic = cfg["topic"]
    title = re.sub(r"\s+", " ", title).strip()[:250]
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        r = requests.post(f"{cfg['url']}/{topic}", data=body.encode("utf-8"),
                          headers=headers, timeout=15)
        return r.status_code, r.text[:120]
    except Exception as e:
        return None, str(e)


def _dec(txt):
    if not txt:
        return ""
    try:
        return str(make_header(decode_header(txt)))
    except Exception:
        if isinstance(txt, bytes):
            return txt.decode("utf-8", "replace")
        return str(txt)


def load_state():
    p = os.path.join(BASE, "state.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


def save_state(st):
    with open(os.path.join(BASE, "state.json"), "w") as f:
        json.dump(st, f, indent=2)


def load_postings():
    p = os.path.join(BASE, "postings.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f).get("postings", [])
    return []


def load_watcher():
    if os.path.exists(WATCHER_STATE):
        with open(WATCHER_STATE) as f:
            return json.load(f)
    return {}


def save_watcher(w):
    with open(WATCHER_STATE, "w") as f:
        json.dump(w, f, indent=2)


def _body_snippet(msg):
    """First ~240 chars of the first text/plain part."""
    out = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not out:
            try:
                out = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace")
            except Exception:
                out = ""
    out = re.sub(r"\s+", " ", out).strip()
    return out[:240]


# ---------------------------------------------------------------------------
def cmd_ping(args):
    cfg = get_config()
    code, txt = ntfy(cfg, "Job pipeline online",
                     "@you remote_job_pipeline is watching your Gmail for responses.",
                     priority="default", tags=["white_check_mark"])
    print("ping ->", code, txt)
    if code == 200:
        print(f"\nIf your phone doesn't buzz: install the ntfy app and subscribe to topic\n"
              f"  {cfg['url']}/{cfg['topic']}\n"
              f"or subscribe from a browser at the same URL.")
        return 0
    print("\npush failed - see error above. Check NTFY_URL/NTFY_TOPIC in .env")
    return 1


def cmd_status(args):
    cfg = get_config()
    w = load_watcher()
    st = load_state()
    applied = [pid for pid, e in st.items() if e.get("sent_to")]
    print(f"ntfy topic : {cfg['url']}/{cfg['topic']}")
    print(f"last uid   : {w.get('last_uid', 'not set (baseline on next watch)')}")
    print(f"auto-sent  : {len(applied)} applications in state.json")
    for pid in applied[:20]:
        e = st[pid]
        print(f"   {pid:<9} {e.get('status','?'):<12} -> {e.get('sent_to')}  ({e.get('subject_msgid','')})")
    return 0


def _classify(subject, refs, in_reply_to):
    """Return (matched_posting_ids_or_None, is_keyword_reply)."""
    st = load_state()
    refs_text = " ".join(filter(None, [in_reply_to, refs]))
    for pid, e in st.items():
        sent_mid = e.get("sent_msgid") or ""
        if not sent_mid:
            continue
        base = re.sub(r"<|>", "", sent_mid)
        if base in refs_text or f"<{base}>" in refs_text or base in in_reply_to or "":
            return [pid], True, False
    return None, False, False


def cmd_watch(args):
    cfg = get_config()
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        m.login(cfg["user"], cfg["pw"])
    except Exception as e:
        print("IMAP login failed:", e)
        return 1
    m.select("INBOX")
    status, data = m.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        print("no messages found")
        m.logout()
        return 0
    uids = [int(x) for x in data[0].split()]
    last_uid = load_watcher().get("last_uid")
    if not last_uid:
        save_watcher({"last_uid": max(uids), "baseline_at": datetime.datetime.now().isoformat(timespec="minutes")})
        print(f"baseline set at UID {max(uids)} ({len(uids)} msgs in box) - already-read backlog will not notify")
        m.logout()
        return 0

    new_uids = [u for u in uids if u > last_uid]
    if not new_uids:
        print("no new mail since last check")
        m.logout()
        return 0

    st = load_state()
    postings = {p["id"]: p for p in load_postings()}
    hits = 0
    max_new = max(new_uids)
    for u in new_uids:
        status, data = m.uid("fetch", str(u), "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            continue
        try:
            msg = email.message_from_bytes(data[0][1])
        except Exception:
            continue
        subj = _dec(msg.get("Subject"))
        frm = _dec(msg.get("From"))
        date = msg.get("Date")
        refs = _dec(msg.get("References"))
        in_reply = _dec(msg.get("In-Reply-To"))
        msgid = _dec(msg.get("Message-ID"))
        snippet = _body_snippet(msg)

        matched, keyword, _ = _classify(subj, refs, in_reply)
        relevant = matched or (keyword)
        if not relevant:
            continue

        # match posting by recency/rules is handled above; mark as responded
        hits += 1
        body = f"From: {frm}\nSubject: {subj}\nDate: {date}\nUID: {u}\n\n{snippet or '(no text body)'}"
        code, txt = ntfy(cfg, f"Job response: {subj[:90]}", body,
                         priority="high", tags=["envelope"])
        print(f"  pushed UID {u}: {subj[:60]}  -> ntfy {code}")
        if code != 200:
            print(f"    push problem: {txt}")

        if matched:
            for pid in matched:
                e = st.setdefault(str(pid), {})
                if e.get("status") not in ("interview", "offer", "closed", "skip"):
                    e["status"] = "responded"
                note = (e.get("notes") or "")
                e["notes"] = (note + " | " if note else "") + f"response: {subj[:80]} ({date})"
                e["updated"] = datetime.datetime.now().isoformat(timespec="minutes")

    save_state(st)
    save_watcher({"last_uid": max_new, "last_run": datetime.datetime.now().isoformat(timespec="minutes")})
    print(f"done: {hits} response(s) found, {len(new_uids)} new msgs scanned, cursor -> {max_new}")
    m.logout()
    return 0


def main():
    ap = argparse.ArgumentParser(prog="notify", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    sub.add_parser("watch")
    sub.add_parser("status")
    args = ap.parse_args()
    return {"ping": cmd_ping, "watch": cmd_watch, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())