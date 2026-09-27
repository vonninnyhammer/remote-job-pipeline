#!/usr/bin/env python3
"""Safety regression tests for autofill. Needs no network.

    /home/jordan/.mempalace-venv/bin/python test_safety.py

Every assertion here is about something autofill must NEVER do, because each
one was found to actually happen at least once:

  * typing into an aggregator's job LISTING page instead of an application
    form - resolve_ATS.py sets apply_url == url when it cannot find the
    employer's real board, so req_url() hands back a listing page that has
    search boxes for a resume to disappear into;
  * answering "Do you require a reasonable accommodation?" from the ordinary
    rules with "None needed." - `_eeo_self_id` matched the stem `disabilit`
    followed by a word boundary, which cannot match "disability", so the whole
    disability/ADA category was invisible and fell through to generic answers;
  * leaving a saved ATS profile's pre-ticked accommodation box in place, while
    reporting it as a problem - the guard read input_value(), which returns a
    checkbox's value ATTRIBUTE whether or not it is ticked;
  * ticking a consent box on Jordan's behalf.

A test that fails here means autofill is unsafe to run unattended, which is
exactly what --sweep does.
"""
import functools
import http.server
import os
import socketserver
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autofill as A
import pipeline as P

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(name)


# A form carrying every trap at once: pre-ticked EEO radio, pre-ticked
# accommodation box, a consent box, a file input and a real submit button.
MOCK = """<!doctype html><html><body><form>
<label>First Name<input name="first_name" type="text"></label>
<label>Last Name<input name="last_name" type="text"></label>
<label>Email<input name="email" type="email"></label>
<label>Current location<input name="location" type="text"></label>
<fieldset><legend>Gender</legend>
  <label><input type="radio" name="gender" value="female">Female</label>
  <label><input type="radio" name="gender" value="male" checked>Male</label>
  <label><input type="radio" name="gender" value="declined">Decline to self identify</label>
</fieldset>
<fieldset><legend>Are you transgender?</legend>
  <label><input type="radio" name="tg" value="yes" checked>Yes</label>
  <label><input type="radio" name="tg" value="no">No</label>
  <label><input type="radio" name="tg" value="declined">Decline to self identify</label>
</fieldset>
<fieldset><legend>Are you a protected veteran?</legend>
  <label><input type="radio" name="vet" value="yes" checked>Yes</label>
  <label><input type="radio" name="vet" value="no">No</label>
  <label><input type="radio" name="vet" value="declined">Decline to self identify</label>
</fieldset>
<label>Do you require a reasonable accommodation?
  <input type="checkbox" name="eeo_accommodation" value="yes" checked></label>
<label><input type="checkbox" name="consent" value="agree">I agree to the privacy policy</label>
<label>Resume<input type="file" name="resume"></label>
<button type="submit">Submit application</button>
</form></body></html>"""


def test_eeo_classifier():
    """No self-identification question may be invisible to _eeo_self_id."""
    print("\nEEO classifier")
    must_match = [
        "Do you require a reasonable accommodation?",
        "Do you have a disability that requires accommodation?",
        "Are you a protected veteran?", "What is your veteran status?",
        "Do you identify as a woman?", "Do you identify as a non-binary person?",
        "What is your race and ethnicity?", "What are your pronouns?",
        "Are you transgender?", "Do you have a chronic condition?",
    ]
    for lab in must_match:
        check(f"recognised: {lab[:48]}", P._eeo_self_id(lab))
    # The false-positive guard matters as much: declining a real skills
    # question would cost Jordan the role.
    must_not = [
        "Have you worked with disability management software?",
        "Describe your approach to gender-neutral design.",
        "How would you approach a team with differing experience levels?",
        "Are you willing to make reasonable accommodations for your team?",
        "How many years of experience managing men and women engineers?",
    ]
    for lab in must_not:
        check(f"not self-id: {lab[:48]}", not P._eeo_self_id(lab))
    # And the end-to-end consequence: an accommodation question must resolve
    # to a review flag, never to a typed answer.
    owner = {"name": "Jordan Guison", "email": "j@example.com",
             "phone": "5551234567", "location": "Remote", "resume_path": None}
    posting = {"id": "T", "employer": "M", "title": "T",
               "apply_url": "http://x/", "url": "http://x/"}
    for lab in ("Do you require a reasonable accommodation?",
                "Do you have a disability?"):
        ans, kind = P.draft_answer(
            {"label": lab, "type": "input_text", "options": []}, posting, owner)
        check(f"not auto-answered: {lab[:40]}",
              not (kind == "input" and ans and "none needed" in str(ans).lower()),
              f"{kind}: {str(ans)[:32]}")


def test_listing_guards():
    """An aggregator posting with no real board must never be swept."""
    print("\nAggregator guards")
    owner = {"name": "J G", "email": "j@e.com", "phone": "5",
             "location": "R", "resume_path": None}
    unresolved = {"id": "1", "employer": "Co", "title": "Support Engineer", "board": "jobicy",
                  "url": "https://jobicy.com/jobs/1", "apply_url": "https://jobicy.com/jobs/1", "lane": "Customer Support",
                  "salary_max": 100}
    check("apply_url == url is a listing page", A.is_listing_page(unresolved))
    check("missing apply_url is a listing page",
          A.is_listing_page({**unresolved, "apply_url": None}))
    resolved = {"id": "2", "employer": "Co", "title": "Support Engineer", "board": "jobicy",
                "url": "https://jobicy.com/jobs/1", "lane": "Customer Support",
                "apply_url": "https://boards.greenhouse.io/co/jobs/9",
                "salary_max": 100}
    check("resolved posting is NOT a listing", not A.is_listing_page(resolved))
    data = {"postings": [unresolved, resolved]}
    rows = A.sweepable(data, owner, any_host=True)
    ids = [p["id"] for p in rows]
    check("listing page excluded from the sweep queue", "1" not in ids, f"queue={ids}")
    check("resolved posting included", "2" in ids, f"queue={ids}")


