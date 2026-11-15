#!/usr/bin/env python3
"""Wake the reporting agent when UniFiWatch has events it has not seen yet.

Runs after watch.py in the same poll. watch.py decides *what happened*; this
decides *whether anyone needs to be told*. It makes no API calls of its own,
reads only files watch.py already wrote, and spawns one detached command:

    openclaw agent --agent <agent> --message "kör natverk-rapport"
                   --deliver --reply-channel slack --reply-to <target>

<agent> and <target> come from the "notify" table in config.local.json (or
UNIFIWATCH_NOTIFY_AGENT / UNIFIWATCH_NOTIFY_TARGET); see load_notify_settings.

Why this exists rather than a cron trigger script: the consumer's scheduler
evaluates trigger scripts in a sandbox that forbids module imports, so a
condition that needs `import watch` cannot live there at all. Putting it here is
better anyway — latency drops from the cron interval to the poll interval, and
the condition sits next to the state it reads.

State:

    notified.json   event ids already handed to the agent

pending.json is not emptied until the agent acknowledges, and an acknowledgement
only takes effect at the next poll. Without notified.json every poll in that
window would wake the agent again with the same events, forever. The file is
pruned against the queue on every run, so it cannot grow for the life of the
install.

notified.json is derived state. If events.jsonl or state.json is ever deleted by
hand, delete it too: the event counter restarts at e1, and an id recorded here
would suppress a brand-new event as already reported.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import watch  # same root resolution, atomic write and ack rules as the poller

# --- Configuration ---------------------------------------------------------

# Absolute, and deliberately not a PATH lookup: launchd's PATH is not a login
# shell's, so a bare "openclaw" would resolve when tested by hand and nowhere
# else — the failure would appear only on the scheduled path.
# "~" is expanded by load_openclaw, so the default is still absolute by the
# time it is checked.
OPENCLAW = "~/.local/bin/openclaw"

# Defaults for the "notify" table in config.local.json. agent and target have
# none: they name somebody's agent and somebody's Slack channel, and a guess
# would wake the wrong one. The message is the agent's own trigger phrase, not
# prose for a human — change it only when the agent's instructions change too.
NOTIFY_DEFAULTS = {
    "agent": "",
    "message": "kör natverk-rapport",
    "channel": "slack",
    "target": "",
}

NOTIFIED_FILENAME = "notified.json"
# NOT <root>/.lock. That one is watch.run_lock's: it is held for a whole poll
# and it blocks rather than failing, so a notifier queued behind it would be
# waiting to act on the very files that poll is rewriting.
LOCK_FILENAME = ".notify.lock"

log = logging.getLogger("notify")


def load_notify_settings() -> dict[str, str]:
    """Resolve agent, message, channel and target: env > config > default.

    The config layer is the "notify" table of config.local.json, read through
    watch's cached reader so the file is parsed once per process. Env names are
    UNIFIWATCH_NOTIFY_<KEY>. A missing agent or target comes back empty; main()
    refuses to wake anyone with it.
    """
    table = watch._read_config().get("notify")
    if not isinstance(table, dict):
        table = {}
    return {
        key: str(os.environ.get(f"UNIFIWATCH_NOTIFY_{key.upper()}")
                 or table.get(key) or default).strip()
        for key, default in NOTIFY_DEFAULTS.items()
    }


def load_openclaw() -> str:
    """Resolve the openclaw binary: env var > module constant.

    Env only, with no config.local.json layer — the one deliberate departure
    from the repo's three-step precedence. That file holds the console's
    credentials, is validated by install.sh and copied to the install directory;
    this is a property of the machine, not of the site. It doubles as the test
    seam: point UNIFIWATCH_OPENCLAW at a stub that records its argv and the
    whole path runs without waking a real agent.
    """
    configured = os.environ.get("UNIFIWATCH_OPENCLAW") or OPENCLAW
    return os.path.expanduser(configured)


# --- Locking ---------------------------------------------------------------

@contextlib.contextmanager
def notify_lock(root: Path):
    """Take <root>/.notify.lock without blocking; yield whether we got it.

    Guards the read-modify-write of notified.json against a second notifier — a
    manual run alongside a scheduled one — which would otherwise read the same
    state and wake the agent twice for one set of events.

    Non-blocking because an overlapping run must skip, not queue. An agent turn
    is allowed 300s and the poll arrives every 300s; everything a waiting run
    would decide is derived from files the run ahead of it is rewriting, so
    blocking only delays a decision already made, and a wrapper still stuck here
    when launchd fires again would stack wrappers.
    """
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / LOCK_FILENAME, "w")
    try:
        acquired = True
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # BlockingIOError while another run holds it
            acquired = False
        yield acquired
    finally:
        handle.close()  # releases the lock


# --- Notified state --------------------------------------------------------

def sort_ids(ids) -> list[str]:
    """Sort `e<N>` ids numerically, so e9 comes before e10 in files and logs."""
    def key(event_id: str) -> tuple[int, int, str]:
        digits = event_id[1:]
        if event_id.startswith("e") and digits.isdigit():
            return (0, int(digits), "")
        return (1, 0, event_id)

    return sorted(ids, key=key)


def load_notified(path: Path) -> tuple[set[str], bool]:
    """Read notified.json as (ids, needs_rewrite).

    A corrupt file must not wedge notifications permanently, so one that will
    not parse counts as empty: that risks one duplicate report, where treating
    it as fatal risks silence, which is worse.

    needs_rewrite is what gets such a file repaired. Without it a run that found
    nothing else to do would return early and leave the bad file exactly where
    it was, warning on every poll from here on.
    """
    if not path.exists():
        return set(), False
    try:
        document = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("cannot read %s (%s) — treating as empty", path, exc)
        return set(), True
    ids = document.get("ids") if isinstance(document, dict) else None
    if not isinstance(ids, list):
        log.warning("%s has no 'ids' list — treating as empty", path)
        return set(), True
    kept = {entry for entry in ids if isinstance(entry, str) and entry}
    return kept, len(kept) != len(ids)


def save_notified(path: Path, ids: set[str]) -> None:
    """Write notified.json atomically; `updated` is local time, like pending.

    Via watch.write_json_atomic, so there is one definition of what an atomic
    write is here. A truncated file would read as empty and re-notify.
    """
    watch.write_json_atomic(path, {
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(ids),
        "ids": sort_ids(ids),
    })


# --- The pending queue -----------------------------------------------------

def queue_ids(root: Path) -> set[str] | None:
    """Event ids listed in pending.json, or None when it cannot be read.

    None and the empty set are different answers and the caller must not
    conflate them. Empty means the poller says nothing is outstanding, so
    notified.json can be pruned to nothing. None means we do not know, and the
    only safe move is to touch neither the state file nor the agent: a missing
    pending.json is the normal state of an install whose first poll has not
    landed, and a consumer is allowed to delete it — the next poll writes it
    back. Pruning against an absent file would throw away exactly the state that
    stops the next run reporting everything a second time.
    """
    path = root / watch.PENDING_FILENAME
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.error("cannot read %s: %s", path, exc)
        return None
    events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(events, list):
        # Falling back to the `count` field would be wrong here. A count with no
        # ids is something to report but nothing to record as reported, so the
        # wake could not be deduplicated and would repeat every five minutes for
        # as long as the file kept that shape.
        log.warning("%s has no 'events' list — nothing to dedup against", path)
        return None
    ids: set[str] = set()
    dropped = 0
    for event in events:
        event_id = event.get("id") if isinstance(event, dict) else None
        # Compared verbatim, not through watch.normalize_event_id: that exists
        # for the inbox, which a consumer writes by hand. pending.json is
        # machine-written, and a normalised id that no longer matched the
        # literal string on disk would silently break both the prune and the
        # suppression comparison.
        if isinstance(event_id, str) and event_id:
            ids.add(event_id)
        else:
            dropped += 1
    if dropped:
        log.warning("%s: %d event(s) with no usable id", path, dropped)
    return ids


# --- Waking the agent ------------------------------------------------------

def wake_agent(openclaw: str, settings: dict[str, str]) -> None:
    """Fire and forget. The poll must not wait on an LLM turn.

    A turn may take minutes and launchd fires the next poll at 300 seconds
    regardless. start_new_session puts the child in a session of its own so it
    survives the wrapper exiting, and all three streams go to DEVNULL: an
    inherited stdout would hold the wrapper's pipe open for the length of the
    turn — the very thing being avoided — and the child's output would
    interleave with the poller's in launchd's log. Nothing waits on the child;
    it is reparented, so there is no zombie to reap.

    Raises OSError if the binary cannot be executed. The caller decides what
    that means.
    """
    subprocess.Popen(
        [
            openclaw, "agent",
            "--agent", settings["agent"],
            "--message", settings["message"],
            "--deliver",
            "--reply-channel", settings["channel"],
            "--reply-to", settings["target"],
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# --- Main ------------------------------------------------------------------

def main() -> int:
    # stderr, so launchd captures it in launchd.err.log beside the wrapper's own
    # log. Configuring the root logger here also surfaces the warnings watch's
    # logger emits while the inbox is read, which would otherwise vanish. The
    # quiet paths log at DEBUG, so a poll with nothing to do stays silent and
    # the log holds only wakes, prunes, skips and errors.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s notify %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Preflight before anything is locked, read or written. A binary that is not
    # there is a broken install rather than a transient failure, and checking
    # first means a fresh id is never recorded as notified by a run that was
    # already incapable of waking anyone.
    openclaw = load_openclaw()
    if not os.path.isabs(openclaw) or not os.access(openclaw, os.X_OK):
        log.error("no executable openclaw at %s — set UNIFIWATCH_OPENCLAW or "
                  "fix OPENCLAW in this script", openclaw)
        return 1
    settings = load_notify_settings()
    missing = [k for k in ("agent", "target") if not settings[k]]
    if missing:
        log.error("notify.%s not configured — set it in config.local.json "
                  "or UNIFIWATCH_NOTIFY_%s", "/".join(missing),
                  "/".join(k.upper() for k in missing))
        return 1

    root = watch.load_root()
    with notify_lock(root) as acquired:
        if not acquired:
            # Nothing is lost by skipping: whoever holds the lock is reading the
            # same files and will reach the same decision.
            log.info("another notify run holds the lock — skipping")
            return 0

        # Read the acknowledgement inbox BEFORE pending.json, never after. A
        # poll draining the inbox deletes ack-inbox.processing.json only after
        # it has rewritten pending.json, so a reader taking the queue first
        # could pair a stale queue with an inbox that has already gone, and wake
        # the agent for events it reported minutes ago. In this order the worst
        # case is suppressing an id for one cycle.
        ack_all, suppressed = watch.suppressed_ack_ids(root)
        queued = queue_ids(root)
        if queued is None:
            log.debug("no usable pending.json under %s — nothing to do", root)
            return 0

        path = root / NOTIFIED_FILENAME
        notified, rewrite = load_notified(path)
        # An inbox asking for "all" is about to clear the whole queue, so
        # nothing in it is worth a wake.
        fresh = set() if ack_all else queued - suppressed - notified
        # Ids that have left the queue are acknowledged and done. Dropping them
        # keeps the file bounded and means a recycled id would notify again,
        # which is the safe direction.
        keep = (notified | fresh) & queued

        # No separate test for `fresh` here: it is disjoint from `notified` and
        # a subset of `queued`, so anything fresh is in `keep` and not in
        # `notified`. This early return cannot swallow a wake.
        if keep == notified and not rewrite:
            log.debug("nothing new: %d queued, %d already notified",
                      len(queued), len(notified))
            return 0

        # Record before invoking. If this write fails the agent must not be
        # woken at all: an unrecorded wake repeats every five minutes for as
        # long as the events sit in the queue, and that storm is worse than the
        # failure in the other direction. That other direction is accepted, not
        # solved — if the turn spawned below dies mid-way, its report dies with
        # it and those events are not re-sent automatically. It is bounded: they
        # stay in pending.json, and a woken agent reads the whole queue rather
        # than only the ids that triggered it, so the next report carries them.
        try:
            save_notified(path, keep)
        except OSError as exc:
            log.error("cannot write %s: %s — not waking agent", path, exc)
            return 1

        if not fresh:
            log.info("rewrote %s: %d id(s) awaiting an ack, %d pruned",
                     NOTIFIED_FILENAME, len(keep), len(notified - keep))
            return 0

        try:
            wake_agent(openclaw, settings)
        except OSError as exc:
            log.error("cannot invoke openclaw: %s", exc)
            return 1
        log.info("waking %s for %d new event(s): %s",
                 settings["agent"], len(fresh), ", ".join(sort_ids(fresh)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
