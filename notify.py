#!/usr/bin/env python3
"""
Response watcher + phone push
=============================
notify.py ping           send a test push to your phone (does NOT touch Gmail)
notify.py watch          scan Gmail for replies to applications, file every
                         relevant one in the triage ledger, and phone-push
                         only what actually needs Jordan (action / rejection)
notify.py status         show what the watcher currently knows

Ran from cron (e.g. every 15 min). Reads .env for Gmail + ntfy config.

Every relevant reply — including auto-replies and ticket confirmations — goes
into responses.json via triage.record(). Only action/rejection categories
buzzer's phone, and only those ever surface in the morning digest. Nothing is
pushed twice: the IMAP UID cursor moves forward regardless, and triage's
alerted_at keeps the digest one-shot.
"""
import argparse, datetime, email, json, os, re, shutil, sys, tempfile, time
from email.header import decode_header, make_header
import imaplib
import requests

from conf import get_config, BASE
import triage

WATCHER_STATE = os.path.join(BASE, "watcher_state.json")
# Every critical file in this pipeline is gitignored, so a truncated file is
# a lost file - that is exactly what happened on 2026-10-18 and it cost four
# days of mail visibility plus the whole posting master. These snapshots are
# the only way back, so they are written on every successful run.
SNAPSHOT_DIR = os.path.expanduser("~/.local/state/pipeline_snapshots/remote")
SNAPSHOT_FILES = ("state.json", "responses.json", "postings.json",
                  "watcher_state.json")


def snapshot_state():
    """Copy the critical JSON files aside, keeping the last 30 snapshots."""
    try:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        d = os.path.join(SNAPSHOT_DIR, stamp)
        os.makedirs(d, exist_ok=True)
        for name in SNAPSHOT_FILES:
            src = os.path.join(BASE, name)
            if os.path.exists(src) and os.path.getsize(src) > 0:
                shutil.copy2(src, os.path.join(d, name))
        snaps = sorted(os.listdir(SNAPSHOT_DIR))
        for old in snaps[:-30]:
            shutil.rmtree(os.path.join(SNAPSHOT_DIR, old), ignore_errors=True)
    except Exception as e:                       # noqa: BLE001
        print("snapshot skipped:", e)

# What is worth Jordan's attention. Mirrors triage.ALERT_CATEGORIES so the
# phone push and the morning digest never disagree about what counts.
ALERT_CATEGORIES = ("action", "rejection")

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


def _atomic_write_json(path, data):
    """Write via temp file + rename.

    state.json was found at 0 bytes on 2026-10-18 07:00 and postings.json
    at 0 bytes at 08:01 the same morning - a crash mid-write truncates
    them, and because they are gitignored there is no way back. An atomic
    rename means the file is either the old version or the new one.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_json_safe(path):
    """Load JSON, treating a missing or corrupt file as empty.

    A 0-byte state.json used to crash the watcher on every run, which
    silently blinded it to every job response until someone noticed.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: {path} is unreadable ({e}) - treating as empty")
        return {}


def load_state():
    return _load_json_safe(os.path.join(BASE, "state.json"))


def save_state(st):
    _atomic_write_json(os.path.join(BASE, "state.json"), st)


def load_watcher():
    return _load_json_safe(WATCHER_STATE)


def save_watcher(w):
    _atomic_write_json(WATCHER_STATE, w)


def _body_text(msg, limit=6000):
    """Concatenated text/plain body. Enough for triage to classify on."""
    chunks = []
    total = 0
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                txt = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace")
            except Exception:
                txt = ""
            if txt:
                chunks.append(txt)
                total += len(txt)
        if total >= limit:
            break
    out = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    return out[:limit]


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


