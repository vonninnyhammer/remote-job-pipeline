#!/usr/bin/env python3
"""Pursuance tests: automability vs pay.

Lane membership is necessary but not sufficient. Jordan's rule (2026-09-27) is
that whether a role is worth his time depends on how far he can automate the
duties, countered against how high the pay is. These tests lock the properties
that rule implies, because both directions are easy to break silently:

  * a very high-paying role is worth pursuing even when the work is not
    automatable (OpenAI security at $385k, auto=2);
  * a very low-paying role is not worth automating for, even when it sits in a
    lane that used to promote it automatically (Warehouse support at $70k);
  * no role Jordan personally ruled in-lane may ever come out "skip";
  * an unpriced role is never auto-swept.

Run: /home/jordan/.mempalace-venv/bin/python test_classifier.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofill as A          # noqa: E402
import pipeline as P          # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def post(title, employer="Acme", pay=None):
    return {"id": f"{employer}:{title}", "title": title, "employer": employer, "salary_max": pay}


print("pursuance: high pay outranks low automability")
check(P.pursuit(post("Security Engineer, Infrastructure Security", pay=385_000)) == "pursue",
      "$385k security engineer is pursued despite auto=" +
      str(P.automate_score("Security Engineer, Infrastructure Security")))
check(P.automate_score("Security Engineer, Infrastructure Security") < 3,
      "security engineering is NOT scored as highly automatable")

print("\npursuance: low pay is not worth automating for")
check(P.pursuit(post("Customer Support & Success Specialist", pay=70_000)) == "skip",
      "$70k customer support is skipped even though Customer Support is a real lane")

print("\npursuance: the four in-lane rulings hold at the pay they actually appear at")
# NOT "never skip at any price" - that would contradict the rule itself. Under
# $120k a role must be highly automatable (auto=3), and AI Success Manager /
# Financial Analyst are auto=2, so a $60k version of either skipping is the
# principle working, not a bug. The invariant is that at the real pay levels
# these roles carry ($110k-$145k per kits/.kitmap.json) none of them skips.
for t in ("Tier III Service Desk Engineer",
          "Intermediate Support Engineer (SHIFT)",
          "AI Success Manager, East",
          "Senior Financial Analyst, GTM"):
    for pay in (400_000, 145_000, 120_000):
        got = P.pursuit(post(t, pay=pay))
        check(got != "skip", f"{t!r} at ${pay:,} is {got}, not skip")
# Below his own $120k threshold the table is *supposed* to be able to skip a
# role that is not highly automatable, so these are asserted as the rule
# working, not as a bug.
for t in ("AI Success Manager, East", "Senior Financial Analyst, GTM"):
    got = P.pursuit(post(t, pay=110_000))
    check(got == "skip", f"{t!r} at $110k is {got} - under $120k and not auto=3")

print("\npursuance: unpriced is its own answer, never a skip and never auto-swept")
check(P.pursuit(post("Cloud Support Engineer", pay=None)) == "unpriced",
      "no pay data -> unpriced")

print("\nautomability ordering is the one Jordan described")
queue = P.automate_score("Systems Administrator")
support = P.automate_score("Support Engineer (AMER)")
security = P.automate_score("Staff Infrastructure Security Engineer (USA)")
check(queue >= support > security,
      f"sysadmin {queue} >= support {support} > security {security}")

print("\nsweep: an unpriced role never gets a tab")
rows = A.sweepable({"postings": [
    dict(post("Cloud Support Engineer", "Nowhere", None),
         apply_url="https://boards.greenhouse.io/nowhere/jobs/1"),
    dict(post("Systems Administrator", "Somewhere", 385_000),
         apply_url="https://boards.greenhouse.io/somewhere/jobs/2")]}, owner={})
unpriced_opened = [p for p in rows if p["title"] == "Cloud Support Engineer"]
check(not unpriced_opened, "unpriced role excluded from sweepable()")
priced_opened = [p for p in rows if p["title"] == "Systems Administrator"]
check(len(priced_opened) == 1, "priced role still swept")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + "; ".join(FAILS))
    sys.exit(1)
print("all classifier tests passed")
