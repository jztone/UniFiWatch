#!/usr/bin/env bash
#
# Behavioural tests for notify.py. Writes only under /tmp/uw-test and never
# calls openclaw for real: UNIFIWATCH_OPENCLAW points at a stub that records its
# argv, one bracketed argument per line, so the exact command is assertable.
#
# What it is protecting: the deduplication is the whole point of the script and
# nothing else in the repo would notice if it broke. A regression there means
# the agent is woken every five minutes on events it has already reported.
#
# Usage:
#   bash tools/test_notify.sh
#
set -uo pipefail

REPO="${UNIFIWATCH_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
NOTIFY="$REPO/notify.py"
PY="$(command -v python3)"

ROOT=/tmp/uw-test
BIN="$ROOT/bin"
CALLS="$ROOT/calls.log"
export UNIFIWATCH_ROOT="$ROOT"
export UNIFIWATCH_OPENCLAW="$BIN/openclaw"
# Neutral values, so the asserted argv never depends on (or reveals) the
# agent and channel in a real config.local.json.
export UNIFIWATCH_NOTIFY_AGENT="test-agent"
export UNIFIWATCH_NOTIFY_TARGET="C000TEST"

case "$ROOT" in
    /tmp/uw-*) rm -rf "$ROOT" ;;
    *) echo "refusing to rm '$ROOT'" >&2; exit 1 ;;
esac
mkdir -p "$BIN"

cat >"$BIN/openclaw" <<EOF
#!/bin/sh
{ printf 'CALL'; for a in "\$@"; do printf ' [%s]' "\$a"; done; echo; } >>"$CALLS"
EOF
chmod +x "$BIN/openclaw"

pass=0; fail=0
ok()  { pass=$((pass+1)); printf '  ok   %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '  FAIL %s\n' "$1"; }
check() {
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 — want '$3', got '$2'"; fi
}
grep_err() {
    case "$ERR" in
        *"$2"*) ok "$1" ;;
        *) bad "$1 — stderr was: $ERR" ;;
    esac
}

calls() { if [ -f "$CALLS" ]; then wc -l <"$CALLS" | tr -d ' '; else echo 0; fi; }

# The child is detached, so a call can land after notify.py has already exited:
# poll for it rather than reading the count once. A wake we do NOT want can only
# be disproved by waiting, hence the explicit sleeps on the negative assertions.
wait_calls() {
    i=0
    while [ "$(calls)" -lt "$1" ] && [ "$i" -lt 40 ]; do sleep 0.05; i=$((i+1)); done
    calls
}

# 2>&1 >/dev/null keeps only stderr. This does not hang because notify.py gives
# the child DEVNULL for all three streams; a child inheriting our stdout would
# hold the command substitution's pipe open until the agent turn finished, which
# is exactly the failure that redirection exists to prevent.
run_notify() {
    ERR="$(cd "${1:-$REPO}" && "$PY" "$NOTIFY" 2>&1 >/dev/null)"
    RC=$?
}

# One space-separated string, not "$@": macOS bash 3.2 errors on an empty
# argument list under `set -u`, and the empty queue is a case worth testing.
seed_queue() {
    "$PY" - "$1" <<'PY'
import json, os, sys
events = [{"id": i, "ts": "2026-07-28T10:00:00+00:00", "kind": "device_offline",
           "subject": "test-ap", "detail": {}, "acknowledged": False}
          for i in sys.argv[1].split()]
path = os.path.join(os.environ["UNIFIWATCH_ROOT"], "pending.json")
with open(path, "w") as fh:
    json.dump({"generated": "2026-07-28T12:00:00+02:00",
               "count": len(events), "events": events}, fh)
PY
}

notified_ids() {
    "$PY" - <<'PY'
import json, os
path = os.path.join(os.environ["UNIFIWATCH_ROOT"], "notified.json")
try:
    print(",".join(json.load(open(path))["ids"]))
except FileNotFoundError:
    print("<missing>")
PY
}

echo "notify.py — behavioural tests (root=$ROOT)"

# 1. No pending.json at all: unknown, not empty. Nothing read, nothing written.
run_notify
check "no pending.json → exit 0"        "$RC" 0
check "no pending.json → no call"       "$(calls)" 0
check "no pending.json → no state"      "$(notified_ids)" "<missing>"

# 2. A known-empty queue.
seed_queue ""
run_notify
check "empty queue → exit 0"            "$RC" 0
sleep 0.3
check "empty queue → no call"           "$(calls)" 0
check "empty queue → no state"          "$(notified_ids)" "<missing>"