def _classify(subject, body, refs, in_reply_to):
    """-> (matched_posting_ids, keyword_hit)

    matched: an exact Message-ID tie back to an auto-sent application, the
    reliable path. keyword_hit: RESPONSE_KW on subject+body, which is what
    catches a recruiter writing in cold with no thread to inherit — that
    branch existed but was never reachable, so those replies were dropped.
    """
    st = load_state()
    refs_text = " ".join(filter(None, [in_reply_to, refs]))
    for pid, e in st.items():
        sent_mid = e.get("sent_msgid") or ""
        if not sent_mid:
            continue
        base = re.sub(r"<|>", "", sent_mid)
        if base and (base in refs_text or base in in_reply_to):
            return [pid], bool(RESPONSE_KW.search(subject or "")
                               or RESPONSE_KW.search(body or ""))
    return [], bool(RESPONSE_KW.search(subject or "")
                    or RESPONSE_KW.search(body or ""))


def _imap_connect(retries=6, wait=20):
    """Open IMAP to Gmail, tolerating a network that is not up yet.

    This laptop sleeps. When it wakes, cron can fire before the Wi-Fi
    association and DHCP lease are done, and the first getaddrinfo fails
    with gaierror EAI_AGAIN. That happened 78 runs in a row between
    2026-10-18 and 2026-10-22 - the watcher was blind for four days,
    including the day Jack Henry sent the denial. A pending application
    that nobody watches dies of neglect rather than of merit, so this
    retries with a backoff instead of giving up on the first error.
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            return imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=120)
        except Exception as e:                    # noqa: BLE001
            last = e
            print(f"IMAP connect attempt {attempt}/{retries} failed: "
                  f"{type(e).__name__}: {e}")
            if attempt < retries:
                time.sleep(wait)
    raise last


def cmd_watch(args):
    cfg = get_config()
    m = _imap_connect()
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
        snapshot_state()
        m.logout()
        return 0

    new_uids = [u for u in uids if u > last_uid]
    if not new_uids:
        print("no new mail since last check")
        snapshot_state()
        m.logout()
        return 0

    st = load_state()
    hits = 0
    pushed = 0
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
        body = _body_text(msg)
        snippet = body[:240]

        matched, keyword = _classify(subj, body, refs, in_reply)
        verdict = triage.classify(subj, body, frm)
        relevant = bool(matched) or keyword or verdict["category"] in ALERT_CATEGORIES
        if not relevant:
            continue

        hits += 1
        try:
            key, entry = triage.record(
                pid=(matched[0] if matched else None), uid=u, msgid=msgid,
                frm=frm, subject=subj, date=date, body=body)
        except ValueError as e:
            print(f"  ledger skip UID {u}: {e}")
            entry = None

        # The phone buzzes only for things that actually need him. Auto-replies
        # and ticket confirmations land in the ledger silently, so the morning
        # digest can prove they happened without ever waking him for them.
        if entry and entry["category"] in ALERT_CATEGORIES:
            summary = f"From: {frm}\nSubject: {subj}\nDate: {date}\n\n"
            if entry.get("action"):
                summary += f"Action: {entry['action']}\n"
            if entry.get("deadline"):
                summary += f"By: {entry['deadline']}\n"
            summary += f"\n{snippet or '(no text body)'}"
            code, txt = ntfy(cfg, f"Job {entry['category']}: {subj[:85]}", summary,
                             priority="high", tags=["envelope"])
            print(f"  pushed UID {u} [{entry['category']}]: {subj[:55]}  -> ntfy {code}")
            if code != 200:
                print(f"    push problem: {txt}")
            else:
                pushed += 1

        # Flip the posting only when a person answered. An auto-ack marking a
        # job 'responded' is what made silent applications look handled.
        if matched and entry and entry["category"] != "auto":
            for pid in matched:
                e = st.setdefault(str(pid), {})
                if e.get("status") not in ("interview", "offer", "closed", "skip"):
                    e["status"] = "responded"
                note = (e.get("notes") or "")
                e["notes"] = (note + " | " if note else "") + f"response: {subj[:80]} ({date})"
                e["updated"] = datetime.datetime.now().isoformat(timespec="minutes")

    save_state(st)
    save_watcher({"last_uid": max_new, "last_run": datetime.datetime.now().isoformat(timespec="minutes")})
    snapshot_state()
    print(f"done: {hits} relevant reply(s), {pushed} pushed, "
          f"{len(new_uids)} new msgs scanned, cursor -> {max_new}")
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