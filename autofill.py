#!/usr/bin/env python3
"""Fill an application form in a real browser, then STOP before Submit.

Three rules govern this file.

1. It never invents an answer. A question is typed only if draft_answer()
   decided it, or if Jordan wrote it into the kit's answers.json. Anything
   else is left blank and reported, so a screening question is never answered
   with a placeholder or with cover-letter prose.

2. It never clicks Submit. Submitting stays a human action, and
   `pipeline.py submitted` stays the explicit record that it happened.

3. It never self-identifies on Jordan's behalf. Voluntary EEO/ADA questions -
   gender, race, disability, veteran status, accommodation, pronouns - are left
   blank or declined, and consent checkboxes stay unticked, on every form. A
   saved ATS profile can pre-select these; enforce_guards() undoes that at the
   end of every pass and the sweep re-checks it across all tabs.

Usage:
    python3 autofill.py <id|kit name>          # fill, screenshot, do not submit
    python3 autofill.py <target> --headed      # watch it happen
    python3 autofill.py <target> --dry-run     # report only, no browser
    python3 autofill.py <target> --submit      # deliberately NOT supported

Sweep mode - the mass version. One browser, one tab per application, all left
open for Jordan to work through:

    python3 autofill.py --sweep --dry-run      # show the queue, type nothing
    python3 autofill.py --sweep                # confirm, then open every tab
    python3 autofill.py --sweep --limit 5      # just the best 5
    python3 autofill.py --sweep --any-host     # include custom employer sites
    python3 autofill.py --sweep --stretch      # add the 354 stretch-lane jobs

Why this is a mode and not a shell loop: launch_persistent_context() takes an
exclusive lock on the profile directory, so a second concurrent invocation
fights the first for the same login. One context with N tabs is the only shape
that fills many applications under a single Greenhouse sign-in.

Needs Playwright, which lives in /home/jordan/.mempalace-venv:
    /home/jordan/.mempalace-venv/bin/python autofill.py --sweep
"""
import json
import os
import re
import sys
import time
import urllib.parse as up

import pipeline as P

POSTINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postings.json")
# Anything containing one of these is the button we must never press.
SUBMIT_WORDS = ("submit application", "submit", "send application", "finish")
# Some ATS boxes are pick-from-list autocompletes, not free text. Lever's
# location box is backed by a places database: it keeps only a value that was
# picked from their dropdown and silently clears anything typed, so no amount
# of fill() will hold. Verified - it reverts to "" on blur for every value
# tried. These are human fields by construction, not a bug to retry.
PICK_FROM_LIST = ("location-input", "address-input", "city-input")


def is_pick_from_list(el):
    try:
        blob = " ".join(filter(None, [
            el.get_attribute("class"), el.get_attribute("data-qa"),
            el.get_attribute("id"), el.get_attribute("name"),
        ])).lower()
    except Exception:
        return False
    return any(h in blob for h in PICK_FROM_LIST)


def load_answers(kit):
    """Human-written overrides from the kit. Empty string == unanswered."""
    path = os.path.join(kit, "answers.json")
    if not os.path.exists(path):
        return {}, None
    with open(path) as f:
        doc = json.load(f)
    return doc.get("answers", {}), doc.get("_style")


def decide(q, p, owner, overrides):
    """(value_to_type, status) for one question.

    status is one of:
      fill     - a decided value, safe to type
      upload   - a file field; caller attaches the resume
      human    - nobody has written this; leave it blank
    """
    lab = q["label"]
    # A written answer always wins, including over a decided draft.
    for key, val in overrides.items():
        if key.strip().lower() == lab.strip().lower() and str(val).strip():
            return str(val).strip(), "fill"
    ans, kind = P.draft_answer(q, p, owner)
    if P.needs_human(ans, kind):
        return None, "human"
    ans = str(ans).strip()
    if q["type"] == "input_file":
        return None, "upload"
    if ans.lower().startswith("(upload file)"):
        return None, "upload"
    return ans, "fill"


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def toks(s):
    return {w for w in norm(s).split() if len(w) > 2}


def live_index(pg):
    """normalised label -> visible elements, read from the live DOM.

    Greenhouse is the reason this exists. Its public API reports field `name`
    attributes that do not exist in the rendered form ([name=phone] matches
    nothing; the file inputs only carry id=resume / id=cover_letter), and its
    API labels disagree with the page ("Resume/CV" vs "Attach"). So the page is
    the only trustworthy source of what there is to fill.
    """
    idx = {}
    loc = pg.locator("input, select, textarea")
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            lab = P._field_label(pg, el)
        except Exception:
            continue
        if lab:
            # keep the raw label too: the extra-fields pass needs the original
            # wording to look up a canonical answer, not the normalised form.
            idx.setdefault(norm(lab), []).append((lab, el))
    return idx


def find_el(pg, q, idx, file_seen, value=None):
    """Resolve one question to its live element.

    Cascade: name -> id -> exact label -> token overlap -> options (for grouped
    checkbox/radio questions) -> document-order fallback for file fields (the
    Nth file question is the Nth file input).

    The options step exists because a grouped question's label is the FIELDSET
    text ("Gender") while every live input is labelled with its own choice
    ("Male", "Female", ...). Without it a perfectly good grouped question
    resolves to nothing.
    """
    name, lab = q.get("name"), q.get("label") or ""
    for sel in ('[name="%s"]' % name, '[id="%s"]' % name) if name else ():
        loc = pg.locator(sel)
        if loc.count():
            return loc.first
    key = norm(lab)
    if key in idx:
        return idx[key][0][1]
    want = toks(lab)
    if want:
        best, score = None, 0.0
        for k, els in idx.items():
            have = toks(k)
            if not have:
                continue
            s = len(want & have) / float(len(want))
            if s > score:
                best, score = els[0][1], s
        if score >= 0.6:
            return best
    # Grouped checkbox/radio: the group's choices are the live labels, so match
    # the drafted answer against the question's own option list and return that
    # input. The caller clicks it.
    # Grouped checkbox/radio: the group's fieldset text ("Gender") is the
    # question label, but every live input is labelled with its own choice
    # ("Male", "Female", ...), so resolve through the option list instead.
    GROUPED = ("multi_value_select", "multi_value_single_select")
    if value and q.get("options") and q.get("type") in GROUPED:
        want_opt = P._pick(q["options"], [value])
        els = idx.get(norm(want_opt)) if want_opt else None
        if els:
            return els[0][1]
        # Deliberately return nothing rather than tick a different member of
        # the group. Falling back to the first option made this tick "Male" for
        # Gender and "Hispanic or Latino" for Race when the drafted decline
        # option could not be located - an assertion about Jordan that must
        # never happen quietly. A loud "[no node]" is the correct outcome.
        return None
    if value and q.get("options"):
        want_opt = P._pick(q["options"], [value])
        if want_opt:
            els = idx.get(norm(want_opt))
            if els:
                return els[0][1]
    if q["type"] == "input_file":
        # Only guess by document order when the question carries no stable
        # identifier. Greenhouse drops a file input from the DOM as soon as the
        # text fields above it are typed into, so a question that DID have
        # name="resume" would resolve to 0 here and then match the *cover
        # letter* input by position - silently attaching the resume to the
        # cover letter slot. A named field that cannot be found is reported
        # instead of guessed.
        if name:
            return None
        files = pg.locator('input[type=file]')
        n = file_seen[0]
        file_seen[0] += 1
        if n < files.count():
            return files.nth(n)
    return None


