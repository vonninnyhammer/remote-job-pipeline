#!/usr/bin/env python3
"""Resolve aggregator postings to the employer's real ATS apply URL.

jobicy hides the outbound apply link behind React + Cloudflare Turnstile, and
exposes no redirect (/apply 301s to a blog post, /r/ 403s), so the ATS has to be
discovered independently. Board tokens are cheap to probe: Greenhouse, Lever and
Ashby all answer 200 for a real board and 404 for a fake one, and most companies
use their brand slug as the token.

Then each aggregator posting is matched to a job on that board. Matching is
title-first with a location tiebreak, and only high-confidence matches are
accepted -- a wrong apply link is worse than no apply link, because a recruiter
who clicks it sees the wrong requisition.

  python3 resolve_ats.py              # resolve every unresolved posting
  python3 resolve_ats.py --status     # report match rates, change nothing
  python3 resolve_ats.py --employer Socure
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import urlparse
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
POSTINGS = os.path.join(BASE, "postings.json")
CACHE = os.path.join(BASE, "ats_cache.json")
UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 "
      "(JordanGuison job-pipeline ATS resolver)")

ATS = {
    "greenhouse": {
        "probe": "https://boards-api.greenhouse.io/v1/boards/{t}",
        "jobs":   "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
        "title":  "title",
        "url":    "absolute_url",
    },
    "lever": {
        "probe": "https://api.lever.co/v0/postings/{t}?mode=json",
        "jobs":   "https://api.lever.co/v0/postings/{t}?mode=json",
        "title":  "text",
        "url":    "hostedUrl",
    },
    "ashby": {
        "probe": "https://api.ashbyhq.com/posting-api/job-board/{t}",
        "jobs":   "https://api.ashbyhq.com/posting-api/job-board/{t}",
        "title":  "title",
        "url":    "jobUrl",
    },
}

SUFFIXES = ("inc", "llc", "ltd", "corp", "corporation", "co", "plc", "gmbh", "sa",
            "technologies", "technology", "tech", "labs", "laboratories", "group",
            "holdings", "systems", "solutions", "software", "io", "ai")

STOP = {"a", "an", "the", "of", "and", "for", "to", "in", "at", "on", "with", "or",
        "senior", "sr", "junior", "jr", "staff", "principal", "i", "ii", "iii",
        "lead", "head", "manager", "associate"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, timeout=15, tries=3):
    """GET + JSON, with retries.

    Without this, a single transient failure is indistinguishable from a real
    answer: the board fetch catches every exception, returns [], and the run
    reports "0/23 matched" as though that were a fact about the postings. It is
    not - it is a fact about the network that minute. 23 postings resolving to
    0 while the same boards answered 200 on a manual retry is exactly that bug.
    Retries with backoff cost a few seconds and keep the match rate honest.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            # 404/400 are real answers ("no such board"), not transport noise.
            if e.code in (404, 400):
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < tries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("unreachable")


def slugs(employer):
    """Candidate board tokens for an employer name, most likely first."""
    base = re.sub(r"[^a-z0-9]+", "", employer.lower())
    words = [w for w in re.split(r"[^a-z0-9]+", employer.lower()) if w]
    while words and words[-1] in SUFFIXES:
        words.pop()
    out = []
    for c in (base, "".join(words), "-".join(words)):
        if c and c not in out:
            out.append(c)
    return out


