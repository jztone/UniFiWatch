#!/usr/bin/env bash
#
# Install (or reinstall) the UniFiWatch five-minute launchd agent.
#
# Copies watch.py and config.local.json to local disk, fills the wrapper and
# plist templates with absolute paths, installs the plist to
# ~/Library/LaunchAgents/, and (re)loads it. Safe to run repeatedly.
#
# Usage:
#   ./install.sh            # install / reload the polling agent
#   ./install.sh uninstall  # stop and remove the agent
#
set -euo pipefail

LABEL="com.jztone.unifiwatch"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$REPO_DIR/${LABEL}.plist"
WRAPPER_TEMPLATE="$REPO_DIR/run.sh.template"
LA_DIR="$HOME/Library/LaunchAgents"
TARGET="$LA_DIR/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

# Local (non-CloudStorage) paths. Nothing on the scheduled path may live on the
# ProtonDrive mount: a launchd job is denied access to the provider's files
# ("Operation not permitted") even where an interactive shell reads them fine,
# and the mount is often unhydrated after a deferred wake. So the wrapper, a
# copy of watch.py, its config and the logs all go to local disk.
SUPPORT_DIR="$HOME/Library/Application Support/UniFiWatch"
WRAPPER="$SUPPORT_DIR/run.sh"
INSTALLED_WATCH_PY="$SUPPORT_DIR/watch.py"
INSTALLED_NOTIFY="$SUPPORT_DIR/notify.py"
INSTALLED_CONFIG="$SUPPORT_DIR/config.local.json"
LAUNCHD_LOG_DIR="$HOME/Library/Logs/UniFiWatch"

# Resolve the root the same way watch.py does (env > config > default), for the
# mkdir and the summary below. The installed watch.py resolves it for itself
# from the config copied next to it, so nothing needs pinning in the wrapper.
ROOT="${UNIFIWATCH_ROOT:-}"
if [[ -z "$ROOT" && -f "$REPO_DIR/config.local.json" ]]; then
    ROOT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("root",""))' \
            "$REPO_DIR/config.local.json" 2>/dev/null || true)"
fi
ROOT="${ROOT/#\~/$HOME}"
ROOT="${ROOT:-$HOME/UniFiWatch}"

uninstall() {
    echo "Stopping and removing ${LABEL}…"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$TARGET" "$WRAPPER" "$INSTALLED_WATCH_PY" "$INSTALLED_NOTIFY" \
          "$INSTALLED_CONFIG"
    # notify.py imports the installed watch.py on every poll, so Python leaves a
    # watch.*.pyc behind that nothing else clears (only the import is cached — a
    # script run as __main__ is never byte-compiled). This also clears caches
    # left by earlier installs whose scripts this version no longer ships.
    rm -rf "$SUPPORT_DIR/__pycache__"
    echo "Removed $TARGET and the installed copies under $SUPPORT_DIR"
    echo "State and events under $ROOT were left alone."
}

if [[ "${1:-}" == "uninstall" ]]; then
    uninstall
    exit 0
fi

PYTHON="$(command -v python3)"
if [[ -z "$PYTHON" ]]; then
    echo "error: python3 not found on PATH" >&2
    exit 1
fi

# The interpreter must not live on the CloudStorage mount either. If it does,
# the wrapper's exec fails before watch.py starts — the same denial that keeps
# the scripts off the mount applies to the thing that runs them, and a venv
# shim there is a symlink whose resolution needs the mount readable. Whoever
# runs this may also have a project venv first on PATH, and UniFiWatch is
# stdlib-only, so borrowing another project's environment buys nothing.
# Resolve through any shim to the real interpreter, then insist it is local.
PYTHON_REAL="$("$PYTHON" -c 'import os, sys
exe = getattr(sys, "_base_executable", None) or sys.executable
print(os.path.realpath(exe))' 2>/dev/null || true)"
if [[ -n "$PYTHON_REAL" && -x "$PYTHON_REAL" ]]; then
    if [[ "$PYTHON_REAL" != "$PYTHON" ]]; then
        echo "Resolved $PYTHON"
        echo "      → $PYTHON_REAL"
    fi
    PYTHON="$PYTHON_REAL"
fi
if [[ "$PYTHON" == "$HOME/Library/CloudStorage/"* ]]; then
    if [[ -x /usr/bin/python3 ]]; then
        echo "note: that interpreter is on the CloudStorage mount, which the" >&2
        echo "      scheduled job may be denied; using /usr/bin/python3." >&2
        PYTHON=/usr/bin/python3
    else
        echo "error: the only python3 found lives on the CloudStorage mount," >&2
        echo "       which launchd cannot rely on reading. Install a python3" >&2
        echo "       on local disk and re-run." >&2
        exit 1
    fi
fi
# Unlike the root, the credentials have no usable default: a scheduled poll
# without them would exit 1 every five minutes forever.
if [[ ! -f "$REPO_DIR/config.local.json" ]]; then
    echo "error: $REPO_DIR/config.local.json not found — create it first (see README)." >&2
    exit 1