def set_value(pg, el, q, value):
    """Type/select one field, choosing the right mechanism for its control."""
    tag = (el.evaluate("e => e.tagName") or "").lower()
    if tag == "select":
        opts = el.evaluate("e => Array.from(e.options).map(o => o.textContent)") or []
        for o in opts:
            if o.strip().lower() == value.strip().lower():
                el.select_option(label=o.strip())
                return "select"
        # drafted answer may not exist as an option; take the first real choice
        for o in opts:
            if o.strip() and o.strip().lower() not in ("select ...", "select", "choose an option"):
                el.select_option(label=o.strip())
                return "select~" + o.strip()[:28]
        return "select?EMPTY"
    if q["type"] == "input_checkbox" or tag == "input" and \
            (el.get_attribute("type") or "") == "checkbox":
        want = value.strip().lower() in ("yes", "true", "1", "required")
        if el.is_checked() != want:
            el.click()
        return "check"
    # A grouped question resolved to one of its own radio/checkbox inputs.
    # There is nothing to type - the drafted answer chose WHICH input, so tick it.
    #
    # Ashby reuses ONE generated name (regenerated per page load) across its
    # Gender, Race and Veteran Status fieldsets, so the browser treats those
    # groups as a single radio group and a real click unchecks the previous
    # answer. Assigning .checked in JS skips native exclusivity, and the
    # dispatched input/change events are what React actually listens for.
    if tag == "input" and (el.get_attribute("type") or "") in ("radio", "checkbox"):
        if not el.is_checked():
            el.evaluate("""e => {
                e.checked = true;
                for (const t of ['input', 'change']) {
                    e.dispatchEvent(new Event(t, { bubbles: true }));
                } }""")
            pg.wait_for_timeout(120)
            if not el.is_checked():      # framework re-rendered and reverted it
                el.click(force=True)
        return "choice"
    # Greenhouse's custom comboboxes (role=combobox, class=select__input) open a
    # suggestion listbox on focus. If the previous field left that list open, the
    # list overlays this input and fill() times out waiting for it to be
    # actionable - which is exactly what happened on "Are you Hispanic/Latino?".
    # Dismiss any open list, then fall back to a forced fill.
    try:
        el.fill(value, timeout=4000)
        return "type"
    except Exception:
        pass
    try:
        pg.keyboard.press("Escape")
        el.evaluate("e => e.blur && e.blur()")
        el.fill(value, timeout=4000)
        return "type!"
    except Exception:
        pass
    el.fill(value, timeout=6000, force=True)
    return "type!!"


def read_back(pg, el, q):
    """What is actually in the box right now ('' if the ATS cleared it)."""
    try:
        tag = (el.evaluate("e => e.tagName") or "").lower()
        if tag == "select":
            return (el.evaluate("e => e.selectedOptions[0] ? e.selectedOptions[0].textContent : ''")
                    or "").strip()
        if (el.get_attribute("type") or "") == "file":
            return (el.evaluate("e => e.files.length ? e.files[0].name : ''") or "").strip()
        return (el.input_value() or "").strip()
    except Exception:
        return ""


# Persistent Chromium profile. Must live under the snap's own tree: snap
# chromium has no AppArmor rule for ~/.remote_job_pipeline and dies with
# "Failed to create .../SingletonLock: Permission denied" on a custom dir.
GH_PROFILE = os.path.expanduser("~/snap/chromium/common/remote_job_pipeline-gh")
# Aggregator job boards, mirroring the list in resolve_ATS.py. A posting from
# one of these only has a real form once resolve_ATS.py has matched it to the
# employer's own board and written a DIFFERENT apply_url.
AGG_BOARDS = {"jobicy", "workingnomads", "remoteok", "remotive", "weworkremotely"}
# netloc forms of the above, for the in-page check after navigation.
AGG_HOSTS = {b + ".com" for b in AGG_BOARDS} | {"www." + b + ".com" for b in AGG_BOARDS}
# Boards autofill.py knows how to fill: public question API or a rendered form,
# plus greenhouse-specific gate clicks and self-identification handling.
KNOWN_ATS = {"greenhouse", "ashby", "lever"}

# Mirrors the guard in pipeline.draft_answer - kept in sync deliberately rather
# than imported, because there it classifies a question and here it has to undo
# a pre-ticked box that some third party filled in.
CONSENT_RE = re.compile(
    r"\b(consent|agree|terms|privacy|policy|attest|certif|"
    r"electronic signature|signature)\b", re.I)


def gh_autofill(pg, ats, enabled=True):
    """Let Greenhouse's own 'Autofill my application' fill the form first.

    Jordan's saved Greenhouse profile is authoritative for identity and contact
    details, which is better than our guesses. Returns a short report so the
    run log shows what Greenhouse did versus what we did.
    """
    if not enabled or ats != "greenhouse":
        return "skipped"
    btn = None
    for sel in ("text=Autofill my application", "text=Autofill My Application",
                "button:has-text('Autofill')"):
        try:
            cand = pg.locator(sel).first
            if cand.count() and cand.is_visible():
                btn = cand
                break
        except Exception:
            continue
    if btn is None:
        return "no button"
    before = pg.eval_on_selector_all(
        "input, select, textarea",
        "els => els.map(e => [e.name||e.id||'', e.type, e.checked, e.value])")
    try:
        btn.click(timeout=8000)
    except Exception as ex:
        return f"click failed: {ex}"
    # Greenhouse fills asynchronously; wait for values to stop changing.
    prev, stable, waited = None, 0, 0
    while waited < 12000:
        pg.wait_for_timeout(400)
        waited += 400
        now = pg.eval_on_selector_all(
            "input, select, textarea",
            "els => els.map(e => [e.value, e.checked])")
        if now == prev:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        prev = now
    after = pg.eval_on_selector_all(
        "input, select, textarea",
        "els => els.map(e => [e.name||e.id||'', e.type, e.checked, e.value])")
    changed = sum(1 for a, b in zip(before, after) if a[3] != b[3] or a[2] != b[2])
    return f"filled {changed} field(s)" if changed else "signed in? nothing filled"