def discover(employer, cache, refresh=False, refresh_negatives=False):
    """Find the employer's ATS board. Returns (ats, token) or (None, None)."""
    for c in slugs(employer):
        hit = cache.get(c)
        # A cached NEGATIVE is only as good as the network that produced it, and
        # 115 of them are stamped 2026-09-26 - a run whose board fetches were
        # failing on transient transport errors. Those verdicts are suspect, so
        # --refresh-negatives re-probes them instead of trusting them forever.
        # This is the same stale-cache trap as scrape.py's dedupe-before-extract:
        # a persistent cache never re-examines a bad answer it wrote once.
        if hit and not refresh and not (refresh_negatives and not hit.get("ats")):
            return (hit["ats"], hit["token"]) if hit.get("ats") else (None, None)
        if hit and refresh:
            continue
        for name, cfg in ATS.items():
            try:
                get(cfg["probe"].format(t=c))
            except urllib.error.HTTPError as e:
                if e.code in (404, 400):
                    continue
                log(f"    probe {name}/{c} -> HTTP {e.code}")
                continue
            except Exception as e:
                log(f"    probe {name}/{c} -> {type(e).__name__}")
                continue
            cache[c] = {"ats": name, "token": c, "found": str(date.today())}
            log(f"    {employer}: {name} board '{c}'")
            return name, c
        # Never downgrade a POSITIVE to a negative. If a re-probe of a board we
        # already know about fails, that is this run's network, not the absence
        # of a board - and overwriting a good entry with a negative is how a
        # transient blip silently deletes months of discovery work.
        if not (cache.get(c) or {}).get("ats"):
            cache[c] = {"ats": None, "token": None, "found": str(date.today())}
    return None, None


# Postings that came straight off a company board (not an aggregator) already
# point at the real requisition, so there is nothing to discover for them. The
# board-token probe in discover() cannot see them anyway: company-hosted
# Greenhouse instances live on vanity domains (careers.datadoghq.com,
# databricks.com, esri.com) and are absent from the public boards API. Without
# this pass they stay unlabelled, which understates ATS coverage by ~860.
DIRECT_HOSTS = (
    ("ashby", re.compile(r"(?:^|\.)ashbyhq\.com$", re.I)),
    ("lever", re.compile(r"(?:^|\.)lever\.co$", re.I)),
    ("greenhouse", re.compile(r"(?:^|\.)greenhouse\.io$", re.I)),
)
GH_JID = re.compile(r"[?&]gh_jid=\d+", re.I)


def _host(url):
    h = (urlparse(url or "").netloc or "").lower()
    return h.rpartition("@")[2].split(":")[0].removeprefix("www.")


def detect_direct(url):
    # Match on the parsed host, never on a pattern anchored to the raw string:
    # "^...greenhouse\.io/" silently fails on "https://job-boards.greenhouse.io"
    # because the URL begins with the scheme, and \w cannot cross ':' or '/'.
    host = _host(url)
    for ats, rx in DIRECT_HOSTS:
        if rx.search(host):
            return ats
    if GH_JID.search(url or ""):   # company-hosted Greenhouse, vanity domain
        return "greenhouse"
    return None


def normalize_direct(P):
    """Label postings whose url is already the direct ATS requisition."""
    n = 0
    for p in P:
        if p.get("ats_url"):
            continue
        ats = detect_direct(p.get("url"))
        if not ats:
            continue
        p["ats_url"], p["ats"] = p["url"], ats
        p["ats_status"] = "direct feed"
        p["apply_url"] = p["url"]
        n += 1
    return n


def norm(s, strip_paren=True):
    s = (s or "").lower()
    if strip_paren:
        s = re.sub(r"\((?:[^)]*)\)", " ", s)  # drop parentheticals
    s = re.sub(r"[,/|–—\-:;]+", " ", s)       # separators
    s = re.sub(r"\b(?:remote|hybrid|us|usa|united states|worldwide|anywhere)\b", " ", s)
    return re.sub(r"[^a-z0-9 ]+", " ", s).split()


def toks(s):
    return {w for w in norm(s) if w not in STOP and len(w) > 1}


