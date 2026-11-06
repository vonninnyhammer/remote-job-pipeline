#!/usr/bin/env python3
"""Rebuild the sent_msgid map in state.json from Gmail's Sent folder.

state.json was found at 0 bytes on 2026-10-18 07:00 (crash mid-write).
That file was the only place recording, for every auto-sent application,
the posting id and the Message-ID of the email that was sent. Without it
notify.py's _classify() cannot tie a recruiter's reply back to the
posting it belongs to, so replies land in the ledger with no posting
attached.

Gmail still has every one of those sent messages, and autosend.py wrote
a stable subject line - "<title> - <employer> (Remote) application" - so
the posting can be recovered by matching the subject against the master.

Usage:
    python3 recover_sent_state.py [--dry-run]
"""
import argparse
import email
import email.utils
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline  # noqa: E402
import scrape  # noqa: E402
from conf import get_config  # noqa: E402
from notify import _imap_connect  # noqa: E402

KITS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kits")

SENT_SUBJECT_RE = re.compile(r"^(?P<title>.+?)\s+-\s+(?P<employer>.+?)\s+\(Remote\) application\s*$")


def sent_messages(m):
    """Yield (message_id, subject, to, employer, title) for sent applications."""
    typ, boxes = m.list()
    sent_box = None
    for b in boxes or []:
        s = b.decode(errors="replace")
        if "\\Sent" not in s:
            continue
        # '(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"' - the folder
        # name is the LAST quoted segment, not the first.
        names = re.findall(r'"([^"]+)"', s)
        if names:
            sent_box = names[-1]
            break
    if sent_box is None:
        raise SystemExit("could not find a Sent folder")

    typ, _ = m.select('"%s"' % sent_box)
    if typ != "OK":
        raise SystemExit("cannot select %s" % sent_box)
    print("scanning %s" % sent_box)

    typ, data = m.search(None, "ALL")
    uids = (data[0].split() if data and data[0] else [])
    print("%d message(s) in the folder" % len(uids))

    for u in uids:
        typ, msgdata = m.uid("fetch", u, "(RFC822)")
        if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
            continue
        msg = email.message_from_bytes(msgdata[0][1])
        subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject") or "")))
        hit = re.match(SENT_SUBJECT_RE, subject.strip())
        if not hit:
            continue
        yield (msg.get("Message-ID"), subject, msg.get("To"),
               hit.group("employer").strip(), hit.group("title").strip())


def merge_kits_into_master(master):
    """Put back the posting records that lived in the kits.

    The rebuilt master only holds what today's boards return. The kits
    still carry the full posting record for every role the pipeline
    prepared, so merging them restores the tracked universe instead of
    silently shrinking it to whatever is still open.
    """
    plist = master.setdefault("postings", [])
    ids = {p["id"] for p in plist}
    restored = 0
    for path in sorted(glob.glob(os.path.join(KITS, "*", "posting.json"))):
        try:
            p = json.load(open(path))
        except (json.JSONDecodeError, OSError):
            continue
        if not p.get("id") or p["id"] in ids:
            continue
        p["kit_name"] = os.path.basename(os.path.dirname(path))
        p["restored_from_kit"] = "2026-10-22"
        plist.append(p)
        ids.add(p["id"])
        restored += 1

    # Kits written by the newer kit path have no posting.json. Match them
    # against the master by employer + title so they keep their real id.
    by_pair = {(p.get("employer", "").strip().lower(), p.get("title", "").strip().lower()): p
               for p in plist}
    relinked = 0
    for kit in sorted(glob.glob(os.path.join(KITS, "*"))):
        if os.path.exists(os.path.join(kit, "posting.json")):
            continue
        name = os.path.basename(kit)
        stem = name.rsplit(" - $", 1)[0]
        if " - " not in stem:
            continue
        employer, title = stem.rsplit(" - ", 1)
        p = by_pair.get((employer.strip().lower(), title.strip().lower()))
        if p is not None:
            p["kit_name"] = name
            relinked += 1
    print("restored %d posting record(s) from kits; %d kit(s) re-linked to the master"
          % (restored, relinked))
    return master


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    postings = pipeline.load_postings()
    postings = merge_kits_into_master(postings)
    plist = postings.get("postings", [])
    by_pair = {}
    by_title = {}
    for p in plist:
        by_pair[(p.get("employer", "").strip().lower(), p.get("title", "").strip().lower())] = p
        by_title.setdefault(p.get("title", "").strip().lower(), []).append(p)

    st = pipeline.load_state()
    before = len(st)

    m = _imap_connect()
    cfg = get_config()
    m.login(cfg["user"], cfg["pw"])
    found = 0
    linked = 0
    for msgid, subject, to, employer, title in sent_messages(m):
        found += 1
        p = by_pair.get((employer.lower(), title.lower()))
        if p is None:
            cands = by_title.get(title.lower(), [])
            p = cands[0] if len(cands) == 1 else None
        if p is None:
            # The posting aged off today's boards, but the application was
            # still sent. Keep the linkage under a synthetic id so a reply
            # to this email still threads back to what Jordan applied for.
            pid = "sent:%s|%s" % (employer.lower(), title.lower())
            print("  no master posting (keeping synthetic link): %s" % subject)
        else:
            pid = p["id"]
        entry = st.setdefault(pid, {})
        if entry.get("sent_msgid") == msgid:
            continue
        entry["status"] = "submitted"
        entry["sent_msgid"] = msgid
        entry["sent_to"] = to
        if p is not None:
            entry["employer"] = p.get("employer")
            entry["title"] = p.get("title")
        entry["notes"] = "auto-sent by email to %s" % to
        entry["updated"] = "2026-10-22T08:00"
        linked += 1
        print("  linked %-22s %s" % (pid[:22], subject[:60]))
    m.logout()

    print("\n%d application email(s) found, %d posting link(s) restored" % (found, linked))
    print("state.json entries: %d -> %d" % (before, len(st)))
    print("master postings: %d" % len(plist))
    if not args.dry_run:
        pipeline.save_state(st)
        scrape.save_postings(postings)
        print("saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