DECLINE_PHRASES = ["decline to self-identify", "decline to self identify",
                   "prefer not to say", "i don't wish to answer",
                   "i do not wish to answer", "not to self-identify"]
# Shorter tokens for matching a radio's VALUE attribute. A form's value is
# usually "declined" or "decline", never the full sentence, so the long phrases
# above never match it and the decline option gets missed.
DECLINE_VALUES = ("decline", "declined", "prefer_not", "prefer not",
                  "self_identify", "self-identify", "not_disclose",
                  "not disclose", "i decline", "no_answer", "n/a")


def deliberate_labels(overrides=None):
    """Which questions Jordan answered on purpose. Not the guard's to touch.

    The point of enforce_guards is to stop the PIPELINE asserting something
    about Jordan - a regex guessing at a medical or demographic answer. It is
    not there to overrule Jordan. Those are different acts and only one of them
    is a bug.

    Returns (screening_keys, override_keys) rather than a flat set, because the
    two sources are matched by DIFFERENT rules and the guard has to reproduce
    them exactly or the two halves of the pipeline disagree about the same
    question:

      * screening.json keys match by substring, exactly as match_screening()
        does (pipeline.py:1367). A bare key like "accommodation" is meant to
        cover "Do you require a reasonable accommodation?", so an exact-match
        set here would let draft_answer() type Jordan's answer and then have
        the guard delete it - the exact failure this was written to prevent.
      * a kit's answers.json matches by exact label, exactly as decide() does.

    Anything that does not match here is the pipeline's own output, and the
    guard is free to clear it.
    """
    screen_keys = []
    for k, v in P.screening_answers()["answers"]:
        v = str(v).strip()
        # __decline__ is Jordan saying DECLINE, which is exactly what the guard
        # enforces - it is not permission to leave the field alone. Including
        # it here disabled the safety net: the guard skipped gender/veteran
        # fields as "deliberate", and a mis-tick then survived, because
        # screening.json records a deliberate __decline__ for them. Only a real
        # answer counts as something the guard must not touch.
        if v and v != P.DECLINE_SENTINEL:
            screen_keys.append(k)
    override_keys = [k for k, v in (overrides or {}).items() if str(v).strip()]
    return screen_keys, override_keys


def is_deliberate(rawlab, deliberate):
    """True when Jordan personally answered this question."""
    if not deliberate:
        return False
    screen_keys, override_keys = deliberate
    lab = P._norm_label(rawlab)
    if not lab:
        return False
    for key in override_keys:                 # exact, as decide() does
        if key.strip().lower() == (rawlab or "").strip().lower():
            return True
    for key in screen_keys:                   # substring, as match_screening() does
        k = P._norm_label(key)
        if k and k in lab:
            return True
    return False


def enforce_guards(pg, idx, deliberate=None):
    """Re-assert the two rules a third-party autofill can silently break.

    A saved Greenhouse profile can pre-select an EEO answer and pre-tick a
    consent box. Both are things Jordan decided must never be agreed on his
    behalf, so after any third-party fill we put them back - regardless of what
    the profile says. Returns (eeo_reset, consent_unticked).
    """
    forced = unticked = 0
    eeo_seen = declined = consent_seen = consent_clear = 0
    honoured = 0
    for _norm, entries in idx.items():
        for rawlab, el in entries:
            try:
                tag = (el.evaluate("e => e.tagName") or "").lower()
                etype = (el.get_attribute("type") or "").lower()

                if etype == "checkbox" and CONSENT_RE.search(rawlab):
                    consent_seen += 1
                    if el.is_checked():
                        el.uncheck()
                        unticked += 1
                    else:
                        consent_clear += 1
                    continue

                # For a radio or checkbox the question usually lives in the
                # group's fieldset/legend, not on the input. _field_label() of a
                # radio in a <fieldset> returns just the option text - "Yes" -
                # so the EEO test below saw "Yes" instead of "Are you
                # transgender?", decided the group was not EEO, and left a
                # pre-ticked affirmative in place. Fold the group question in
                # so the group is recognised no matter how the form is marked
                # up. Deliberate matching is checked on the same combined
                # text, so a real answer is still honoured.
                grp = ""
                if etype in ("radio", "checkbox"):
                    try:
                        grp = P._group_label(el) or ""
                    except Exception:
                        grp = ""
                eeo_text = (rawlab + " " + grp).strip()
                eeo_text = re.sub(r"\s+", " ", eeo_text)

                if not P._eeo_self_id(eeo_text):
                    continue
                # Jordan answered this one himself. Leave it exactly as written
                # and do not even count it: it is not something the pipeline
                # asserted, so it is not a thing the guard failed to catch.
                if is_deliberate(eeo_text, deliberate) or is_deliberate(rawlab, deliberate):
                    honoured += 1
                    continue
                eeo_seen += 1

                if tag == "select":
                    opts = el.evaluate(
                        "e => Array.from(e.options).map(o => o.textContent)") or []
                    got = P._pick(opts, DECLINE_PHRASES)
                    if got:
                        cur = el.evaluate(
                            "e => e.selectedOptions[0] ? e.selectedOptions[0].textContent : ''")
                        if (cur or "").strip().lower() != got.strip().lower():
                            el.select_option(label=got.strip())
                            forced += 1
                        declined += 1
                elif etype == "radio":
                    name = el.get_attribute("name")
                    if not name:
                        continue
                    # Match the decline option on text that belongs to THIS one
                    # radio, never on _field_label(). For a radio inside a
                    # <label> or <fieldset> that wraps the whole question,
                    # _field_label() returns the complete option list for every
                    # radio in the group, so a decline-phrase test against it
                    # matched the FIRST option - "Yes" - and clicking that
                    # reported a correction while having answered the exact
                    # opposite: "Yes, I am transgender".
                    #
                    # e.labels[0] is no safer: when the markup wraps the whole
                    # question in one <label>, the first radio's own label IS
                    # that wrapper, so its text lists every option. So the
                    # value attribute leads, then aria-label, and the radio's
                    # own label text is used only when it is short enough to be
                    # one option rather than the whole question. Anything that
                    # fails all three is left unclaimed, which surfaces as
                    # unresolved instead of guessing.
                    target = None
                    try:
                        for sib in pg.locator(f'input[type=radio][name="{name}"]').all():
                            val = (sib.get_attribute("value") or "").strip().lower()
                            aria = (sib.get_attribute("aria-label") or "").strip()
                            if any(t in val for t in DECLINE_VALUES) or \
                               any(p in aria.lower() for p in DECLINE_PHRASES):
                                target = sib
                                break
                        if target is None:
                            for sib in pg.locator(f'input[type=radio][name="{name}"]').all():
                                own = (sib.evaluate(
                                    "e => (e.labels && e.labels.length) ? "
                                    "(e.labels[0].textContent || '') : ''") or "").strip()
                                if 0 < len(own) <= 40 and \
                                   any(p in own.lower() for p in DECLINE_PHRASES):
                                    target = sib
                                    break
                    except Exception:
                        continue
                    if target is None:
                        # No decline option on this form. Say nothing and count
                        # nothing: the field stays blank, and because it was
                        # seen but not declined it surfaces as UNRESOLVED and
                        # stops the sweep. Reporting it as clean would be a
                        # lie - there is no safe automated answer to a strict
                        # Yes/No protected-characteristic question.
                        continue
                    was_checked = target.is_checked()
                    if not was_checked:
                        try:
                            target.click(force=True)
                            pg.wait_for_timeout(200)
                        except Exception:
                            continue
                    # Verify. A styled radio is often covered by its label, and
                    # a click that silently missed must not be counted as a
                    # decline - that is how a real mis-fill survives a run that
                    # says everything is fine.
                    if target.is_checked():
                        declined += 1
                        if not was_checked:
                            forced += 1
                elif etype == "checkbox":
                    # A ticked EEO/ADA box is a self-identification made on
                    # Jordan's behalf - "yes, I need an accommodation" is a
                    # medical status, not a preference. Two bugs lived here:
                    # input_value() returns the value ATTRIBUTE ("yes") whether
                    # or not the box is ticked, so the old check read "yes" off
                    # a perfectly blank form and reported it as an answer; and
                    # nothing ever unticked it, so a saved profile that
                    # pre-selected accommodation was COUNTED as a problem and
                    # then left in place. is_checked() is the only honest test.
                    # Blank is the correct end state - there is no "decline"
                    # option on an accommodation checkbox, so leaving it alone
                    # IS declining. Same stance as the consent boxes above,
                    # which were always repaired rather than preserved.
                    if el.is_checked():
                        el.uncheck()
                        forced += 1
                    declined += 1
                else:
                    # Free text. Deliberately NOT cleared: this may be a value
                    # Jordan typed himself on a half-finished form, and
                    # destroying his own words to satisfy a counter would be a
                    # worse failure than the one it prevents. Reported instead,
                    # and the sweep refuses to hand over a tab that still has
                    # one (see the eeo_unresolved check in sweep()).
                    cur = (el.input_value() or "").strip()
                    if not cur or any(p in cur.lower() for p in DECLINE_PHRASES):
                        declined += 1
            except Exception:
                continue
    return (forced, unticked, eeo_seen, declined, consent_seen, consent_clear,
            honoured)