def test_live_form():
    """Drive a real browser at a form that starts out pre-ticked."""
    print("\nLive form (real Chromium, local only)")
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testform.html")
    with open(tmp, "w") as f:
        f.write(MOCK)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=os.path.dirname(tmp))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/.testform.html"
    owner = {"name": "Jordan Guison", "email": "jordan@example.com",
             "phone": "5551234567", "location": "Remote", "resume_path": None}
    posting = {"id": "T", "employer": "MockCo", "title": "T",
               "apply_url": url, "url": url, "salary_max": 1}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIP  playwright not installed")
        return
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            A.GH_PROFILE,
            executable_path=P.CHROME_CANDIDATES[0] if P.CHROME_CANDIDATES else None,
            headless=True, args=["--no-sandbox"],
            viewport={"width": 1440, "height": 1000})
        try:
            pg = ctx.new_page()
            res = A.fill_one(pg, posting, owner, os.path.dirname(tmp), [], {})
            check("ordinary fields were filled",
                  pg.eval_on_selector('[name="first_name"]', "e => e.value") == "Jordan")
            check("accommodation carries Jordan's deliberate Yes",
                  pg.is_checked('[name="eeo_accommodation"]'),
                  "screening.json sets accommodation=Yes; the guard must not "
                  "overrule an answer he gave on purpose")
            check("gender not left as Male",
                  not pg.is_checked('[value="male"]') and not pg.is_checked('[value="female"]'))
            # The worst bug this file guards: the guard used to match its
            # "decline" option against _field_label(), which returns the whole
            # option list for every radio in a wrapped group. The first
            # candidate therefore matched, it clicked "Yes", and it counted the
            # correction - reporting a clean run while the form said "Yes, I am
            # transgender" and "Yes, I am a protected veteran". Never again.
            for group, q in (("gender", "gender"), ("tg", "transgender"),
                             ("vet", "protected veteran")):
                state = pg.eval_on_selector_all(
                    f'input[name="{group}"]', "els => els.map(e => [e.value, e.checked])")
                affirmed = [v for v, c in state if c and v in ("yes", "male", "female")]
                check(f"no affirmative answer to '{q}'", not affirmed, f"state={state}")
            check("consent box unticked", not pg.is_checked('[name="consent"]'))
            check("submit button present and not clicked",
                  pg.get_by_role("button", name="Submit application").count() == 1)
            _f, _u, seen, declined, cseen, cclear, hon = A.enforce_guards(
                pg, A.live_index(pg), A.deliberate_labels({}))
            check("guard accounts for every EEO field",
                  seen > 0 and seen - declined == 0, f"seen={seen} declined={declined}")
            check("guard accounts for every consent box", cseen - cclear == 0)

            # ---- the deliberate answer must SURVIVE the guard ----
            # Jordan answered the accommodation question on purpose. The guard's
            # job is to stop the PIPELINE asserting something, not to overrule
            # him, so an explicit Yes has to come through the whole fill path
            # intact while the demographics beside it still decline.
            f2, _u2, seen2, dec2, _cs2, _cc2, hon2 = A.enforce_guards(
                pg, A.live_index(pg), A.deliberate_labels({}))
            check("explicit accommodation answer is honoured, not overwritten",
                  hon2 >= 1, f"honoured={hon2}")
            check("demographics still declined alongside it",
                  seen2 - dec2 == 0, f"seen={seen2} declined={dec2}")
            for group in ("tg", "vet"):
                st = pg.eval_on_selector_all(
                    f'input[name="{group}"]', "els => els.map(e => [e.value, e.checked])")
                check(f"'{group}' still not affirmative after a deliberate pass",
                      not [v for v, c in st if c and v in ("yes", "male", "female")],
                      f"state={st}")

            # ...and the inverse: with NOTHING deliberate on record, a pre-ticked
            # accommodation box is exactly the thing the guard exists to undo.
            # The deliberate Yes above means the main pass no longer covers
            # this, so it gets its own case rather than silently losing it.
            pg.reload()
            pg.eval_on_selector('[name="eeo_accommodation"]', "e => e.checked = true")
            check("pre-ticked accommodation box starts ticked",
                  pg.is_checked('[name="eeo_accommodation"]'))
            _f3, _u3, _s3, _d3, _c3, _cc3, hon3 = A.enforce_guards(
                pg, A.live_index(pg), {})
            check("no deliberate answers recorded -> accommodation CLEARED",
                  hon3 == 0 and not pg.is_checked('[name="eeo_accommodation"]'),
                  f"honoured={hon3}")

            # Same page, but reached the way an aggregator posting reaches us.
            A.AGG_HOSTS = A.AGG_HOSTS | {"127.0.0.1"}
            pg2 = ctx.new_page()
            res2 = A.fill_one(pg2, posting, owner, os.path.dirname(tmp), [], {})
            typed = pg2.eval_on_selector('[name="first_name"]', "e => e.value")
            check("listing page refused", bool(res2.get("refused")), str(res2.get("refused")))
            check("nothing typed into the listing page", typed == "", repr(typed))
        finally:
            ctx.close()
    srv.shutdown()
    try:
        os.remove(tmp)
    except OSError:
        pass


if __name__ == "__main__":
    print("autofill safety tests")
    test_eeo_classifier()
    test_listing_guards()
    test_live_form()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
        sys.exit(1)
    print("all safety tests passed")
