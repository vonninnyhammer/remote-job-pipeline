#!/usr/bin/env bash
# Daily remote-job board sync. Safe to run repeatedly (idempotent).
#   ./run.sh   (from anywhere)
set -uo pipefail
PIPE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$PIPE/logs/sync.log"
mkdir -p "$(dirname "$LOG")"

echo "==== $(date '+%Y-%m-%d %H:%M:%S') ====" >> "$LOG"
cd "$PIPE" || exit 1
python3 scrape.py sync >> "$LOG" 2>&1
echo "----- heat top 12 -----" >> "$LOG"
python3 pipeline.py heat 2>/dev/null | head -14 >> "$LOG"

# Focused hunt: rebuild shortlist + tracker workbook
SL="$PIPE/logs/shortlist.log"
echo "==== $(date '+%Y-%m-%d %H:%M:%S') ====" >> "$SL"
python3 pipeline.py shortlist >> "$SL"
python3 pipeline.py export >> "$SL" 2>&1
echo >> "$SL"

# Auto-submit email-apply postings (veto-gated, daily-capped)
AS="$PIPE/logs/autosend.log"
echo "==== $(date '+%Y-%m-%d %H:%M:%S') ====" >> "$AS"
python3 autosend.py send --limit 20 >> "$AS" 2>&1

echo >> "$LOG"