def gh_login():
    """One-time interactive sign-in so the profile persists for later runs."""
    from playwright.sync_api import sync_playwright
    os.makedirs(GH_PROFILE, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            GH_PROFILE,
            executable_path=P.CHROME_CANDIDATES[0] if P.CHROME_CANDIDATES else None,
            headless=False, args=["--no-sandbox"])
        pg = ctx.new_page() if not ctx.pages else ctx.pages[0]
        pg.goto("https://my.greenhouse.io/", wait_until="domcontentloaded", timeout=45000)
        print("\n  Sign in to Greenhouse in this Chromium window, then close it.")
        try:
            while ctx.pages:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        ctx.close()
    print("  saved to", GH_PROFILE)
    return 0


def attached_docs(pg):
    """Filenames the form is already showing as attached documents."""
    try:
        txt = pg.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return []
    hits = []
    for line in txt.splitlines():
        s = line.strip()
        if re.search(r"\.(pdf|docx?|rtf|txt)\b", s, re.I) and len(s) < 80:
            if s not in hits:
                hits.append(s)
    return hits


def upload_target(q, kit, resume):
    """Which local file belongs in this file field.

    Sending the resume to every file field put the resume PDF into the cover
    letter slot, which is exactly the kind of quiet mistake that reaches a
    recruiter. Cover letter fields get the kit's own cover_letter.* when one
    exists, and are otherwise left for Jordan.
    """
    blob = (q.get("label") or "") + " " + (q.get("name") or "")
    if re.search(r"cover", blob, re.I):
        for ext in (".txt", ".pdf", ".docx", ".doc", ".rtf", ".md"):
            cand = os.path.join(kit, "cover_letter" + ext)
            if os.path.exists(cand):
                return cand
        return None                      # no letter on file - Jordan writes it
    return resume


