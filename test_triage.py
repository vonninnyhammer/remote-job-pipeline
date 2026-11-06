#!/usr/bin/env python3
"""Triage tests: what wakes Jordan, what never does, and the one-shot rule.

These are the properties the morning alert exists to guarantee, in the order
they broke when the alert was just "print the last three responded entries":

  * an auto-reply or ticket confirmation is never an alert — that is what made
    the same three stale entries print every morning for a week;
  * a rejection and an actionable human reply always are;
  * a reply is alerted once, then never again unless --all is asked for;
  * "within 5 business days" is a deadline, but "since 10/2021" is not;
  * a silent application surfaces as a follow-up, then waits its turn
    instead of nagging every morning.

No network, no Gmail. Writes only to a temp ledger, never the real one.

Run: /home/jordan/.mempalace-venv/bin/python test_triage.py
"""
import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILS = []


def check(ok, label, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


# --------------------------------------------------------------------------
print("classify: what is never an alert")

auto_cases = [
    ("Automatic reply: GIS Industry Solutions Specialist - Esri",
     "This is an automated response.", "askcareers@esri.com"),
    ("Ticket Resolved - Anaplan Systems Architect - Dataiku",
     "Your ticket REQ-118600 has been resolved. Do not reply to this message.",
     "reasonable-accommodations@dataiku.com"),
    ("Out of Office: Re: Backend Engineer",
     "I am out of the office until Monday.", "noreply@corp.com"),
    ("Re: Dataiku application - [#REQ-118600]",
     "We have received your application.", "hr@dataiku.com"),
]
for subj, body, frm in auto_cases:
    import triage as T
    v = T.classify(subj, body, frm)
    check(v["category"] == "auto", f"auto: {subj[:48]}", v["category"])

print("\nclassify: what always is")

alert_cases = [
    ("rejection", "Re: Senior QA Engineer",
     "Thank you for your application. We have decided to move forward with "
     "other candidates at this time.", "talent@caylent.com"),
    ("rejection", "Re: Platform Engineer",
     "Unfortunately we will not be proceeding with your candidacy. We wish "
     "you the best in your search.", "hr@example.com"),
    ("action", "Re: Backend Engineer",
     "We would like to schedule a 45-minute interview next week. Please "
     "reply to confirm your availability by October 15, 2026.", "hr@example.com"),
    ("action", "Interview invitation",
     "We would love to move forward. Please complete the take-home "
     "assessment within 5 business days.", "jobs@startup.io"),
    ("action", "Re: DevOps role",
     "Could you send over a recent copy of your resume? We have a few "
     "questions about your Kubernetes experience.", "recruiting@example.com"),
]
for want, subj, body, frm in alert_cases:
    v = T.classify(subj, body, frm)
    check(v["category"] == want, f"{want}: {subj[:44]}", v["category"])

print("\nclassify: a person writing with no ask is not an alert")
v = T.classify("Re: DevOps role",
               "Hi Jordan, just confirming we received your application. "
               "Nothing needed from you right now.", "jane@corp.com")
check(v["category"] == "human", "no-ask human reply is 'human'", v["category"])

print("\nclassify: an interview invite must not be misfiled as a rejection")
v = T.classify("Re: Role",
               "We enjoyed our conversation and would like to schedule the "
               "second round. Please reply with your availability.",
               "recruiter@corp.com")
check(v["category"] == "action", "interview invite is 'action'", v["category"])


# --------------------------------------------------------------------------
print("\ndeadlines: a real due date, not a date that happens to be in the text")

check(T.parse_deadline("Please reply by October 20, 2026.", datetime.datetime(2026, 10, 7)) == "2026-10-20",
      "month-name date after a trigger")
check(T.parse_deadline("Please respond by 10/12/2026.", datetime.datetime(2026, 10, 7)) == "2026-10-12",
      "MM/DD/YYYY after a trigger")
check(T.parse_deadline("Please complete the assessment within 5 business days.",
                       datetime.datetime(2026, 10, 7)) == "2026-10-14",
      "5 business days from Wed Oct 7 -> Wed Oct 14")
check(T.parse_deadline("Please complete this within 3 days.",
                       datetime.datetime(2026, 10, 7)) == "2026-10-10",
      "3 calendar days")
check(T.parse_deadline("I joined the company in March and have been here "
                       "since 10/2021.") is None,
      "a date with no due-word is not a deadline")
check(T.parse_deadline("We received your application on September 20.")
      is None, "received-on date is not a deadline")
check(T.parse_deadline("Let me know if Friday works for you.") is None,
      "a bare weekday without a due-word is not a deadline")


# --------------------------------------------------------------------------
print("\nledger: one-shot alerting")

tmp = tempfile.mkdtemp(prefix="triage-test-")
real_responses = T.RESPONSES
T.RESPONSES = os.path.join(tmp, "responses.json")
try:
    key, e = T.record(pid="rTEST1", uid=1, msgid="<m1@test>",
                      frm="hr@example.com", subject="Re: Role",
                      date="Wed, 07 Oct 2026 09:00:00 +0000",
                      body="Please reply to confirm your availability by "
                           "October 15, 2026.")
    check(e["category"] == "action", "record() classifies on write", e["category"])
    check(e["deadline"] == "2026-10-15", "record() extracts deadline", str(e["deadline"]))

    _, e2 = T.record(pid="rTEST1", uid=1, msgid="<m1@test>",
                     frm="hr@example.com", subject="Re: Role",
                     date="Wed, 07 Oct 2026 09:00:00 +0000",
                     body="Please reply to confirm your availability.")
    check(len([k for k in T.load_ledger() if not k.startswith("followup:")]) == 1,
          "re-recording the same Message-ID does not duplicate")

    pending = T.pending()
    check(len(pending) == 1, "unacked action reply is pending", str(len(pending)))

    T.ack([pending[0][0]])
    check(T.pending() == [], "acked reply leaves the pending list")
    check(len(T.load_ledger()) == 1, "acking does not delete the record")

    T.record(pid="rTEST2", uid=2, msgid="<m2@test>",
             frm="talent@corp.com", subject="Re: Role",
             date="Wed, 07 Oct 2026 10:00:00 +0000",
             body="We have decided to move forward with other candidates.")
    T.record(pid="rTEST3", uid=3, msgid="<m3@test>",
             frm="askcareers@esri.com", subject="Automatic reply: Role",
             date="Wed, 07 Oct 2026 11:00:00 +0000",
             body="This is an automated response.")
    pend = T.pending()
    check(len(pend) == 1 and pend[0][1]["category"] == "rejection",
          "rejection pending, auto-reply never pending",
          str([(k, v["category"]) for k, v in pend]))

    print("\nledger: replay only on request")
    T.ack([pend[0][0]])
    check(T.pending() == [], "nothing pending after acking")
    replay = [(k, v) for k, v in T.load_ledger().items()
              if not k.startswith("followup:")
              and v.get("category") in T.ALERT_CATEGORIES]
    check(len(replay) == 2, "--all can still see both alerts", str(len(replay)))

    print("\nledger: a person answered only if a person answered")
    check(T.human_replied("rTEST2") is True, "rejection counts as a human reply")
    check(T.human_replied("rTEST3") is False, "auto-reply does not count")
    check(T.human_replied("rTEST1") is True, "actionable reply counts")
    check(T.human_replied("rNOPE") is False, "unknown pid is not a reply")

    print("\nfollow-ups: surfaced once per milestone, not every morning")
    import json
    state = {"rSILENT": {"status": "submitted", "sent_to": "hr@x.com",
                         "updated": (datetime.datetime.now()
                                     - datetime.timedelta(days=13)).isoformat(
                                         timespec="minutes")},
             "rFRESH": {"status": "submitted", "sent_to": "hr@y.com",
                        "updated": datetime.datetime.now().isoformat(
                            timespec="minutes")},
             "rANSWERED": {"status": "submitted", "sent_to": "hr@z.com",
                           "updated": (datetime.datetime.now()
                                       - datetime.timedelta(days=20)).isoformat(
                                           timespec="minutes")},
             "rSKIPPED": {"status": "skip", "sent_to": "hr@w.com",
                          "updated": (datetime.datetime.now()
                                      - datetime.timedelta(days=30)).isoformat(
                                          timespec="minutes")}}
    real_state = T.STATE
    T.STATE = os.path.join(tmp, "state.json")
    with open(T.STATE, "w") as f:
        json.dump(state, f)
    T.record(pid="rANSWERED", uid=9, msgid="<m9@test>",
             frm="hr@z.com", subject="Re: Role",
             date="Wed, 07 Oct 2026 09:00:00 +0000",
             body="We would like to schedule a call. Please reply.")

    rows = T.followups()
    pids = [r["pid"] for r in rows]
    check("rSILENT" in pids, "13-day silence is a follow-up", str(pids))
    check("rFRESH" not in pids, "fresh application is not a follow-up")
    check("rANSWERED" not in pids, "application a person answered is not a follow-up")
    check("rSKIPPED" not in pids, "skipped application is not a follow-up")

    pend = T.pending_followups()
    check(len(pend) == 1 and pend[0]["pid"] == "rSILENT",
          "first milestone is pending", str([r["pid"] for r in pend]))
    T.ack_followups(pend)
    check(T.pending_followups() == [],
          "acked follow-up does not re-surface the next morning")
    rows = T.followups()
    check(len(rows) == 1, "it is still tracked as silent, just not nagging")

    print("\nfollow-up draft")
    d = T.followup_draft("rSILENT")
    check(d["to"] == "hr@x.com", "draft is addressed", d["to"])
    check("Following up" in d["subject"], "draft has a subject", d["subject"])
    check("13 days" in d["body"], "draft says how long it has been", d["body"][:200])

    print("\nlabels: an aggregator hostname is not the employer")
    check(T.employer_from_address("hr@launchdarkly.com") == "LaunchDarkly",
          "sender domain names the employer")
    check(T.employer_from_address("jobs@jobicy.com") is None,
          "an aggregator address does not")
    check(T.employer_from_address("someone@gmail.com") is None,
          "a personal mailbox does not")
    check("Anaplan" in T.job_label("r7e308af") or "Dataiku" in T.job_label("r7e308af"),
          "known posting resolves from the kit map", T.job_label("r7e308af"))
    check(T.job_label(None) == "(unmatched reply)", "null pid has a readable label")

finally:
    T.RESPONSES = real_responses
    if "real_state" in dir():
        T.STATE = real_state
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("all triage tests passed")