fi
if ! python3 -c 'import json,sys
cfg = json.load(open(sys.argv[1]))
missing = [k for k in ("host", "apiKey", "siteId") if not cfg.get(k)]
sys.exit("missing " + ", ".join(missing) if missing else 0)' \
        "$REPO_DIR/config.local.json"; then
    echo "error: config.local.json is incomplete (see the message above)." >&2
    exit 1
fi

# A missing openclaw must not stop the poll, so the wrapper deliberately cannot
# fail on it — which makes install time the only moment a human hears about it.
# Ask notify.py for the path rather than duplicating the constant; importing it
# has no side effects, because it configures logging inside main().
OPENCLAW_PATH="$(cd "$REPO_DIR" && "$PYTHON" -c \
    'import notify; print(notify.load_openclaw())' 2>/dev/null || true)"
if [[ -z "$OPENCLAW_PATH" || ! -x "$OPENCLAW_PATH" ]]; then
    echo "note: no executable openclaw at '${OPENCLAW_PATH:-unknown}' — the" >&2
    echo "      poll will run and pending.json will fill, but no agent will" >&2
    echo "      be woken until it exists." >&2
fi

# Same reasoning for the agent and its channel: notify.py refuses to wake
# anyone without them, and only says so in launchd.err.log.
NOTIFY_AGENT="$(cd "$REPO_DIR" && "$PYTHON" -c \
    'import notify; s = notify.load_notify_settings()
print(s["agent"] if s["agent"] and s["target"] else "")' 2>/dev/null || true)"
if [[ -z "$NOTIFY_AGENT" ]]; then
    echo "note: notify.agent / notify.target are not set in config.local.json —" >&2
    echo "      the poll will run, but no agent will be woken." >&2
fi

mkdir -p "$LA_DIR" "$SUPPORT_DIR" "$LAUNCHD_LOG_DIR" "$ROOT/logs"

# Copy watch.py and its config to local disk. The scheduled job must not read
# the repo. These copies are build artifacts — edit the repo files and re-run
# this script to update them.
echo "Copying watch.py → $INSTALLED_WATCH_PY"
cp "$REPO_DIR/watch.py" "$INSTALLED_WATCH_PY"
chmod +x "$INSTALLED_WATCH_PY"

# The wrapper runs notify.py after every poll, so it cannot reach the repo
# either. It needs no path wiring beyond sitting here: Python puts a script's
# own directory first on sys.path, so its `import watch` finds the copy above,
# and that copy resolves CONFIG_FILE from its own location and picks up the
# installed config. run.sh derives this path as dirname(watch.py)/notify.py —
# there is no placeholder to keep in sync.
echo "Copying notify.py → $INSTALLED_NOTIFY"
cp "$REPO_DIR/notify.py" "$INSTALLED_NOTIFY"
chmod +x "$INSTALLED_NOTIFY"

# watch.py reads config.local.json from its own directory, so the copy is what
# configures the scheduled job. It holds the API key: owner-only, and never
# written anywhere the repo can see.
echo "Copying config.local.json → $INSTALLED_CONFIG"
cp "$REPO_DIR/config.local.json" "$INSTALLED_CONFIG"
chmod 600 "$INSTALLED_CONFIG"

echo "Rendering wrapper → $WRAPPER"
sed \
    -e "s#__PYTHON__#${PYTHON}#g" \
    -e "s#__WATCH_PY__#${INSTALLED_WATCH_PY}#g" \
    -e "s#__LAUNCHD_LOG_DIR__#${LAUNCHD_LOG_DIR}#g" \
    "$WRAPPER_TEMPLATE" > "$WRAPPER"
chmod +x "$WRAPPER"

echo "Rendering plist → $TARGET"
sed \
    -e "s#__WRAPPER__#${WRAPPER}#g" \
    -e "s#__WORKDIR__#${SUPPORT_DIR}#g" \
    -e "s#__LOG_DIR__#${LAUNCHD_LOG_DIR}#g" \
    "$TEMPLATE" > "$TARGET"

# Reload cleanly (bootout first so re-runs pick up changes).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"

echo "Installed. Polling every 5 minutes."
echo "State + events  : $ROOT/{state.json,events.jsonl,pending.json,notified.json}"
# Scheduled runs log to LAUNCHD_LOG_DIR: the wrapper exports it as
# UNIFIWATCH_LOG_DIR, so <root>/logs only ever sees manual `python3 watch.py` runs.
echo "Logs (scheduled): $LAUNCHD_LOG_DIR/{unifiwatch,wrapper,launchd.out,launchd.err}.log"
echo "Logs (manual)   : $ROOT/logs/unifiwatch.log"
echo
echo "Poll now:            launchctl kickstart -k $DOMAIN/$LABEL"
echo "Check status:        python3 status.py"
echo "Agent wake:          $INSTALLED_NOTIFY → openclaw agent --agent ${NOTIFY_AGENT:-<unset>}"
echo "Agent state:         launchctl print $DOMAIN/$LABEL | head -n 30"
