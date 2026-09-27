# remote-hunt

An automated, one-click remote job application pipeline. It aggregates live
postings from eight legitimate job boards, filters to **remote + US-workable +
$60k+**, and builds a tailored application kit per role — including real form
questions pre-drafted from the employer's own Greenhouse listing.

**The job search stays human.** Nothing is ever submitted automatically. The
pipeline stages your answers, loads them into your clipboard, and opens the
real application form in your browser. You review, approve, paste, and click
submit yourself.

## Why "almost one-click"?

Full auto-submit is a ToS/CAPTCHA minefield (Indeed, LinkedIn, ZipRecruiter all
forbid scraping and don't expose APIs). This tool respects that line: it reads
**official board APIs and public HTML**, stages your answers, and only opens the
real form for you to finish. Ten seconds of human attention per application
keeps every account and thread legit.

## Boards (official, compliant)

| Board | Source |
|---|---|
| Remotive | official API |
| RemoteOK | official API |
| WeWorkRemotely | official feed |
| Jobicy | official API |
| Working Nomads | official API |
| Greenhouse companies | official board API (with `?questions=true`) |
| Lever companies | official board API (with schema) |
| SmartRecruiters | official board API |

Greenhouse boards are special: the API exposes the **actual questions on the
form** (with options), so each answer is staged at the exact dropdown/text field
you'll see — no guessing.

## Workflow

```
sync → heat → stage → review → approve → open → submitted
```

```
pip install -r requirements.txt

# 0) Set up (gitignored, never commit):
cp owner.example.json owner.json     # identity + resume path + tracker output
cp profile.example.json profile.json # skill bullets + per-lane cover-letter angles

# 1) Pull all boards into postings.json
python3 scrape.py sync

# 2) Ranked view (verified salary first, unverified flagged '?')
python3 pipeline.py heat

# 3) ONE-CLICK for a single job: builds the kit, fetches the real form
#    questions from Greenhouse, drafts every answer, loads your clipboard,
#    and opens the application. Nothing is submitted.
python3 pipeline.py stage [job_id|--all]

# 4) Show what was generated
python3 pipeline.py review [job_id|--all]

# 5) Explicit gate: nothing opens the browser until approved
python3 pipeline.py approve [job_id|--all]

# 6) Open the real form + print paste-ready answers
python3 pipeline.py open [job_id|--all]

# 7) Record outcome
python3 pipeline.py submitted [job_id|--all]
python3 pipeline.py note <id> interview|offer|closed|skip <text>

# 8) Track progress in Excel
python3 pipeline.py export
```

## Cron

```
0 8,13,18 * * * /usr/bin/flock -n /tmp/remote-jobs.lock /path/to/remote-hunt/run.sh
```

`run.sh` is idempotent: it syncs and appends a heat summary to `logs/sync.log`.

## Filtering rules

- **Remote**: fully-remote or worldwide/global postings (US-workable).
- **US-workable**: US + worldwide/global. Regional-only postings are rejected and
  logged (`flows not-US/remote` in sync output).
- **Salary**: clear skips below $60k; unverified salary is kept if the role is
  in a whitelisted lane (AI/ML, Data, QA/Test, DevOps/Cloud, IT Ops, Customer
  Support, Business Ops, Accounting/Finance, Sales/BD, Legal/Compliance).

## Personalization

Everything personal stays out of the repo (gitignored):

- `owner.json` — name, contact, resume path, tracker output path.
- `profile.json` — resume-style skill bullets and per-lane cover-letter opening
  lines used by `generate`/`stage`.

Leave both absent and the tool still runs with neutral defaults.

## File layout

```
scrape.py        board sync (8 boards) → postings.json
pipeline.py      CLI: heat/status/stage/generate/review/approve/open/submitted/note/export
run.sh           cron entry point (sync + heat log)
postings.json    (gitignored) master store
state.json       (gitignored) per-job status
kits/<Employer> - <Title> - $ask/   (gitignored) generated application kits
```

## Notes

- No Indeed / LinkedIn / ZipRecruiter / Monster — no scraping of disallowed
  sources, period.
- Date/timestamp tracking per posting for dedupe (`seen.json`).
- Uses local system tools: `xclip` (clipboard) and `xdg-open` (browser) on
  Linux; clipboard falls back to printing the staged answers.

## License

MIT. See LICENSE. Built for the author's own job hunt — use it, adapt it,
reclaim your evenings.