def score(ptitle, ploc, job, strip_paren=True):
    """0..1 confidence that an aggregator posting refers to this board job."""
    a, b = norm(ptitle, strip_paren), norm(job["title"], strip_paren)
    if a and a == b:
        s = 1.0
    else:
        ta, tb = toks(ptitle) if not strip_paren else toks(ptitle), toks(job["title"])
        if not ta or not tb:
            return 0.0
        j = len(ta & tb) / len(ta | tb)
        s = 0.80 * j if j >= 0.5 else j
    if ploc and job.get("location"):
        pl, jl = ploc.lower(), job["location"].lower()
        if pl[:18] in jl or jl[:18] in pl:
            s = min(1.0, s + 0.05)
    return s


def best_match(p, jobs, used, strip_paren):
    """Highest-scoring board job for posting p that no other posting has claimed.

    One board job may satisfy at most one aggregator posting. Without this,
    "Full-Stack Engineer (Front-End Leaning)" and "(Back-End Leaning)" both
    reduce to "full stack engineer" and both get pointed at the same req --
    which is worse than leaving them unresolved, because the recruiter opens a
    link to a job Jordan did not apply to.
    """
    ranked = sorted(
        ((score(p["title"], p.get("location", ""), j, strip_paren), j) for j in jobs),
        key=lambda x: -x[0])
    best, j = ranked[0]
    runner = next((s for s, jj in ranked[1:] if jj["url"] not in used), 0.0)
    if j["url"] in used:
        return 0.0, j, runner
    ok = best >= 0.90 or (best >= 0.75 and best - runner >= 0.10)
    return (best if ok else 0.0), j, runner


def board_jobs(ats, token, jcache):
    key = f"{ats}:{token}"
    if key in jcache:
        return jcache[key]
    cfg = ATS[ats]
    try:
        data = get(cfg["jobs"].format(t=token))
    except urllib.error.HTTPError as e:
        # 404/400 = the board genuinely is not there. Anything else is transport
        # noise and is recorded as such so a bad-network run cannot be read as
        # "this employer has no board".
        log(f"    board {key} -> no such board (HTTP {e.code})")
        jcache[key] = []
        return []
    except Exception as e:
        log(f"    board {key} -> TRANSPORT FAILURE {type(e).__name__}: {e}")
        jcache[key] = []
        return []
    raw = data if isinstance(data, list) else (data.get("jobs") or [])
    jobs = []
    for j in raw:
        title = j.get(cfg["title"])
        url = j.get(cfg["url"])
        if not title or not url:
            continue
        loc = j.get("location") or (j.get("categories") or {}).get("location") or ""
        if isinstance(loc, dict):          # greenhouse returns {"name": "..."}
            loc = loc.get("name") or " ".join(
                str(v) for v in loc.values() if isinstance(v, (str, int)))
        elif isinstance(loc, list):
            loc = " ".join(str(x) for x in loc)
        jobs.append({"title": title, "url": url, "location": str(loc)})
    jcache[key] = jobs
    log(f"    board {key}: {len(jobs)} open jobs")
    time.sleep(0.3)
    return jobs