def fill_one(pg, p, owner, kit, plan, overrides, shot_name="autofill.png"):
    """Fill ONE already-navigated application page. Never submits.

    Extracted from main() unchanged so that sweep mode drives every tab through
    exactly the same guarded path as a single run. If the two ever diverge, the
    safety rules below stop being true of every application in the sweep, which
    is the one property this file exists to guarantee.

    Returns a dict of per-application counters for the caller's summary.
    """
    url = P.req_url(p)
    ats = P.ats_board_jid(url)[0]
    resume = owner.get("resume_path")
    filled = skipped = 0
    runtime_human = []
    extra_filled = []
    # Navigate here rather than in the caller: sweep mode hands fill_one a
    # freshly-opened tab per application, so the page it is given is always
    # about:blank and the goto has to be part of the unit of work.
    pg.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Last line of defence against typing into a job board's search page. The
    # queue is already filtered for this, but single mode can be pointed at any
    # id, and a listing page has text inputs that would swallow a resume and a
    # cover letter without complaint. Refuse before a single keystroke.
    landed = (up.urlparse(pg.url).hostname or "").lower()   # hostname, not netloc:
    # keeps the :port, which would make the comparison miss.
    if landed in AGG_HOSTS:
        print(f"    [REFUSED] {landed} is a job listing, not an application form.")
        print("              Refusing to type into it. Resolve the employer's")
        print("              real board first:  resolve_ATS.py --buckets target,watch")
        return {"filled": 0, "skipped": 0, "runtime_human": [], "lost": [],
                "left": [], "shot": None, "extra_filled": [], "guards": None,
                "url": url, "ats": ats, "refused": landed}
    for text in P.GATE_TEXT.get(ats, ()):
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
        pass
    pg.wait_for_timeout(1200)

    # Documents go first, for two independent reasons. Typing into the text
    # fields makes Greenhouse drop the file inputs from the DOM, so a cover
    # letter uploaded after the resume has nowhere to go. And Greenhouse's
    # own autofill attaches the resume from Jordan's saved profile and then
    # removes the input, which would make our document unattachable - so
    # whichever file lands first is the file that gets sent.
    idx = live_index(pg)
    file_seen = [0]
    uploads = [x for x in plan if x[2] == "upload"]
    for q, _val, how in uploads:
        target = upload_target(q, kit, resume)
        if target is None:
            print(f"    [human]  {q['label'][:50]}  (no cover letter on file - "
                  f"write one in the kit, then re-run)")
            runtime_human.append((q["label"], "cover letter not written yet"))
            skipped += 1
            continue
        el = find_el(pg, q, idx, file_seen, None)
        if el is None:
            shown = attached_docs(pg)
            if shown:
                print(f"    [ok]     {q['label'][:40]}  already attached: "
                      f"{', '.join(shown[:2])}")
                filled += 1
            else:
                print(f"    [no node] {q['label'][:56]}")
                skipped += 1
            continue
        if not os.path.exists(target):
            print(f"    [missing] {q['label'][:40]}  {target}")
            skipped += 1
            continue
        try:
            el.set_input_files(target)
            print(f"    [file]   {q['label'][:56]}  <- {os.path.basename(target)}")
            filled += 1
        except Exception as ex:
            print(f"    [upload failed] {q['label'][:40]}  {str(ex)[:50]}")
            skipped += 1
        pg.wait_for_timeout(400)
    if uploads:
        pg.wait_for_timeout(1200)
        idx = live_index(pg)

    for q, val, how in plan:
        if how in ("human", "upload"):
            continue
        el = find_el(pg, q, idx, file_seen, val)
        if el is None:
            print(f"    [no node] {q['label'][:56]}")
            skipped += 1
            continue
        if is_pick_from_list(el):
            print(f"    [human]  {q['label'][:50]}  (autocomplete - pick from its list)")
            runtime_human.append((q["label"], "dropdown, not free text"))
            continue
        try:
            how2 = set_value(pg, el, q, val)
            if how2 == "choice":
                # Ashby shares one generated `name` across its Gender,
                # Race and Veteran Status fieldsets, so the groups are
                # mutually exclusive in the DOM and React re-renders
                # all but the last one back to empty. Verify after the
                # re-render settles and hand any that reverted back to
                # Jordan rather than reporting a false success.
                pg.wait_for_timeout(700)
                if not el.is_checked():
                    print(f"    [human]  {q['label'][:44]}  "
                          f"(form reverted it - tick this one yourself)")
                    runtime_human.append(
                        (q["label"], "radio group reverted; tick manually"))
                    continue
            print(f"    [{how2:6}] {q['label'][:56]}")
            filled += 1
        except Exception as e:
            print(f"    [FAIL]   {q['label'][:48]}  {type(e).__name__}")
            skipped += 1

    # Fields the API never mentioned. Greenhouse's public questions API
    # omits its entire voluntary self-identification block (gender, race,
    # veteran status, disability, sexual orientation, transgender) - those
    # inputs exist only in the rendered DOM. Anything visible on the page
    # with no plan entry gets a question synthesised from its live label and
    # run through the same canonical-answer lookup, so answering once in
    # screening.json covers them too.
    planned = {norm(q["label"]) for q, _, _ in plan}
    for key, els in idx.items():
        if key in planned or not els:
            continue
        raw, el = els[0]
        if is_pick_from_list(el):
            continue
        try:
            extra = {"label": raw, "name": None, "required": False,
                     "options": P.dom_options(el),
                     "type": P.TYPE_MAP.get(P.dom_type(el), "input_text")}
        except Exception:
            continue
        if extra["type"] == "input_file":
            continue                      # never guess at a second upload
        if extra["type"] in ("input_radio", "radio", "multi_value_select"):
            # Never guess at a radio group whose question was not in the plan.
            # live_index keys a wrapped group by question PLUS every option
            # ("are you transgender yes no decline to self identify"), which
            # never matches the plan's question key, so an EEO group lands here
            # and gets re-decided from a string that contains the word "Yes".
            # The answer to a radio group depends on the question, not on the
            # options, and a wrong guess on one of these is a misstatement
            # about a protected characteristic. enforce_guards() still sets the
            # decline option; it just does not have to fight this pass first.
            continue
        val, how = decide(extra, p, owner, overrides)
        if how == "human" or not val:
            continue
        try:
            how2 = set_value(pg, el, extra, val)
            print(f"    [{how2:6}] {raw[:56]}  (unlisted field)")
            filled += 1
            extra_filled.append((raw, val))
        except Exception as e:
            print(f"    [FAIL]   {raw[:48]}  {type(e).__name__}  (unlisted field)")
            skipped += 1

    shot = os.path.join(kit, shot_name)
    # Verify pass. Some ATS fields reject a value they do not recognise and
    # silently clear themselves on blur - Lever wiped "Current location"
    # the moment a later field took focus. Filling blind would report
    # success on a box that is actually empty, so re-read every field,
    # repair once, and report whatever still will not hold a value.
    lost = []
    spare = [0]
    for q, val, how in plan:
        if how != "fill" or not val:
            continue
        el = find_el(pg, q, idx, spare)
        if el is None:
            continue
        got = read_back(pg, el, q)
        if got and got.lower() != val.lower():
            continue                      # select fell back to a real option
        if got.lower() == val.lower():
            continue
        try:
            set_value(pg, el, q, val)
            pg.wait_for_timeout(150)
        except Exception:
            pass
        still = read_back(pg, el, q)
        if still.lower() != val.lower():
            lost.append((q["label"], val, still))
    # Belt and braces: make sure we are sitting on a filled form, not a
    # confirmation page, and that the submit button was left alone.
    left = [w for w in SUBMIT_WORDS
            if any(w in (pg.locator("button").nth(i).inner_text() or "").lower()
                   for i in range(pg.locator("button").count()))]
    # Last line of defence: whatever filled the form, our two rules hold.
    # Always reported, so every run states its own safety position rather
    # than leaving it to be spot-checked.
    g = enforce_guards(pg, live_index(pg), deliberate_labels(overrides))
    forced, unticked, eeo_seen, declined, consent_seen, consent_clear, honoured = g
    print(f"  EEO: {declined}/{eeo_seen} declined or left blank"
          + (f" ({forced} corrected)" if forced else ""))
    if honoured:
        print(f"  {honoured} EEO answer(s) are your own recorded answers - left as written")
    print(f"  consent boxes: {consent_clear}/{consent_seen} unticked"
          + (f" ({unticked} were ticked and got cleared)" if unticked else ""))
    try:
        pg.screenshot(path=shot, full_page=False)
    except Exception:
        pass
    return {"filled": filled, "skipped": skipped, "runtime_human": runtime_human,
            "lost": lost, "left": left, "shot": shot, "extra_filled": extra_filled,
            "guards": g, "url": url, "ats": ats}


