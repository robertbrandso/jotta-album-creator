#!/bin/bash
# Unattended wrapper around create_albums.py, meant to be run by launchd or cron.
#
# Usage: run.sh "Jotta/Sync/Pictures" ["Jotta/Archive/Old pictures" ...]
#
# Each folder is processed in turn, in one run under one lock.
#
# - Takes a lock so two runs never overlap (they would rotate the refresh token
#   in each other's way).
# - Appends output to a log file as it happens; the log is rotated at 1 MB
#   (one previous generation is kept).
# - Shows a macOS notification if the run fails.
set -u

if [ $# -lt 1 ]; then
    echo "usage: $0 <folder>... (e.g. Jotta/Sync/Pictures \"Jotta/Archive/Old pictures\")" >&2
    exit 2
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"
LOG="${JOTTA_ALBUM_LOG:-$HOME/Library/Logs/jotta-album-creator.log}"
LOCK="${TMPDIR:-/tmp}/jotta-album-creator.lock"

mkdir -p "$(dirname "$LOG")"
timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(timestamp)  $*" >> "$LOG"; }
RULE="================================================================"

# mkdir is atomic. A lock is stale if its owner pid is dead. If the pid file is
# missing the owner may be between mkdir and writing it, so only call that stale
# once the lock is over a minute old. A stale lock is claimed with mv, which only
# one process can win.
acquire_lock() {
    mkdir "$LOCK" 2>/dev/null && return 0
    local pid
    pid="$(cat "$LOCK/pid" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 1
    fi
    if [ -z "$pid" ] && [ -z "$(find "$LOCK" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
        return 1
    fi
    if mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
        rm -rf "$LOCK.stale.$$"
        log "removed stale lock"
        mkdir "$LOCK" 2>/dev/null && return 0
    fi
    return 1
}

if ! acquire_lock; then
    log "RUN SKIPPED: another run is in progress"
    exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# Rotate while holding the lock, so no other run is writing to the log.
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 1048576 ]; then
    mv "$LOG" "$LOG.1"
fi

# Each run is framed by a banner so runs are easy to tell apart in the log.
{ echo; echo "$RULE"; echo "$(timestamp)  RUN START  (pid $$, $# folder(s))"; echo "$RULE"; } >> "$LOG"

# Process every folder in turn; a failure in one doesn't stop the others.
# The script's output is streamed into the log with a timestamp on every line.
STATUS=0
FAILED=0
for FOLDER in "$@"; do
    log "start: $FOLDER"
    "$PYTHON" -u "$DIR/create_albums.py" "$FOLDER" --apply --quiet 2>&1 |
        while IFS= read -r LINE; do echo "$(timestamp)    $LINE"; done >> "$LOG"
    CODE=${PIPESTATUS[0]}
    if [ $CODE -ne 0 ]; then
        log "FAILED with exit code $CODE: $FOLDER"
        FAILED=$((FAILED + 1))
        [ $STATUS -eq 0 ] && STATUS=$CODE
    else
        log "done: $FOLDER"
    fi
done

if [ $FAILED -eq 0 ]; then RESULT="OK"; else RESULT="FAILED ($FAILED of $# folder(s))"; fi
{ echo "$(timestamp)  RUN END: $RESULT, took ${SECONDS}s"; echo "$RULE"; } >> "$LOG"

if [ $FAILED -ne 0 ]; then
    osascript -e "display notification \"$FAILED of $# folder(s) failed. See ~/Library/Logs/jotta-album-creator.log\" with title \"jotta-album-creator failed\"" >/dev/null 2>&1
    exit $STATUS
fi