# 3. Two events → exactly one wake, with the exact argv.
seed_queue "e1 e2"
run_notify
check "queue → exit 0"                  "$RC" 0
check "queue → one call"                "$(wait_calls 1)" 1
check "queue → argv" "$(tail -n1 "$CALLS")" \
  "CALL [agent] [--agent] [test-agent] [--message] [kör natverk-rapport] [--deliver] [--reply-channel] [slack] [--reply-to] [C000TEST]"
check "queue → recorded"                "$(notified_ids)" "e1,e2"
grep_err "queue → logged to stderr"     "waking test-agent for 2 new event"

# 4. Same queue again — the ack has not landed yet. This is the whole point.
run_notify
check "rerun → exit 0"                  "$RC" 0
sleep 0.3
check "rerun → still one call"          "$(calls)" 1
check "rerun → state unchanged"         "$(notified_ids)" "e1,e2"

# 5. A third event beside the two already reported → one more wake.
seed_queue "e1 e2 e3"
run_notify
check "new event → second call"         "$(wait_calls 2)" 2
check "new event → recorded"            "$(notified_ids)" "e1,e2,e3"

# 6. An ack queued but not yet applied by a poll → no wake for e4.
seed_queue "e1 e2 e3 e4"
printf '{"acknowledge": ["e4"]}\n' >"$ROOT/ack-inbox.json"
run_notify
sleep 0.3
check "queued ack → no call"            "$(calls)" 2
check "queued ack → not recorded"       "$(notified_ids)" "e1,e2,e3"
rm -f "$ROOT/ack-inbox.json"

# 7. The poll applied them: ids that left the queue are pruned.
seed_queue "e3"
run_notify
sleep 0.3
check "prune → no call"                 "$(calls)" 2
check "prune → pruned"                  "$(notified_ids)" "e3"

# 8. Corrupt state is a duplicate report, never permanent silence.
printf 'not json at all' >"$ROOT/notified.json"
run_notify
check "corrupt state → wakes anyway"    "$(wait_calls 3)" 3
check "corrupt state → rewritten"       "$(notified_ids)" "e3"
grep_err "corrupt state → warned"       "cannot read"

# 9. An overlapping run skips instead of queueing behind the first.
"$PY" - <<'PY' &
import fcntl, os, time
root = os.environ["UNIFIWATCH_ROOT"]
handle = open(os.path.join(root, ".notify.lock"), "w")
fcntl.flock(handle, fcntl.LOCK_EX)
open(os.path.join(root, "held"), "w").close()
time.sleep(10)
PY
holder=$!
while [ ! -f "$ROOT/held" ]; do sleep 0.05; done
seed_queue "e3 e5"
run_notify
check "lock held → exit 0"              "$RC" 0
sleep 0.3
check "lock held → no call"             "$(calls)" 3
check "lock held → no state change"     "$(notified_ids)" "e3"
grep_err "lock held → logged"           "holds the lock"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null; rm -f "$ROOT/held"

# 10. cwd must not matter: `import watch` resolves from the script's directory.
run_notify /tmp
check "from /tmp → exit 0"              "$RC" 0
check "from /tmp → call"                "$(wait_calls 4)" 4
check "from /tmp → recorded"            "$(notified_ids)" "e3,e5"

# 11. A broken openclaw path is loud — and the preflight runs before any state
#     is written, so a fresh id stays unrecorded and is retried next poll.
before="$(notified_ids)"
seed_queue "e3 e5 e6"
saved="$UNIFIWATCH_OPENCLAW"
export UNIFIWATCH_OPENCLAW=/nonexistent/openclaw
run_notify
export UNIFIWATCH_OPENCLAW="$saved"
check "bad openclaw → exit 1"           "$RC" 1
check "bad openclaw → no call"          "$(calls)" 4
check "bad openclaw → state untouched"  "$(notified_ids)" "$before"
grep_err "bad openclaw → message"       "no executable openclaw"

# 12. …and the retry works once the path is good again.
run_notify
check "recovered → call"                "$(wait_calls 5)" 5
check "recovered → recorded"            "$(notified_ids)" "e3,e5,e6"

# 13. No target is a broken install, like a missing binary: loud, and nothing
#     recorded. A blank env value (stripped to "") stands in for an absent one,
#     because env beats config.local.json and the repo may hold a real one.
before="$(notified_ids)"
seed_queue "e3 e5 e6 e7"
export UNIFIWATCH_NOTIFY_TARGET=" "
run_notify
export UNIFIWATCH_NOTIFY_TARGET="C000TEST"
check "no target → exit 1"              "$RC" 1
sleep 0.3
check "no target → no call"             "$(calls)" 5
check "no target → state untouched"     "$(notified_ids)" "$before"
grep_err "no target → message"          "notify.target not configured"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