def ensure_questions(p, owner):
    """Make sure this posting has a questions.json, fetching it if needed.

    Deliberately NOT pipeline.py's cmd_stage(). Stage shells out to `xdg-open`,
    which would spawn a fresh uncontrolled browser per posting and hand Jordan
    N windows that are not the tabs we are about to fill. It also writes
    state.json. All sweep needs is the cached question list, so this calls the
    same two fetchers stage uses and nothing else.
    """
    kit = P.kit_dir(p, owner)
    qpath = os.path.join(kit, "questions.json")
    if os.path.exists(qpath):
        with open(qpath) as f:
            return json.load(f)
    # Pin the kit name first: fetch_questions/render_form cache to
    # kit_dir(p), which resolves independently of the owner we were handed.
    # Without a pinned name the cache can land in a different directory than
    # the one we are about to read, and the fetch looks like it did nothing.
    if not p.get("kit_name"):
        p["kit_name"] = os.path.basename(kit)
    board, _jid = P.gh_board_jid(p)
    if board:
        qs = P.fetch_questions(p)          # Greenhouse has a public API
    else:
        qs = P.render_form(p)              # Ashby / Lever: must be rendered
    if not qs:
        return None
    try:
        os.makedirs(kit, exist_ok=True)
        with open(qpath, "w") as f:
            json.dump(qs, f, indent=2)
    except OSError:
        pass
    return qs


def prepare_one(data, owner, pid, verbose=True):
    """Resolve one posting into everything fill_one needs, or None if unready.

    Returns None (after printing why) rather than raising, so a sweep can skip
    the handful of un-staged or board-less postings and still do the rest
    instead of aborting the whole run on the first bad id.
    """
    p = next((x for x in data["postings"] if x["id"] == pid), None)
    if p is None:
        print(f"  unknown id {pid}")
        return None
    kit = P.kit_dir(p, owner)
    url = P.req_url(p)
    if not url:
        print(f"  {p['employer']} - {p['title'][:40]}: no application URL")
        return None
    ats = P.ats_board_jid(url)[0]
    qpath = os.path.join(kit, "questions.json")
    if os.path.exists(qpath):
        with open(qpath) as f:
            questions = json.load(f)
    else:
        questions = ensure_questions(p, owner)
    if not questions:
        print(f"  {p['employer']} - {p['title'][:40]}: no form found "
              f"(needs a manual apply)")
        return None
    overrides, style = load_answers(kit)
    plan = []
    for q in questions:
        val, how = decide(q, p, owner, overrides)
        plan.append((q, val, how))
    todo = [x for x in plan if x[2] == "fill"]
    ups = [x for x in plan if x[2] == "upload"]
    humans = [x for x in plan if x[2] == "human"]
    if verbose:
        print(f"\n{p['employer']} - {p['title']}")
        print(f"  {ats}  {url}")
        print(f"  {len(todo)} to fill, {len(ups)} file upload, {len(humans)} need you")
        if humans:
            print("\n  NOT ANSWERED - left blank on purpose:")
            for q, _, _ in humans:
                print(f"    - {q['label'][:66]}")
            print(f"\n  write them in: {os.path.join(kit, 'answers.json')}")
            if style:
                print(f"  style: {style[:150]}")
    return {"p": p, "pid": pid, "kit": kit, "url": url, "ats": ats, "plan": plan,
            "humans": humans, "todo": todo, "ups": ups, "style": style,
            "overrides": overrides}


def is_listing_page(p):
    """True when this posting has NO application form - only a job listing.

    resolve_ATS.py sets apply_url == url when it could not match the posting to
    a real board job, so req_url() hands back the aggregator's listing page.
    Those pages have search boxes, filters and newsletter forms but no apply
    form: running the fill pass against one would type Jordan's details into a
    site search box. Unconditionally un-sweepable, in both single and sweep mode.
    """
    if (p.get("board") or "").lower() not in AGG_BOARDS:
        return False
    apply_url = p.get("apply_url") or ""
    return not apply_url or apply_url == p.get("url")


def host_of(p):
    import urllib.parse
    return (urllib.parse.urlparse(P.req_url(p)).hostname or "").lower()


def sweepable(data, owner, limit=0, include_stretch=False, any_host=False):
    """Postings worth opening a tab for, best first.

    target first, then watch, then stretch only if asked - stretch is 354
    postings and Jordan chose to keep it out of the default sweep.

    Statuses: 'staged' is explicitly INCLUDED - that is the normal state of a
    prepared-but-unsent application, i.e. exactly what wants filling. Only
    terminal states (submitted, responded, closed, skip) are excluded.
    questions.json is NOT required here; ensure_questions() fetches it during
    preparation, so an unstaged-but-real posting still gets a tab.

    By default only boards we have a proven fill path for (Greenhouse, Ashby,
    Lever). Custom employer career sites need --any-host: their forms are
    real, but the Greenhouse-specific gate clicks and field heuristics are
    tuned for a known ATS. Aggregator listing pages are never included.
    """
    order = {"target": 0, "watch": 1, "stretch": 2}
    if not include_stretch:
        order.pop("stretch")
    st = P.load_state()
    out = []
    for p in data["postings"]:
        bucket = P.fit_bucket(p)
        if bucket not in order:
            continue
        # Pursuance gate. Jordan: a role with no pay data is his call to make,
        # never something to auto-apply to - so "unpriced" never gets a tab.
        # See pipeline.pursuit() for the automability-vs-pay table.
        pu = P.pursuit(p)
        if pu == "unpriced":
            continue
        if P.get(p, st)[0] not in ("new", "staged", "open", "rejected"):
            continue
        if is_listing_page(p):
            continue
        if not P.req_url(p):
            continue
        if not any_host and P.ats_board_jid(P.req_url(p))[0] not in KNOWN_ATS:
            continue
        pu_order = {"pursue": 0, "maybe": 1}.get(pu, 2)
        pay = P.pay_source(p)[1]
        out.append((order[bucket], pu_order, -int(pay or 0), p))
    out.sort(key=lambda t: t[:3])
    rows = [t[3] for t in out]
    return rows[:limit] if limit else rows