def main():
    args = set(sys.argv[1:])
    dry = "--status" in args
    recheck = "--recheck" in args
    refresh_neg = "--refresh-negatives" in args
    only = None
    buckets = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--employer" and i + 1 < len(argv):
            only = argv[i + 1]
        if a == "--buckets" and i + 1 < len(argv):
            buckets = set(x.strip() for x in argv[i + 1].split(",") if x.strip())

    # postings.json is a MASTER DOC, not a bare list: scrape.py's
    # load_postings()/save_postings() round-trip the whole {owner_path,
    # generated_by, postings} object. Read and write the document, mutate only
    # the postings list, or the next `scrape.py sync` reads a list and dies.
    doc = json.load(open(POSTINGS))
    P = doc["postings"] if isinstance(doc, dict) else doc
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    jcache = {}

    # Runs first: these need no discovery, and labelling them here keeps them
    # out of `todo` below so the aggregator pass never re-probes them.
    n_direct = normalize_direct(P)
    if n_direct:
        log(f"{n_direct} postings already carried a direct ATS URL")

    if "--direct-only" in args:
        # Normalisation is idempotent and needs no network, so it can be
        # committed on its own without re-probing every aggregator board.
        if not dry:
            doc["postings"] = P
            json.dump(doc, open(POSTINGS, "w"), indent=2)
            log(f"wrote {POSTINGS}")
        log(f"{'DRY RUN' if dry else 'APPLIED'}: {n_direct} direct-feed "
            f"postings labelled")
        return 0

    todo = [p for p in P if not p.get("ats_url")] if not recheck else list(P)
    if recheck:
        for p in todo:
            p.pop("ats_url", None)
            p.pop("ats", None)
            p.pop("ats_status", None)
    if only:
        todo = [p for p in todo if only.lower() in p["employer"].lower()]
    # Probing is a network round-trip per employer, so scoping to the fit
    # buckets matters: 193 postings were unresolved but only 23 of them were
    # target/watch. `import pipeline` is deferred so this file stays runnable
    # even if pipeline's deps are missing from the interpreter in use.
    if buckets:
        import pipeline as _P
        todo = [p for p in todo if _P.fit_bucket(p) in buckets]
    agg = [p for p in todo if p.get("board") in ("jobicy", "workingnomads",
                                                 "remoteok", "remotive",
                                                 "weworkremotely")]

    log(f"{len(agg)} aggregator postings to resolve "
        f"({len(set(p['employer'] for p in agg))} employers)")

    matched = unresolved = 0
    by_emp = {}
    for p in agg:
        by_emp.setdefault(p["employer"], []).append(p)

    for emp, posts in sorted(by_emp.items()):
        log(f"  {emp} ({len(posts)})")
        ats, token = discover(emp, cache, refresh_negatives=refresh_neg)
        if not ats:
            unresolved += len(posts)
            for p in posts:
                p["ats_url"] = None
                p["ats_status"] = "no-board-found"
            continue
        jobs = board_jobs(ats, token, jcache)
        if not jobs:
            unresolved += len(posts)
            continue
        # Pass 1 keeps parentheticals, because they often carry the ONLY
        # disambiguator ("(Front-End Leaning)" vs "(Back-End Leaning)").
        # Pass 2 retries without them, and only for postings pass 1 could not
        # place. used_urls enforces one posting per board job across both.
        used = set()
        placed, deferred = [], []
        for p in sorted(posts, key=lambda x: -len(x["title"])):
            best, j, runner = best_match(p, jobs, used, strip_paren=False)
            if best:
                p["ats_url"], p["ats"] = j["url"], ats
                p["ats_status"] = f"matched {best:.2f} exact+paren"
                used.add(j["url"])
                placed.append(p)
            else:
                deferred.append(p)
        for p in deferred:
            best, j, runner = best_match(p, jobs, used, strip_paren=True)
            if best:
                p["ats_url"], p["ats"] = j["url"], ats
                p["ats_status"] = f"matched {best:.2f} (paren-stripped)"
                used.add(j["url"])
                matched += 1
            else:
                p["ats_url"] = None
                p["ats_status"] = (f"ambiguous {best:.2f} vs {runner:.2f}"
                                   if best else "no title match")
                unresolved += 1
        for p in placed:
            matched += 1

    # The ATS req is the thing you actually submit through, so it wins over the
    # aggregator link. Keep the aggregator URL in `url` for provenance.
    for p in agg:
        if p.get("ats_url"):
            p["apply_url"] = p["ats_url"]
        elif not p.get("apply_url"):
            p["apply_url"] = p["url"]

    if not dry:
        doc["postings"] = P
        json.dump(doc, open(POSTINGS, "w"), indent=2)
        json.dump(cache, open(CACHE, "w"), indent=1)
        log(f"\nwrote {POSTINGS} and {CACHE}")

    total = matched + unresolved
    log(f"\n{'DRY RUN' if dry else 'RESOLVED'}: {matched}/{total} matched "
        f"({100.0*matched/total if total else 0:.1f}%), {unresolved} unresolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