def sweep(args):
    """One browser, one tab per application, all left open for Jordan.

    The reason this cannot be a shell loop: launch_persistent_context takes an
    exclusive lock on the profile directory, so a second concurrent invocation
    would either fail or silently hand the same profile to two Chromiums. One
    context, N tabs is the only shape that fills many forms in one login.
    """
    limit = 0
    include_stretch = "--stretch" in args
    any_host = "--any-host" in args
    if "--limit" in args:
        i = args.index("--limit")
        try:
            limit = int(args[i + 1])
        except (IndexError, ValueError):
            print("  --limit needs a number")
            return 2
        args = args[:i] + args[i + 2:]
    if "--dry-run" in args:
        dry = True
    else:
        dry = False

    doc = json.load(open(POSTINGS))
    data = {"postings": doc["postings"]} if isinstance(doc, dict) else doc
    owner = P.load_owner(data)
    resume = owner.get("resume_path")
    if not resume or not os.path.exists(resume):
        print(f"  [warn] resume_pdf not found at {resume!r}; file fields stay empty")

    rows = sweepable(data, owner, limit, include_stretch, any_host)
    if not rows:
        print("  nothing sweepable. Stage postings first:  pipeline.py stage <id>")
        return 1
    # Report what the filters removed, so a short queue is never a mystery.
    n_all = len(sweepable(data, owner, 0, include_stretch, True))
    if n_all > len(rows):
        print(f"  {n_all - len(rows)} hidden: no proven form yet"
              + ("" if any_host else "  (add --any-host to try custom employer sites)"))
    listing = sum(1 for p in data["postings"]
                  if (p.get("board") or "").lower() in AGG_BOARDS and is_listing_page(p)
                  and P.fit_bucket(p) in ("target", "watch"))
    if listing:
        print(f"  {listing} target/watch skipped: aggregator listing page, no real form")
    # Prepare BEFORE promising a count. ensure_questions() fetches a form for
    # postings that were never staged, so the queue can shrink while it runs,
    # and a header that says "24 queued" when 22 survive is worse than no
    # header at all.
    prepped, skipped = [], []
    for p in rows:
        r = prepare_one(data, owner, p["id"])
        if r:
            prepped.append(r)
        else:
            skipped.append(p)
    if not prepped:
        print("\n  nothing could be prepared - no forms to fill")
        return 1
    if skipped:
        print(f"  ({len(skipped)} could not be prepared - no form found)")

    # How much of each form is already answered decides whether a tab is worth
    # Jordan's time. A job with 14 fields and 7 blanks is a 50% manual job, and
    # burying that in a wall of per-question output is how the 10% target gets
    # missed by accident.
    print(f"\n{len(prepped)} application(s) ready, best lane and salary first.\n"
          f"  {'employer':22} {'auto':>9}  {'needs you':>9}  title")
    for r in prepped:
        n = len(r["todo"]) + len(r["ups"])
        h = len(r["humans"])
        pct = 100 - (100 * h // n) if n else 0
        print(f"    {r['p']['employer'][:22]:22} {pct:>7}%  {str(h) + ' blank':>9}  "
              f"{r['p']['title'][:40]}")
    thin = [r for r in prepped
            if len(r["todo"]) + len(r["ups"])
            and len(r["humans"]) * 2 > len(r["todo"]) + len(r["ups"])]
    if thin:
        print(f"\n  {len(thin)} of these are more than half blank "
              f"({', '.join(r['p']['employer'] for r in thin[:6])}"
              f"{'...' if len(thin) > 6 else ''}) - expect to hand-write those.")

    if dry:
        print("\n  DRY RUN - no browser launched, nothing typed")
        return 0

    print(f"\n  Open {len(prepped)} prefilled tabs now? [y/N] ", end="")
    ans = input().strip().lower()
    if ans not in ("y", "yes"):
        print("  cancelled - nothing typed, nothing submitted")
        return 1

    from playwright.sync_api import sync_playwright
    os.makedirs(GH_PROFILE, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            GH_PROFILE,
            executable_path=P.CHROME_CANDIDATES[0] if P.CHROME_CANDIDATES else None,
            headless=False, args=["--no-sandbox"],
            viewport={"width": 1440, "height": 1000})
        for n, r in enumerate(prepped, 1):
            head = f"  [{n}/{len(prepped)}] {r['p']['employer']} - {r['p']['title'][:44]}"
            print(f"\n{'-' * 66}\n{head}\n  {r['ats']}  {r['url']}")
            try:
                # Reuse the context's own first page for the first application
                # instead of leaving a stray about:blank tab at position 0 -
                # it would otherwise sit to the left of the queue and make the
                # tab count disagree with the printed list.
                pg = ctx.pages[0] if (n == 1 and ctx.pages) else ctx.new_page()
                res = fill_one(pg, r["p"], owner, r["kit"], r["plan"],
                               r["overrides"], shot_name="sweep.png")
            except Exception as e:
                print(f"    [TAB FAILED] {type(e).__name__}: {str(e)[:70]}")
                results.append((r, None, f"{type(e).__name__}"))
                continue
            r.update(res)
            results.append((r, res, None))
        # Sweep-level safety assertion. The per-tab guards already ran inside
        # fill_one, but "every tab is safe" is the claim that matters, so it is
        # re-checked across every open page and reported as one number.
        # enforce_guards returns (forced, unticked, eeo_seen, declined,
        # consent_seen, consent_clear, honoured). An EEO field that is seen but
        # neither declined nor force-corrected holds an answer the pipeline put
        # there by itself, and is a hard stop: this is an unattended run and
        # nobody is watching these pages until Jordan sits down at them. Fields
        # he answered deliberately are excluded via `honoured` and reported
        # separately, so an accurate "Yes" to an accommodation question is
        # never counted against the run.
        print(f"\n{'-' * 66}\n  sweep guard check across {len(ctx.pages)} tab(s):")
        eeo_tot = eeo_bad = c_tot = c_bad = hon_tot = 0
        # Tabs were opened in queue order, so ctx.pages lines up with `results`.
        # Zip rather than assume: a refused tab still owns its page, and a
        # mismatch must not silently shift one tab's deliberate set onto
        # another tab's questions.
        for pg, entry in zip(ctx.pages, results):
            r = entry[0]
            try:
                _f, _u, seen, dec, cseen, cclear, hon = enforce_guards(
                    pg, live_index(pg), deliberate_labels(r.get("overrides")))
            except Exception:
                continue
            eeo_tot += seen
            eeo_bad += (seen - dec)
            c_tot += cseen
            c_bad += (cseen - cclear)
            hon_tot += hon
        print(f"    EEO self-identification: {eeo_tot - eeo_bad}/{eeo_tot} declined or blank"
              + (f"  <-- {eeo_bad} STILL FILLED" if eeo_bad else ""))
        print(f"    consent checkboxes:      {c_tot - c_bad}/{c_tot} unticked"
              + (f"  <-- {c_bad} STILL TICKED" if c_bad else ""))
        if hon_tot:
            print(f"    your own recorded EEO answers, left as written: {hon_tot}")
        if eeo_bad or c_bad:
            print("\n  STOP. A tab failed the safety check - do not submit anything.")
            print("  Open it, clear the flagged box, then submit that one by hand.")
        else:
            print("    all tabs clean: no EEO asserted, no consent agreed, no submit clicked")
        print("\n  Browser left OPEN. Work the tabs left to right.")
        print("  Nothing was submitted. For each one: answer the blanks, check it,")
        print("  click Submit yourself, then record it:")
        ok_tabs, bad_tabs = [], []
        for r, res, err in results:
            name = f"{r['p']['employer']} - {r['p']['title'][:36]}"
            if err:
                bad_tabs.append((name, f"tab failed: {err}"))
            elif res.get("refused"):
                bad_tabs.append((name, f"refused - {res['refused']} is a listing page"))
            else:
                ok_tabs.append(r)
        for r in ok_tabs:
            print(f"    python3 pipeline.py submitted {r['pid']}")
        if bad_tabs:
            print(f"\n  {len(bad_tabs)} tab(s) got nothing typed - do NOT submit these:")
            for name, why in bad_tabs:
                print(f"    {name}  <- {why}")
        n_lost = sum(len(r.get("lost") or []) for r, res, e in results if res)
        n_human = sum(len(res.get("runtime_human") or []) for r, res, e in results if res)
        if n_lost or n_human:
            print(f"\n  {n_lost} field(s) the form rejected and cleared on blur; "
                  f"{n_human} box(es) that will not accept typing.")
            print("  Both lists were printed per tab above - retype those by hand.")
        print(f"\n  {len(ok_tabs)} tab(s) prefilled and awaiting your review.")
        input("\n  Press Enter here once you are done - the browser will close.")
    return 0


def main():
    args = [a for a in sys.argv[1:]]
    if "--submit" in args:
        print("refusing: --submit is not supported. Submitting is a human action,\n"
              "         then run:  python3 pipeline.py submitted <id>")
        return 2
    if "--gh-login" in args:
        return gh_login()
    if "--sweep" in args:
        if "--submit" in args:
            print("refusing: --submit is not supported, in sweep or single mode.")
            return 2
        return sweep(args)
    headed = "--headed" in args
    dry = "--dry-run" in args
    gh = "--no-gh-autofill" not in args
    targets = [a for a in args if not a.startswith("--")]
    if not targets:
        print(__doc__)
        return 2

    doc = json.load(open(POSTINGS))
    data = {"postings": doc["postings"]} if isinstance(doc, dict) else doc
    owner = P.load_owner(data)
    ids = P.resolve_ids(targets, data, owner)
    if not ids:
        return 1
    if len(ids) > 1:
        print("  more than one match; name one:")
        for i in ids:
            p = next(x for x in data["postings"] if x["id"] == i)
            print(f"      {i}  {P.kit_name(p, owner)}")
        return 1
    pid = ids.pop()
    p = next(x for x in data["postings"] if x["id"] == pid)
    kit = P.kit_dir(p, owner)
    url = P.req_url(p)
    ats = P.ats_board_jid(url)[0]

    qpath = os.path.join(kit, "questions.json")
    if not os.path.exists(qpath):
        print(f"  no questions.json in {os.path.basename(kit)} - run: pipeline.py stage {pid}")
        return 1
    with open(qpath) as f:
        questions = json.load(f)
    overrides, style = load_answers(kit)

    resume = owner.get("resume_path")
    if not resume or not os.path.exists(resume):
        print(f"  [warn] resume_pdf not found at {resume!r}; file fields stay empty")

    plan = []
    for q in questions:
        val, how = decide(q, p, owner, overrides)
        plan.append((q, val, how))

    todo = [x for x in plan if x[2] == "fill"]
    ups = [x for x in plan if x[2] == "upload"]
    humans = [x for x in plan if x[2] == "human"]
    print(f"\n{p['employer']} - {p['title']}")
    print(f"  {ats}  {url}")
    print(f"  {len(todo)} to fill, {len(ups)} file upload, {len(humans)} need you")
    if humans:
        print("\n  NOT ANSWERED - left blank on purpose:")
        for q, _, _ in humans:
            print(f"    - {q['label'][:66]}")
        print(f"\n  write them in: {os.path.join(kit, 'answers.json')}")
        if style:
            print(f"  style: {style[:150]}")
    if dry:
        print("\n  DRY RUN - no browser launched, nothing typed")
        return 0

    from playwright.sync_api import sync_playwright
    os.makedirs(GH_PROFILE, exist_ok=True)
    with sync_playwright() as pw:
        # Persistent context so the one-time Greenhouse sign-in survives between
        # runs. launch() would hand a blank profile every time and force a login
        # per application.
        ctx = pw.chromium.launch_persistent_context(
            GH_PROFILE,
            executable_path=P.CHROME_CANDIDATES[0] if P.CHROME_CANDIDATES else None,
            headless=not headed, args=["--no-sandbox"],
            viewport={"width": 1440, "height": 1000})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        res = fill_one(pg, p, owner, kit, plan, overrides)
        ctx.close()

    filled, skipped = res["filled"], res["skipped"]
    runtime_human, lost, left = res["runtime_human"], res["lost"], res["left"]
    shot = res["shot"]
    print(f"\n  filled {filled}, skipped {skipped}")
    if runtime_human:
        print(f"  {len(runtime_human)} box(es) the form will not accept typing into:")
        for lab, why in runtime_human:
            print(f"    [human]  {lab[:50]}  <- {why}")
    if lost:
        print(f"  the form REJECTED {len(lost)} value(s) - it cleared them on blur:")
        for lab, want, got in lost:
            print(f"    [LOST]  {lab[:44]}")
            print(f"            wanted {want[:52]!r}, box now {got[:32]!r}")
        print("    -> type these by hand before submitting")
    print(f"  screenshot: {shot}")
    print(f"  submit button present and NOT clicked: {', '.join(left) or 'n/a'}")
    if humans:
        print(f"\n  NEXT: answer the {len(humans)} question(s) above in answers.json,")
        print(f"        re-run:  python3 autofill.py {pid}")
    print(f"\n  Review it. Click Submit yourself. Then:")
    print(f"    python3 pipeline.py submitted {pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
