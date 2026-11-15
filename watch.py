#!/usr/bin/env python3
"""UniFiWatch — five-minute monitor for a local UniFi Dream Machine.

Polls the console's local Network Integration API, compares what it finds
against the previous poll, and writes one line per *transition* to an
append-only event log:

    <root>/state.json      what we saw last time
    <root>/events.jsonl    every transition, ever
    <root>/pending.json    the unacknowledged subset, for the consumer
    <root>/metrics/        per-client traffic samples, for trend analysis

Design notes:
  - Python 3, standard library only. No third-party dependencies.
  - Events are written on transition only. A device that is still offline on
    the next poll produces nothing; when it returns we report the outage.
  - Going offline starts a timer; the event is written only once the device's
    threshold is exceeded, so a brief unplug is silence rather than noise.
  - Every collector is independent: one that raises is logged and counted, and
    the rest of the poll continues.
  - Outages are reported retrospectively. While the link is down there is
    nobody to tell, so the outage window is persisted and reported on recovery.
  - Statistics are a separate world from events: they come from the legacy
    API, they answer trend questions rather than "what changed?", and nothing
    they do can affect the poll's events or its exit code.
  - macOS-only: notifications use osascript, scheduling uses launchd.

See CLAUDE.md for the full project guide.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Configuration ---------------------------------------------------------

DEFAULT_ROOT = Path.home() / "UniFiWatch"
# Read from the script's OWN directory, so the installed copy under
# ~/Library/Application Support/UniFiWatch picks up the config install.sh
# placed beside it rather than falling back to an unconfigured default.
CONFIG_FILE = Path(__file__).resolve().parent / "config.local.json"

DEFAULT_OFFLINE_MINUTES = 5
API_BASE = "/proxy/network/integration/v1"
# The unofficial API the statistics collector reads. Kept apart from API_BASE
# on purpose — see the collector section for why the two must never share a
# fetch function.
LEGACY_API_BASE = "/proxy/network/api"
DEFAULT_LEGACY_SITE = "default"
DEFAULT_RETAIN_DAYS = 90
PAGE_LIMIT = 100        # the API defaults to 25; fewer round-trips at 100
MAX_PAGES = 100         # backstop against a console that never stops paging
HTTP_TIMEOUT = 15
PING_BIN = "/sbin/ping"  # absolute: launchd's PATH is not a login shell's
PING_TARGET = "1.1.1.1"
DNS_PROBE_HOST = "apple.com"

log = logging.getLogger("unifiwatch")


@functools.lru_cache(maxsize=1)
def _read_config() -> dict:
    """Parse config.local.json, or return {} when it is missing or unreadable.

    Cached for the process lifetime: one `python3 watch.py` run is one poll,
    and nothing writes config.local.json mid-run, so a fresh read on every
    call would only re-parse the same file. Environment variables still take
    precedence over the config in every load_* resolver, so caching the file
    here does not change the env > config > default rule.
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", CONFIG_FILE, exc)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def load_root() -> Path:
    """Resolve the state/log root: env var > config.local.json > default."""
    env = os.environ.get("UNIFIWATCH_ROOT")
    if env:
        return Path(env).expanduser()
    configured = _read_config().get("root")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_ROOT


def load_api_settings() -> tuple[str, str, str]:
    """Resolve (host, apiKey, siteId): env var > config.local.json > empty.

    Missing values come back as empty strings rather than raising, so main()
    can name every one of them at once instead of failing on whichever happens
    to be read first.
    """
    cfg = _read_config()

    def pick(env_name: str, cfg_key: str) -> str:
        return str(os.environ.get(env_name) or cfg.get(cfg_key) or "").strip()

    return (pick("UNIFIWATCH_HOST", "host"),
            pick("UNIFIWATCH_API_KEY", "apiKey"),
            pick("UNIFIWATCH_SITE_ID", "siteId"))


def _positive_minutes(raw, source: str, fallback: float) -> float:
    """Coerce a threshold to a positive number of minutes, or warn and fall back.

    A zero or negative threshold would make every momentary flap alertable,
    which is the exact noise this tool exists to suppress.
    """
    if raw is None or raw == "":
        return fallback
    try:
        minutes = float(raw)
    except (ValueError, TypeError):
        minutes = 0.0
    if minutes <= 0:
        log.warning("Ignoring invalid %s (%r); using %g minute(s)",
                    source, raw, fallback)
        return fallback
    return minutes


def load_thresholds() -> tuple[float, dict[str, float]]:
    """Resolve (default offline minutes, per-device-name overrides).

    Overrides are keyed by device NAME, matching what config.local.json and the
    UniFi app show; renaming a device in the app therefore rebinds it to the
    default until config.local.json is updated to match.
    """
    thresholds = _read_config().get("thresholds") or {}
    raw = os.environ.get("UNIFIWATCH_DEFAULT_OFFLINE_MINUTES")
    source = "UNIFIWATCH_DEFAULT_OFFLINE_MINUTES"
    if raw is None:
        raw = thresholds.get("default_offline_minutes")
        source = f"thresholds.default_offline_minutes in {CONFIG_FILE}"
    default = _positive_minutes(raw, source, DEFAULT_OFFLINE_MINUTES)
    per_device = {
        name: _positive_minutes(value, f"thresholds.per_device[{name!r}]", default)
        for name, value in (thresholds.get("per_device") or {}).items()
    }
    return default, per_device


_MAC_PREFIX = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){0,5}")


def load_client_ignores() -> tuple[str, ...]:
    """Resolve the MAC prefixes whose new clients are recorded but not reported.

    An explicit list rather than a heuristic: "wired, randomized, behind an AP"
    also describes a stranger plugged into an AP's second port, which is what
    the new_client event exists to catch. Prefixes must be whole octets in
    colon form; anything else is dropped with a warning, because an empty or
    malformed prefix could match far more than its author meant.
    """
    section = _read_config().get("clients") or {}
    if not isinstance(section, dict):
        # Raising here would abort the poll before any collector ran; a typo
        # in an optional filter must cost the filter, not the alerts.
        log.warning("Ignoring clients in %s: not an object", CONFIG_FILE)
        return ()
    raw = section.get("ignore_mac_prefixes") or []
    if not isinstance(raw, list):
        log.warning("Ignoring clients.ignore_mac_prefixes in %s: not a list",
                    CONFIG_FILE)
        return ()
    prefixes = []
    for entry in raw:
        # No str() coercion: a bare 12 would become the prefix "12" and
        # silence every MAC beginning 12:, which nobody wrote on purpose.
        prefix = entry.strip().lower() if isinstance(entry, str) else ""
        if not _MAC_PREFIX.fullmatch(prefix):
            log.warning("Ignoring invalid MAC prefix %r in "
                        "clients.ignore_mac_prefixes", entry)
            continue
        prefixes.append(prefix)
    return tuple(prefixes)


def load_log_dir(root: Path) -> Path:
    """Resolve where unifiwatch.log is written: env var > <root>/logs."""
    env = os.environ.get("UNIFIWATCH_LOG_DIR")
    if env:
        return Path(env).expanduser()
    return root / "logs"


def _positive_int(raw, source: str, fallback: int) -> int:
    """Coerce a count of days to a positive int, or warn and fall back.

    A zero or negative retention would delete today's metrics on the poll that
    wrote them, which reads as the collector being broken rather than as a
    misconfiguration.
    """
    if raw is None or raw == "":
        return fallback
    try:
        days = int(raw)
    except (ValueError, TypeError):
        days = 0
    if days <= 0:
        log.warning("Ignoring invalid %s (%r); using %d day(s)",
                    source, raw, fallback)
        return fallback
    return days


def load_stats_settings() -> tuple[bool, int, str]:
    """Resolve (enabled, retain_days, legacy site name) for the stats collector.

    `site` is the LEGACY site name — almost always `default` — and is a
    different identifier space from the Integration API's `siteId` UUID. Two
    site keys in one config file looks like a mistake until you know that.
    """
    stats = _read_config().get("stats")
    stats = stats if isinstance(stats, dict) else {}
    enabled = stats.get("enabled")
    retain = _positive_int(stats.get("retain_days"),
                           f"stats.retain_days in {CONFIG_FILE}",
                           DEFAULT_RETAIN_DAYS)
    site = str(stats.get("site") or DEFAULT_LEGACY_SITE).strip()
    return (True if enabled is None else bool(enabled),
            retain, site or DEFAULT_LEGACY_SITE)


# --- Atomic writes ---------------------------------------------------------

def write_json_atomic(target: Path, data) -> None:
    """Serialise to a temp file beside the target, then os.replace() onto it.

    pending.json is polled by another process, which must never observe a
    half-written document. The temp file goes in the same directory so the
    replace is a rename within one filesystem, hence atomic.
    """
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, target)


# --- Locking ---------------------------------------------------------------

@contextlib.contextmanager
def run_lock(root: Path):
    """Hold an exclusive lock on <root>/.lock for the duration of the block.

    A poll appends to events.jsonl while a manual --acknowledge may be
    rewriting that same file; without the lock the rewrite would silently drop
    an event that landed between its read and its write. Blocking rather than
    failing fast, because a poll takes seconds and launchd never runs two
    instances of one label concurrently.

    Read-only tools such as status.py deliberately take no lock: every rewrite
    lands via os.replace, so they can only ever see a complete file.
    """
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / ".lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()  # releases the lock


# --- State -----------------------------------------------------------------

STATE_FILENAME = "state.json"
EVENTS_FILENAME = "events.jsonl"
PENDING_FILENAME = "pending.json"
STATE_SCHEMA = 1


def load_state(root: Path) -> dict:
    """Read state.json, or an empty skeleton when it is missing or corrupt.

    Each collector's slice starts as None, not as an empty dict: None means
    "seed silently on this run", so a first install — or a lost state file —
    costs history rather than producing one event per device and client on the
    network.
    """
    path = root / STATE_FILENAME
    if path.exists():
        try:
            state = json.loads(path.read_text())
            if isinstance(state, dict):
                return state
            log.warning("State file %s is not an object; starting fresh", path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Corrupt state file %s (%s); starting fresh", path, exc)
    return {"schema": STATE_SCHEMA, "devices": None, "known_clients": None,
            "wan": None, "next_event_id": 1, "last_run": None}


def save_state(root: Path, state: dict) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(root / STATE_FILENAME, state)


# --- Events and pending ----------------------------------------------------

def load_events(root: Path) -> list[tuple[str, dict | None]]:
    """Read events.jsonl as (raw line, parsed object or None) pairs.

    Unparseable lines come back with a None object so a rewrite can put them
    back verbatim. A line we cannot read is still history, and dropping it to
    tidy up the file would be worse than carrying it along.
    """
    path = root / EVENTS_FILENAME
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []
    entries: list[tuple[str, dict | None]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Unparseable line in %s: %.80s", path, line)
            parsed = None
        entries.append((line, parsed if isinstance(parsed, dict) else None))
    return entries


def append_events(root: Path, events: list[dict]) -> None:
    """Append events to events.jsonl, one JSON object per line."""
    with open(root / EVENTS_FILENAME, "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def repair_event_counter(state: dict, root: Path) -> None:
    """Advance next_event_id past the highest id already in events.jsonl.

    The counter lives in state.json but the ids live in events.jsonl. Without
    this, a lost or restored state file would reissue ids that are already on
    disk and make `--acknowledge e12` ambiguous.

    The full-log scan is O(size of events.jsonl) on every poll; it stays cheap
    only because transitions are rare. Its cost is bounded by the same future
    log rotation that CLAUDE.md §"Gotchas / when editing" names as the bound
    for `--acknowledge`'s whole-file rewrite — consider the two together and
    never optimise one independently of the other. If rotation is ever
    implemented, this scan should read only the active shard while still
    advancing next_event_id past the highest id across all shards.
    """
    counter = state.get("next_event_id")
    if not isinstance(counter, int) or counter < 1:
        counter = 1
    highest = 0
    for _raw, event in load_events(root):
        event_id = event.get("id") if event else None
        if isinstance(event_id, str) and event_id.startswith("e"):
            try:
                highest = max(highest, int(event_id[1:]))
            except ValueError:
                continue
    state["next_event_id"] = max(counter, highest + 1)


def new_event(state: dict, kind: str, subject: str, detail: dict) -> dict:
    """Build one event envelope, consuming an id from the state counter."""
    counter = state.get("next_event_id", 1)
    state["next_event_id"] = counter + 1
    return {
        "id": f"e{counter}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "subject": subject,
        "detail": detail,
        "acknowledged": False,
    }


def write_pending(root: Path) -> int:
    """Rebuild pending.json from events.jsonl; returns the unacknowledged count.

    Always regenerated from the log rather than edited in place, so it can
    never disagree with the log it summarises and a consumer that deletes it
    gets it back on the next poll. `generated` is local time with an offset
    (this file is read by a human-facing agent); everything inside an event is
    UTC.
    """
    pending = [event for _raw, event in load_events(root)
               if event and not event.get("acknowledged")]
    write_json_atomic(root / PENDING_FILENAME, {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(pending),
        "events": pending,
    })
    return len(pending)


# --- Acknowledgement inbox -------------------------------------------------

ACK_INBOX_FILENAME = "ack-inbox.json"
ACK_PROCESSING_FILENAME = "ack-inbox.processing.json"

# read_ack_inbox statuses.
INBOX_MISSING = "missing"
INBOX_BAD = "bad"
INBOX_ALL = "all"
INBOX_OK = "ok"


def normalize_event_id(raw) -> str | None:
    """Coerce an inbox entry to an `e<N>` event id, or None if it is not one.

    The consumer writes whichever form is natural to it — 12, "12", "e12" — and
    all three mean the same event. Requiring the `e` prefix would make the file
    format depend on an internal detail of how ids are minted, and getting it
    wrong would fail silently as an unknown id.
    """
    if isinstance(raw, bool):  # bool is an int subclass; "true" is not an id
        return None
    if isinstance(raw, int):
        return f"e{raw}" if raw >= 1 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    digits = text[1:] if text.startswith("e") else text
    if not digits.isdigit() or int(digits) < 1:
        return None
    return f"e{int(digits)}"


def read_ack_inbox(path: Path) -> tuple[str, set[str]]:
    """Parse an inbox file into (status, ids); status names what was found.

    Returns one of INBOX_MISSING, INBOX_BAD, INBOX_ALL or INBOX_OK. Shared by
    the poller (which sets a bad file aside) and suppressed_ack_ids (which
    treats it as empty), so the two can never disagree about what a given file
    means.

    A document that parses but cannot be acted on — not an object, or an
    `acknowledge` value that is neither a list nor "all" — is BAD rather than
    empty: the consumer wrote something it believed was meaningful, and keeping
    the file is what makes that debuggable. Unusable *entries* within a valid
    list are a different matter and are dropped silently, since an id that no
    longer exists is the normal result of acknowledging twice.
    """
    if not path.exists():
        return INBOX_MISSING, set()
    try:
        document = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("Unreadable acknowledgement inbox %s: %s", path, exc)
        return INBOX_BAD, set()
    if not isinstance(document, dict):
        log.warning("Acknowledgement inbox %s is not an object", path)
        return INBOX_BAD, set()

    requested = document.get("acknowledge")
    if isinstance(requested, str):
        if requested.strip().lower() == INBOX_ALL:
            return INBOX_ALL, set()
        log.warning("Acknowledgement inbox %s: unknown request %r", path, requested)
        return INBOX_BAD, set()
    if not isinstance(requested, list):
        log.warning("Acknowledgement inbox %s: 'acknowledge' must be a list "
                    "of event ids or the string \"all\"", path)
        return INBOX_BAD, set()

    ids = {event_id for event_id in
           (normalize_event_id(entry) for entry in requested) if event_id}
    dropped = len(requested) - len(ids)
    if dropped > 0:
        log.debug("Acknowledgement inbox %s: ignored %d unusable entr(ies)",
                  path, dropped)
    return INBOX_OK, ids


def suppressed_ack_ids(root: Path) -> tuple[bool, set[str]]:
    """Ids the consumer has asked to acknowledge but the poller has not applied.

    Merges both inbox paths — the file a consumer just wrote and one a poll is
    part-way through draining — because exactly one of them exists at any
    instant during a drain, and a reader that checked only one could miss it.
    Returns (acknowledge_everything, ids); an unreadable file counts as empty
    here, since refusing to suppress is the safe direction for a reader.
    """
    ack_all = False
    ids: set[str] = set()
    for name in (ACK_INBOX_FILENAME, ACK_PROCESSING_FILENAME):
        status, found = read_ack_inbox(root / name)
        if status == INBOX_ALL:
            ack_all = True
        ids |= found
    return ack_all, ids


# --- macOS notification ----------------------------------------------------

def notify(title: str, message: str) -> None:
    """Best-effort macOS notification; never raises.

    Reserved for failures that stop the poll working at all. Network events go
    to pending.json for the consumer to report — notifying on those too would
    mean every incident arrives twice.
    """
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --- UniFi Network Integration API -----------------------------------------

class AuthError(RuntimeError):
    """Raised when the console rejects the API key (HTTP 401/403)."""


_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """TLS context with certificate verification disabled.

    A UniFi console serves its local API under a self-signed certificate issued
    to a LAN IP address, which no CA can vouch for, so verification cannot
    succeed however it is configured. It is disabled for this one pinned host:
    the request never leaves the local network, and the X-API-KEY header is
    what actually authenticates us to the console. Do not reuse this context
    for anything reached over the internet.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        _SSL_CONTEXT = context
    return _SSL_CONTEXT


_CONNECTIONS: dict[tuple[str, int], http.client.HTTPSConnection] = {}


def console_connection(host: str, timeout: int) -> http.client.HTTPSConnection:
    """The shared keep-alive connection to the console, created on first use.

    Every API call in a poll goes to the same host, so reusing one connection
    turns the poll's N TLS handshakes into one. The no-verify TLS context is
    the console-only one from `ssl_context` — this helper is its only outlet,
    so it can never leak to anything internet-reachable. A stale connection
    is never repaired in place: `_console_request` drops it and lets this
    remake it on the next call.
    """
    key = (host, timeout)
    connection = _CONNECTIONS.get(key)
    if connection is None:
        connection = http.client.HTTPSConnection(
            host, timeout=timeout, context=ssl_context())
        _CONNECTIONS[key] = connection
    return connection


def _console_request(host: str, path: str, api_key: str,
                     timeout: int) -> tuple[int, bytes]:
    """GET `path` (base + path + query) from the console; return (status, body).

    The body is always read in full so the shared connection returns to idle
    for the next call. A failure on an already-established connection means
    the keep-alive link was silently closed (by the console, or across a Mac
    sleep): drop it and retry once on a fresh one. A failure on a fresh
    connection means the console is down rather than stale, so it raises at
    once. Nothing is classified here — `api_get` and `legacy_api_get` map
    failures onto their own error contracts.
    """
    headers = {"X-API-KEY": api_key, "Accept": "application/json"}
    retried = False
    while True:
        connection = console_connection(host, timeout)
        established = connection.sock is not None
        try:
            connection.request("GET", path, headers=headers)
            with connection.getresponse() as response:
                return response.status, response.read()
        except (http.client.HTTPException, OSError):
            _CONNECTIONS.pop((host, timeout), None)
            connection.close()
            if retried or not established:
                raise
            retried = True


def api_get(host: str, api_key: str, path: str, params: dict | None = None,
            timeout: int = HTTP_TIMEOUT) -> dict:
    """GET one API path and return the decoded JSON object.

    Raises AuthError on 401/403 — a rejected key, which must abort the run
    before anything is written — and RuntimeError on every other failure. The
    console being unreachable is routine here rather than exceptional: the
    thing being monitored is also the thing answering the question.

    Goes through the shared keep-alive connection (see `console_connection`)
    rather than opening a fresh TLS connection per call.
    """
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        status, body = _console_request(host, url, api_key, timeout)
    except (http.client.HTTPException, OSError) as exc:
        # timeouts, refused connections and TLS errors all land here
        raise RuntimeError(f"{path} unreachable: {exc}") from exc
    if status in (401, 403):
        raise AuthError(f"HTTP {status} from {path}")
    if not 200 <= status < 300:
        raise RuntimeError(f"HTTP {status} from {path}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{path} returned {type(payload).__name__}, expected an object")
    return payload


def api_get_all(host: str, api_key: str, path: str) -> list[dict]:
    """Fetch every page of a collection endpoint and return the merged records.

    Collection responses are wrapped as
    {"offset":0,"limit":25,"count":25,"totalCount":63,"data":[…]} and the
    default page size is 25, so a network with more clients than that is
    silently truncated unless the offset is followed to the end. An empty page
    also ends the loop, so a console reporting a totalCount it cannot deliver
    costs one wasted request instead of spinning.
    """
    records: list[dict] = []
    offset = 0
    for _page in range(MAX_PAGES):
        payload = api_get(host, api_key, path,
                          {"offset": offset, "limit": PAGE_LIMIT})
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            break
        offset += len(data)
        records.extend(item for item in data if isinstance(item, dict))
        total = payload.get("totalCount")
        if not isinstance(total, int) or offset >= total:
            break
    else:
        log.warning("Stopped paginating %s after %d pages", path, MAX_PAGES)
    return records


# --- Time helpers ----------------------------------------------------------

def _minutes_since(iso: str | None, now: datetime) -> float | None:
    """Minutes between a stored UTC timestamp and now; None if unparseable."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 60.0


# --- Collector: devices ----------------------------------------------------

ONLINE = "ONLINE"
OFFLINE = "OFFLINE"


def _device_snapshot(device: dict, now_iso: str) -> dict:
    """The per-device state entry, as recorded on first sight."""
    return {
        "name": device.get("name") or device.get("id"),
        "model": device.get("model"),
        "state": device.get("state"),
        "state_since": now_iso,
        "offline_since": None,
        "offline_reported": False,
        "firmware_version": device.get("firmwareVersion"),
        "firmware_updatable": bool(device.get("firmwareUpdatable")),
        "ip_address": device.get("ipAddress"),
    }


def collect_devices(devices: list[dict], state: dict, default_minutes: float,
                    per_device: dict[str, float], now: datetime) -> list[dict]:
    """Report device up/down transitions and firmware changes.

    Offline is debounced: dropping offline only starts a timer, and the event
    is written once the device has been down longer than its threshold. A
    device that returns before then produces no event in either direction —
    a mesh AP that gets unplugged for ten minutes at a time and
    none of that is worth reporting.

    Devices are keyed by the API's device id, which survives a rename;
    thresholds are keyed by name, which does not.
    """
    now_iso = now.isoformat()
    known = state.get("devices")
    if known is None:
        state["devices"] = {device["id"]: _device_snapshot(device, now_iso)
                            for device in devices if device.get("id")}
        log.info("First run: seeded %d device(s), no events",
                 len(state["devices"]))
        return []

    events: list[dict] = []
    seen: set[str] = set()
    for device in devices:
        device_id = device.get("id")
        if not device_id:
            continue
        seen.add(device_id)
        name = device.get("name") or device_id
        prior = known.get(device_id)
        if prior is None:
            known[device_id] = _device_snapshot(device, now_iso)
            log.info("New device %s (%s); recorded without an event",
                     name, device_id)
            continue

        current = device.get("state")
        threshold = per_device.get(name, default_minutes)
        if current == OFFLINE:
            if prior.get("offline_since") is None:
                prior["offline_since"] = now_iso
                prior["offline_reported"] = False
                log.info("%s went offline; reporting if it stays down %g minute(s)",
                         name, threshold)
            elif not prior.get("offline_reported"):
                down = _minutes_since(prior.get("offline_since"), now)
                if down is not None and down >= threshold:
                    prior["offline_reported"] = True
                    events.append(new_event(state, "device_offline", name, {
                        "device_id": device_id,
                        "model": prior.get("model"),
                        "last_ip": prior.get("ip_address"),
                        "offline_since": prior.get("offline_since"),
                        "offline_minutes": round(down, 1),
                        "threshold_minutes": threshold,
                    }))
        elif current == ONLINE:
            if prior.get("offline_reported"):
                down = _minutes_since(prior.get("offline_since"), now)
                events.append(new_event(state, "device_online", name, {
                    "device_id": device_id,
                    "offline_since": prior.get("offline_since"),
                    "outage_minutes": round(down, 1) if down is not None else None,
                }))
            elif prior.get("offline_since"):
                log.info("%s came back before its %g minute threshold; no event",
                         name, threshold)
            prior["offline_since"] = None
            prior["offline_reported"] = False
        # Any other state (UPDATING, ADOPTING, …) is transitional: it neither
        # starts a timer nor clears one that is already running, so a firmware
        # update cannot fake an outage or mask one in progress.

        if current != prior.get("state"):
            prior["state"] = current
            prior["state_since"] = now_iso

        updatable = bool(device.get("firmwareUpdatable"))
        if updatable and not prior.get("firmware_updatable"):
            events.append(new_event(state, "firmware_available", name, {
                "device_id": device_id,
                "firmware_version": device.get("firmwareVersion"),
            }))
        prior["firmware_updatable"] = updatable

        version = device.get("firmwareVersion")
        if version and version != prior.get("firmware_version"):
            events.append(new_event(state, "firmware_updated", name, {
                "device_id": device_id,
                "old_version": prior.get("firmware_version"),
                "new_version": version,
            }))
            prior["firmware_version"] = version

        prior["name"] = name
        prior["model"] = device.get("model") or prior.get("model")
        prior["ip_address"] = device.get("ipAddress") or prior.get("ip_address")

    # An offline device is still listed by the API, so dropping out of the list
    # entirely means un-adopted or removed — a different problem, reported once
    # rather than on every poll for the rest of time.
    for device_id, entry in known.items():
        if device_id in seen:
            if entry.pop("absent", False):
                log.info("Device %s is listed by the API again", entry.get("name"))
        elif not entry.get("absent"):
            entry["absent"] = True
            log.warning("Device %s (%s) is no longer listed by the API — "
                        "un-adopted or removed, not offline",
                        entry.get("name"), device_id)
    return events


# --- Collector: clients ----------------------------------------------------

def collect_clients(clients: list[dict], devices: list[dict], state: dict,
                    now: datetime,
                    ignore_prefixes: tuple[str, ...] = ()) -> list[dict]:
    """Report clients whose MAC has never been seen on this network before.

    Keyed by MAC rather than by the API's client id, which is reassigned across
    reconnects; the known set only ever grows, so a device leaving and coming
    back is silence. The uplink device id is resolved to a name here because
    the consumer wants "joined through Express Mesh 1", not a UUID.

    A MAC matching one of `ignore_prefixes` is still added to the known set,
    marked `ignored`, but produces no event: removing the prefix later must
    not turn every MAC it had swallowed into an alert at once.
    """
    now_iso = now.isoformat()
    device_names = {device["id"]: (device.get("name") or device["id"])
                    for device in devices if device.get("id")}

    known = state.get("known_clients")
    if known is None:
        state["known_clients"] = {
            mac: {"name": client.get("name"), "type": client.get("type"),
                  "first_seen": now_iso}
            for client, mac in ((c, _client_mac(c)) for c in clients) if mac
        }
        log.info("First run: seeded %d known client MAC(s), no events",
                 len(state["known_clients"]))
        return []

    events: list[dict] = []
    for client in clients:
        mac = _client_mac(client)
        if not mac:
            log.debug("Client with no MAC address, skipped: %s", client.get("name"))
            continue
        if mac in known:
            continue
        uplink_id = client.get("uplinkDeviceId")
        known[mac] = {"name": client.get("name"), "type": client.get("type"),
                      "first_seen": now_iso}
        matched = next((p for p in ignore_prefixes if mac.startswith(p)), None)
        if matched:
            known[mac]["ignored"] = True
            log.info("Ignored new client %s via %s (prefix %s)", mac,
                     device_names.get(uplink_id, "unknown"), matched)
            continue
        events.append(new_event(state, "new_client", client.get("name") or mac, {
            "mac": mac,
            "ip_address": client.get("ipAddress"),
            "type": client.get("type"),
            "uplink_device": device_names.get(uplink_id, "unknown"),
            "uplink_device_id": uplink_id,
            "connected_at": client.get("connectedAt"),
        }))
    return events


def _client_mac(client: dict) -> str:
    """Normalise a client's MAC to lowercase; empty string when absent."""
    return str(client.get("macAddress") or "").strip().lower()


# --- Collector: WAN --------------------------------------------------------

UP = "up"
NO_ROUTE = "no_route"
DNS_FAILURE = "dns_failure"


def _ping() -> bool:
    """True if the ping target answers within three single-packet attempts.

    Three attempts because one dropped packet on a wireless uplink is not an
    outage, and declaring one would put a spurious gap in the event log.
    """
    for _attempt in range(3):
        try:
            proc = subprocess.run(
                [PING_BIN, "-c", "1", "-W", "3000", PING_TARGET],
                capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("Could not run %s: %s", PING_BIN, exc)
            return False
        if proc.returncode == 0:
            return True
    return False


def _dns_ok() -> bool:
    """True if a stable hostname resolves."""
    try:
        socket.getaddrinfo(DNS_PROBE_HOST, 443)
        return True
    except OSError:
        return False


def check_connectivity() -> str:
    """Classify internet reachability as up, no_route or dns_failure.

    Two probes because they fail for different reasons and want different
    responses: no_route is the line or the ISP, dns_failure is a working line
    with broken name resolution, which is usually a resolver setting and is
    fixable without calling anyone. If ping fails the DNS result says nothing
    useful, so no_route wins.
    """
    if not _ping():
        return NO_ROUTE
    return UP if _dns_ok() else DNS_FAILURE


def _is_public_address(address) -> bool:
    """True for an IP literal outside the private ranges.

    Deliberately "not private" rather than "globally routable": an ISP using
    carrier-grade NAT hands out a 100.64.0.0/10 address, which is not globally
    routable but is still the WAN address the console reports and the one worth
    recording when it changes. Every LAN address is RFC1918 and so excluded.
    """
    try:
        return not ipaddress.ip_address(str(address)).is_private
    except (ValueError, TypeError):
        return False


def find_gateway(devices: list[dict], configured_name: str | None) -> dict | None:
    """Pick the device whose ipAddress is the public WAN address.

    The gateway reports its WAN address in the same ipAddress field every other
    device uses for its LAN address, so "the one with a routable address" finds
    it without hardcoding a model. `gatewayName` in config.local.json overrides
    the heuristic for double-NAT setups, where no device has a public address.
    """
    if configured_name:
        for device in devices:
            if device.get("name") == configured_name:
                return device
        log.warning("gatewayName %r matches no device; falling back to the "
                    "public-address heuristic", configured_name)
    for device in devices:
        if _is_public_address(device.get("ipAddress")):
            return device
    return None


def collect_wan(devices: list[dict] | None, state: dict, now: datetime,
                gateway_name: str | None = None) -> list[dict]:
    """Report WAN address changes and internet outages.

    Runs even when the API call failed (devices is None) — the console being
    unreachable is exactly when connectivity is most interesting. Only the
    address half needs the device list; the probes do not.

    Reporting is retrospective by design: while the link is down there is
    nobody to tell, so down_since is persisted on every poll and the recovery
    event carries the whole window for the consumer to report after the fact.
    That also covers an install that happens during an outage — the first poll
    seeds the down state silently and recovery still reports the gap.
    """
    now_iso = now.isoformat()
    wan = state.get("wan")
    seeding = wan is None
    if seeding:
        wan = {"address": None, "connectivity": None,
               "down_since": None, "last_ok": None}
        state["wan"] = wan

    events: list[dict] = []
    if devices is not None:
        gateway = find_gateway(devices, gateway_name)
        address = gateway.get("ipAddress") if gateway else None
        if not gateway:
            log.debug("No device reports a public address; WAN address unchecked")
        elif address and address != wan.get("address"):
            if wan.get("address"):
                events.append(new_event(state, "wan_address_changed", "wan", {
                    "old_address": wan.get("address"),
                    "new_address": address,
                    "gateway": gateway.get("name"),
                }))
            wan["address"] = address

    status = check_connectivity()
    prior = wan.get("connectivity")
    if status == UP:
        if wan.get("down_since"):
            minutes = _minutes_since(wan["down_since"], now)
            events.append(new_event(state, "wan_recovered", "wan", {
                "down_since": wan["down_since"],
                "recovered_at": now_iso,
                "outage_minutes": round(minutes, 1) if minutes is not None else None,
                "mode": prior,
            }))
            log.info("WAN recovered after %.1f minute(s)", minutes or 0.0)
        wan["down_since"] = None
        wan["last_ok"] = now_iso
    elif wan.get("down_since") is None:
        wan["down_since"] = now_iso
        if not seeding:
            events.append(new_event(state, "wan_down", "wan", {"mode": status}))
        log.warning("WAN down (%s) as of %s", status, now_iso)
    elif prior != status:
        # Still one outage, so no second event — but the mode is worth a line.
        log.info("WAN still down; mode changed %s -> %s", prior, status)
    wan["connectivity"] = status
    return events


# --- Collector: stats (legacy API) -----------------------------------------

METRICS_DIRNAME = "metrics"
WIRED = "WIRED"
WIRELESS = "WIRELESS"

# Where a display name came from, best first. A client is identified by its MAC
# and only ever by its MAC; these rank the *label*, which is presentation.
NAME_ALIAS = "alias"        # someone named this device in the controller
NAME_HOSTNAME = "hostname"  # the device announced it — and can change its mind
NAME_MAC = "mac"            # nothing else was on offer
_NAME_RANKS = {NAME_ALIAS: 1, NAME_HOSTNAME: 2, NAME_MAC: 3}


def legacy_api_get(host: str, api_key: str, path: str,
                   timeout: int = HTTP_TIMEOUT) -> dict | None:
    """GET one legacy-API path, or None on any failure whatsoever.

    This deliberately does not reuse `api_get`. That function raises AuthError
    on 401 and `main` turns AuthError into exit 2 plus an "API key rejected"
    notification — but the legacy API answers **401** for a site name it does
    not recognise (`api.err.NoSiteContext`), not 404. Sharing the fetch would
    therefore let a typo in `stats.site` impersonate an expired key and abort a
    poll with nothing wrong with it. Nothing in here is allowed to propagate.

    What the two do share is the keep-alive connection (see
    `console_connection`) — the separation above is about error routing, not
    sockets.
    """
    url = f"{LEGACY_API_BASE}{path}"
    try:
        status, body = _console_request(host, url, api_key, timeout)
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — the legacy API may never break a poll
        log.warning("Legacy API %s unavailable: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        log.warning("Legacy API %s returned %s, expected an object",
                    path, type(payload).__name__)
        return None
    meta = payload.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("rc") != "ok":
        log.warning("Legacy API %s refused the request: rc=%r %s",
                    path, meta.get("rc"), meta.get("msg") or "")
        return None
    return payload


def fetch_client_stats(host: str, api_key: str,
                       site: str) -> list[dict] | None:
    """Fetch every currently connected client from the legacy stat/sta endpoint.

    Returns None when the call failed in any way at all, which the caller reads
    as "no statistics this poll" rather than as an error. A client that has
    disconnected simply stops being listed; that is the normal shape of this
    endpoint and not a signal of anything.
    """
    payload = legacy_api_get(host, api_key, f"/s/{site}/stat/sta")
    if payload is None:
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        log.warning("Legacy stat/sta returned no client list")
        return None
    return [record for record in data if isinstance(record, dict)]


def _as_int(raw):
    """Coerce an API value to int, or None when it is not a number."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _as_float(raw):
    """Coerce an API value to float, or None when it is not a number."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def name_rank(source) -> int:
    """Rank a display-name source, best first; an unknown one ranks as hostname.

    Public because `stats.py` ranks the same sources when it picks one label
    per MAC, and the two must agree. Rows written before `name_src` existed
    carry nothing, and a hostname is what they almost always were.
    """
    return _NAME_RANKS.get(source, _NAME_RANKS[NAME_HOSTNAME])


def _resolve_display_name(record: dict, mac: str) -> tuple[str, str]:
    """Pick a label for one client record: (name, which field it came from).

    The controller sets `name` only for devices someone has renamed by hand —
    1 client in 27 on this network — so `hostname` carries almost every label
    and the MAC is the last resort. One definition, used everywhere, because a
    label resolved two different ways is how one device becomes two.
    """
    alias = str(record.get("name") or "").strip()
    if alias:
        return alias, NAME_ALIAS
    hostname = str(record.get("hostname") or "").strip()
    if hostname:
        return hostname, NAME_HOSTNAME
    return mac, NAME_MAC


def _stabilise_name(prior: dict | None, candidate: str,
                    source: str) -> tuple[str, str, str | None]:
    """Hold a display name still across polls; returns (name, source, pending).

    A fixed precedence is not enough on its own, because the flapping is in the
    console rather than in the choice: this network has a client whose
    `hostname` alternated between two values across 18 of 56 polls, so the same
    device was written to the metrics file under two names. The MAC never
    moved, and nothing is keyed on the label — but a file that changes what it
    calls a device invites exactly the mistake of grouping by name.

    So the resolved name is remembered, and:

      - nothing stored yet, including a state.json written before this existed,
        adopts the candidate at once. That is what makes this need no migration.
      - a *better* source wins immediately: an alias is somebody's deliberate
        decision and should never wait on a confirmation.
      - the same name in different casing is not a rename; take the new casing
        and do not start a confirmation cycle over it.
      - anything else must appear on two consecutive polls before it replaces
        the stored name. Applying that to a *downgrade* too is what lets a
        genuinely deleted alias eventually fall back to the hostname rather
        than pinning a name the controller no longer knows about.
    """
    if not prior:
        return candidate, source, None
    stored = prior.get("name")
    if not isinstance(stored, str) or not stored:
        return candidate, source, None

    stored_source = prior.get("name_src")
    if stored_source not in _NAME_RANKS:
        stored_source = NAME_HOSTNAME
    if _NAME_RANKS[source] < _NAME_RANKS[stored_source]:
        return candidate, source, None
    if candidate.casefold() == stored.casefold():
        return candidate, source, None

    if prior.get("name_pending") == candidate:
        log.debug("Display name for %s settled on %r (was %r)",
                  prior.get("name"), candidate, stored)
        return candidate, source, None
    return stored, stored_source, candidate


def _normalise_client(record: dict) -> tuple[dict, int, int] | None:
    """Flatten one legacy client record; returns (row, rx_total, tx_total).

    The legacy API names the byte counters differently per client type —
    `rx_bytes`/`tx_bytes` on wireless, `wired-rx_bytes`/`wired-tx_bytes` on
    wired — and each type carries whole fields the other lacks: switch mac and
    port on wired, signal and channel on wireless. Neither set is assumed
    present. A record with no counter at all still produces a row, marked
    `partial`, because a client we can see but cannot measure is worth
    recording as exactly that rather than as an absence.

    The label comes from `_resolve_display_name` and is *observed*, not final:
    `collect_stats` may keep an earlier one. Returns None only when there is no
    MAC — the one field everything downstream is keyed by, and the only thing
    here that identifies a client at all.
    """
    mac = str(record.get("mac") or "").strip().lower()
    if not mac:
        return None
    wired = bool(record.get("is_wired"))

    if wired:
        rx = _as_int(record.get("wired-rx_bytes"))
        tx = _as_int(record.get("wired-tx_bytes"))
        link_mbps = _as_int(record.get("wired_rate_mbps"))
    else:
        rx = _as_int(record.get("rx_bytes"))
        tx = _as_int(record.get("tx_bytes"))
        # tx_rate is the negotiated PHY rate in kbps — the speed the radio
        # agreed on, not the throughput achieved. Recorded in Mbps so it is
        # the same unit as the wired side.
        rate = _as_int(record.get("tx_rate"))
        link_mbps = rate // 1000 if rate else None

    name, name_src = _resolve_display_name(record, mac)
    row = {
        "mac": mac,
        "name": name,
        "name_src": name_src,
        "type": WIRED if wired else WIRELESS,
        "rx_delta": 0,   # filled in by collect_stats, kept here for field order
        "tx_delta": 0,
        "satisfaction": _as_int(record.get("satisfaction")),
        "link_mbps": link_mbps,
        "uplink": record.get("last_uplink_name"),
        "sw_mac": record.get("sw_mac"),
        "sw_port": _as_int(record.get("sw_port")),
        # The console reports this directly; it is a recent-window sample and
        # is NOT tx_retries/wifi_tx_attempts, which are lifetime counters and
        # give a materially different number.
        "tx_retries_pct": _as_float(record.get("wifi_tx_retries_percentage")),
        "satisfaction_reason": _as_int(record.get("satisfaction_reason")),
        # dev_vendor and os_name are fingerprint-database ids, not names; oui
        # is the readable vendor string and is what makes them usable.
        "dev_vendor": _as_int(record.get("dev_vendor")),
        "os_name": _as_int(record.get("os_name")),
        "oui": record.get("oui"),
        "signal": _as_int(record.get("signal")),
        "noise": _as_int(record.get("noise")),
        "channel": _as_int(record.get("channel")),
        "radio_proto": record.get("radio_proto"),
        "essid": record.get("essid"),
    }
    if rx is None and tx is None:
        row["partial"] = True
    return row, rx or 0, tx or 0


def _counter_delta(previous, current: int) -> tuple[int, bool]:
    """Bytes moved since the last poll, and whether the counter had reset.

    The counters are cumulative for the life of a client's *session* and start
    again at zero when it reconnects. Summing them naively measures uptime
    rather than usage, and subtracting naively goes negative across a
    reconnect, so:

      - no previous value at all is a **first sighting**: the delta is zero and
        the counter is merely recorded. Reporting it would attribute a week of
        accumulated traffic to a single five-minute poll and put a false spike
        in every chart on the day the collector was installed.
      - a current value **below** the stored one can only mean a new session,
        so the whole of it is this interval's traffic.
    """
    if current < 0:
        return 0, False
    if previous is None:
        return 0, False
    if current >= previous:
        return current - previous, False
    return current, True


def collect_stats(records: list[dict], state: dict, now: datetime) -> dict:
    """Turn a legacy client list into one metrics document, updating baselines.

    Touches no files — the delta arithmetic can therefore be driven from a test
    harness with no console and no root directory. Baselines live in
    `state["stats"]`, keyed by MAC and kept clear of the monitoring keys beside
    them so a change here can never disturb device or client history.

    **A client is its MAC.** Baselines, rows and every downstream aggregation
    key on it and nothing else. The display name is a label: it is resolved
    once here, held still by `_stabilise_name`, and never used to group, count
    or key anything. Both directions of that go wrong in real data — one device
    on this network reported two different hostnames across a single evening,
    and seven separate speakers all call themselves `SonosZP`.

    Null fields are dropped from each emitted row: wired clients carry no RF
    data and wireless ones no switch port, and writing both sets of nulls for
    every client would inflate a file that already grows by megabytes a day.
    """
    baselines = state.get("stats")
    baselines = baselines if isinstance(baselines, dict) else {}
    state["stats"] = baselines
    now_iso = now.isoformat()

    clients: list[dict] = []
    resets = 0
    renamed = 0
    for record in records:
        normalised = _normalise_client(record)
        if normalised is None:
            log.debug("Legacy client record with no MAC, skipped")
            continue
        row, rx_total, tx_total = normalised
        mac = row["mac"]
        prior = baselines.get(mac)
        prior = prior if isinstance(prior, dict) else None
        row["rx_delta"], rx_reset = _counter_delta(
            _as_int(prior.get("rx")) if prior else None, rx_total)
        row["tx_delta"], tx_reset = _counter_delta(
            _as_int(prior.get("tx")) if prior else None, tx_total)
        if rx_reset or tx_reset:
            resets += 1
            log.debug("%s reconnected; counters restarted", mac)

        name, name_src, pending = _stabilise_name(prior, row["name"],
                                                  row["name_src"])
        if name != row["name"]:
            renamed += 1
        row["name"], row["name_src"] = name, name_src

        entry = {"rx": rx_total, "tx": tx_total, "seen": now_iso,
                 "name": name, "name_src": name_src}
        if pending is not None:
            entry["name_pending"] = pending
        baselines[mac] = entry
        clients.append({key: value for key, value in row.items()
                        if value is not None})

    if resets:
        log.info("%d client(s) reconnected since the last poll", resets)
    if renamed:
        log.debug("%d client(s) kept an earlier display name this poll", renamed)
    return {"ts": now_iso, "clients": clients}


def _prune_baselines(baselines: dict, retain_days: int, now: datetime) -> int:
    """Drop counter baselines for clients not seen inside the retention window.

    A baseline older than the metrics it would ever feed is dead weight, and
    without this every device that has ever joined — a visitor's phone, a
    neighbour's guest session — stays in state.json for good. Entries with an
    unreadable `seen` stamp are kept: losing a live client's baseline would
    make its next poll look like a first sighting and silently drop an
    interval of traffic.
    """
    horizon_minutes = retain_days * 24 * 60
    stale = []
    for mac, entry in baselines.items():
        if not isinstance(entry, dict):
            continue
        age = _minutes_since(entry.get("seen"), now)
        if age is not None and age > horizon_minutes:
            stale.append(mac)
    for mac in stale:
        del baselines[mac]
    if stale:
        log.info("Dropped %d stale client baseline(s)", len(stale))
    return len(stale)


def write_metrics(root: Path, document: dict, now: datetime) -> Path:
    """Append one poll's metrics to <root>/metrics/YYYY-MM-DD.jsonl.

    Appended rather than rewritten: a day's file is a time series and every
    line is final the moment it lands. Unlike `append_events` this does not
    fsync — an event is a fact that must survive a crash, whereas a lost
    metrics line is one missing sample in a trend and not worth a disk flush
    every five minutes.
    """
    directory = root / METRICS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.strftime('%Y-%m-%d')}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, separators=(",", ":")) + "\n")
    return path


def prune_metrics(root: Path, retain_days: int, now: datetime) -> int:
    """Delete metrics files past the retention horizon; returns how many went.

    The date comes from the **filename**, not the mtime. Appending moves an
    mtime every five minutes, so an mtime horizon would measure when a file was
    last written rather than which day it covers, and a file restored from a
    backup would carry a timestamp with nothing to do with its contents. A name
    that does not parse is left alone rather than guessed at.
    """
    directory = root / METRICS_DIRNAME
    if not directory.is_dir():
        return 0
    horizon = (now - timedelta(days=retain_days)).date()
    removed = 0
    for path in sorted(directory.glob("*.jsonl")):
        try:
            covered = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            log.debug("Ignoring unrecognised metrics filename %s", path.name)
            continue
        if covered >= horizon:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)
    if removed:
        log.info("Pruned %d metrics file(s) older than %d day(s)",
                 removed, retain_days)
    return removed


def record_stats(root: Path, records: list[dict], state: dict,
                 retain_days: int, now: datetime) -> int:
    """Write one metrics line, prune the history, return the clients recorded."""
    document = collect_stats(records, state, now)
    write_metrics(root, document, now)
    _prune_baselines(state["stats"], retain_days, now)
    prune_metrics(root, retain_days, now)
    return len(document["clients"])


# --- Acknowledge -----------------------------------------------------------

def _apply_acknowledgement(root: Path,
                           wanted: set[str] | None) -> tuple[int, set[str], int]:
    """Mark events acknowledged and regenerate pending.json; the lock must be held.

    wanted=None acknowledges everything outstanding. Returns (events changed,
    ids that matched a line, events still pending). Shared by the CLI and the
    inbox so there is one definition of what acknowledging means; they differ
    only in how they report an id that matched nothing.

    The log is rewritten in full rather than appended to, because
    acknowledgement corrects a fact already on disk; the rewrite is atomic and
    lines that could not be parsed are written back byte for byte. When nothing
    changed the rewrite is skipped entirely, which is what makes re-applying the
    same inbox a genuine no-op rather than a no-op-shaped rewrite.
    """
    entries = load_events(root)
    if not entries:
        log.info("No events recorded yet; nothing to acknowledge")
        return 0, set(), write_pending(root)

    matched: set[str] = set()
    changed = 0
    lines: list[str] = []
    for raw, event in entries:
        event_id = event.get("id") if event else None
        targeted = wanted is None or (event_id in wanted)
        if event and targeted:
            matched.add(event_id)
            if not event.get("acknowledged"):
                event["acknowledged"] = True
                changed += 1
                lines.append(json.dumps(event))
                continue
        lines.append(raw)

    if changed:
        path = root / EVENTS_FILENAME
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)
    return changed, matched, write_pending(root)


def acknowledge(root: Path, event_ids: list[str] | None) -> int:
    """Acknowledge event ids from the command line; None means all of them.

    Acknowledging everything also clears any inbox a consumer has queued: the
    two paths must not be able to disagree, and after this runs the queued ids
    are acknowledged anyway. A targeted --acknowledge deliberately leaves the
    inbox alone, since it may name ids this invocation did not.
    """
    wanted = set(event_ids) if event_ids else None
    with run_lock(root):
        changed, matched, remaining = _apply_acknowledgement(root, wanted)
        if wanted is None:
            clear_ack_inbox(root)

    log.info("Acknowledged %d event(s); %d still pending", changed, remaining)
    unknown = sorted(wanted - matched) if wanted else []
    if unknown:
        log.error("No such event id(s): %s", ", ".join(unknown))
        return 1
    return 0


def clear_ack_inbox(root: Path) -> None:
    """Remove both inbox files; used when the CLI has acknowledged everything."""
    for name in (ACK_INBOX_FILENAME, ACK_PROCESSING_FILENAME):
        path = root / name
        try:
            path.unlink()
            log.info("Removed %s (superseded by --acknowledge-all)", name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Could not remove %s: %s", path, exc)


def _set_aside_bad_inbox(path: Path, now: datetime) -> None:
    """Rename an unusable inbox out of the way so it cannot block the next one."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    spoiled = path.with_name(f"ack-inbox.bad-{stamp}.json")
    try:
        os.replace(path, spoiled)
        log.warning("Set aside unusable acknowledgement inbox as %s", spoiled.name)
    except OSError as exc:
        log.error("Could not set aside %s: %s", path, exc)


def process_ack_inbox(root: Path, now: datetime) -> int:
    """Drain <root>/ack-inbox.json into events.jsonl; returns the ids applied.

    This exists because the consumer is a sandboxed agent that cannot execute
    anything: writing a file is the only channel it has, so acknowledgement has
    to be a file rather than a command.

    A leftover ack-inbox.processing.json is drained *before* the new inbox is
    renamed into place. That file means a previous run died between the rename
    and the apply, and renaming onto it would destroy the ids it holds.

    The rename is what makes this safe to run against a live consumer: it is
    atomic, so a consumer writing a fresh inbox while a poll is in flight lands
    on a clean path and is picked up by the next run instead of being half-read
    or silently overwritten.
    """
    applied = 0
    processing = root / ACK_PROCESSING_FILENAME
    inbox = root / ACK_INBOX_FILENAME

    if processing.exists():
        log.info("Resuming an acknowledgement inbox left over from a previous run")
        applied += _drain_ack_file(root, processing, now)

    if not inbox.exists():
        return applied
    try:
        os.replace(inbox, processing)
    except OSError as exc:
        log.error("Could not claim %s: %s", inbox, exc)
        return applied
    return applied + _drain_ack_file(root, processing, now)


def _drain_ack_file(root: Path, processing: Path, now: datetime) -> int:
    """Apply one claimed inbox file, then remove it; returns the ids applied.

    The file is removed *after* pending.json has been regenerated, not before.
    In between, a reader subtracts these ids from pending.json via
    suppressed_ack_ids — so deleting first would leave a window where the ids
    are gone from the inbox but still listed as pending, and a consumer waking
    in that window would report them a second time. That double report is the
    whole reason this feature exists, so the ordering is load-bearing.
    """
    status, ids = read_ack_inbox(processing)
    if status == INBOX_BAD:
        _set_aside_bad_inbox(processing, now)
        return 0
    if status == INBOX_MISSING:
        return 0
    if status == INBOX_OK and not ids:
        log.info("Acknowledgement inbox named no usable event ids")

    with run_lock(root):
        wanted = None if status == INBOX_ALL else ids
        if status == INBOX_ALL or ids:
            changed, matched, remaining = _apply_acknowledgement(root, wanted)
            # Unknown and already-acknowledged ids are not errors here: the
            # consumer is entitled to replay an inbox, and doing so must be a
            # no-op rather than a complaint it has no way to read.
            log.info("Inbox acknowledged %d event(s) of %s requested; "
                     "%d still pending", changed,
                     "all" if wanted is None else len(ids), remaining)
            applied = len(matched)
        else:
            applied = 0
        try:
            processing.unlink()
        except OSError as exc:
            log.error("Could not remove %s after applying it: %s", processing, exc)
    return applied


# --- Main ------------------------------------------------------------------

def init_logging(root: Path) -> None:
    """Log to stdout and <log_dir>/unifiwatch.log; shared by every entry point."""
    log_dir = load_log_dir(root)
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_dir / "unifiwatch.log")],
    )


def main() -> int:
    root = load_root()
    init_logging(root)
    log.info("UniFiWatch poll starting; root=%s", root)

    # Drain the acknowledgement inbox first, before the config check and before
    # any API call. It is a purely local file operation that owes nothing to the
    # console, and running it here means a rejected key — which returns 2 below
    # and would otherwise do so on every poll forever — can never leave the
    # consumer with no way to acknowledge anything.
    #
    # This must NOT move inside the run_lock block further down: run_lock opens
    # a fresh descriptor each call and flock is per open-file-description, so a
    # nested acquire from this same process would block forever.
    try:
        process_ack_inbox(root, datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 — a bad inbox must not abort the poll
        log.error("Acknowledgement inbox failed: %s", exc)

    host, api_key, site_id = load_api_settings()
    missing = [key for key, value in
               (("host", host), ("apiKey", api_key), ("siteId", site_id))
               if not value]
    if missing:
        message = f"Missing {', '.join(missing)} — see {CONFIG_FILE}"
        log.error("Incomplete configuration: %s", message)
        notify("UniFiWatch config error", message)
        return 1

    # Fetch everything before touching state. An auth failure must leave the
    # root exactly as it was, so that a rejected key can never be recorded as
    # a network that suddenly has no devices and no clients.
    devices = clients = None
    fetch_failed = False
    try:
        devices = api_get_all(host, api_key, f"/sites/{site_id}/devices")
        clients = api_get_all(host, api_key, f"/sites/{site_id}/clients")
        log.info("Fetched %d device(s), %d client(s)", len(devices), len(clients))
    except AuthError as exc:
        message = "API key rejected — check apiKey in config.local.json."
        log.error("%s (%s)", message, exc)
        notify("UniFiWatch auth failed", message)
        return 2
    except RuntimeError as exc:
        fetch_failed = True
        log.error("UniFi API unavailable: %s", exc)

    # Statistics come from the unofficial legacy API, so they are fetched apart
    # from the official calls above and by a function that cannot raise: see
    # legacy_api_get for why a shared fetch would let a bad site name look
    # exactly like a rejected key.
    stats_enabled, retain_days, stats_site = load_stats_settings()
    stats_records = None
    if stats_enabled:
        try:
            stats_records = fetch_client_stats(host, api_key, stats_site)
        except Exception as exc:  # noqa: BLE001 — statistics may never break a poll
            log.error("Statistics fetch failed: %s", exc)

    default_minutes, per_device = load_thresholds()
    ignore_prefixes = load_client_ignores()
    gateway_name = _read_config().get("gatewayName")
    now = datetime.now(timezone.utc)
    events: list[dict] = []
    errors = 1 if fetch_failed else 0

    with run_lock(root):
        state = load_state(root)
        repair_event_counter(state, root)

        # Each collector is independent and returns events; adding a new check
        # means adding a function and one line here.
        collectors = []
        if devices is not None and clients is not None:
            collectors.append(("devices", lambda: collect_devices(
                devices, state, default_minutes, per_device, now)))
            collectors.append(("clients", lambda: collect_clients(
                clients, devices, state, now, ignore_prefixes)))
        # The WAN collector runs whether or not the API answered: its probes are
        # local, and an unreachable console is when they matter most.
        collectors.append(("wan", lambda: collect_wan(
            devices, state, now, gateway_name)))

        for name, collector in collectors:
            try:
                events.extend(collector())
            except Exception as exc:  # noqa: BLE001 — one collector must not abort the poll
                errors += 1
                log.error("Collector %s failed: %s", name, exc)

        if events:
            append_events(root, events)
            for event in events:
                log.info("Event %s %s: %s", event["id"], event["kind"],
                         event["subject"])
        # Regenerate on a quiet poll only to replace a file a consumer removed;
        # rewriting it every five minutes would churn its mtime for nothing.
        if events or not (root / PENDING_FILENAME).exists():
            pending = write_pending(root)
            log.info("%d new event(s); %d unacknowledged", len(events), pending)

        # Statistics run last, and deliberately outside the collector list
        # above: that list feeds `errors`, and a failure here must never change
        # the poll's exit code. By this point events.jsonl and pending.json are
        # already written and durable, and nothing below touches either.
        if stats_records is not None:
            try:
                recorded = record_stats(root, stats_records, state,
                                        retain_days, now)
                log.info("Recorded statistics for %d client(s)", recorded)
            except Exception as exc:  # noqa: BLE001 — statistics may never break a poll
                log.error("Statistics collector failed: %s", exc)

        save_state(root, state)

    if fetch_failed and (state.get("wan") or {}).get("connectivity") == UP:
        log.error("The console did not answer but the internet is up — "
                  "check the UniFi host, not the line")
    log.info("Done: %d new event(s), %d error(s)", len(events), errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    if "--acknowledge-all" in sys.argv[1:]:
        _root = load_root()
        init_logging(_root)
        log.info("UniFiWatch acknowledge-all starting; root=%s", _root)
        sys.exit(acknowledge(_root, None))
    if "--acknowledge" in sys.argv[1:]:
        _ids = [arg for arg in sys.argv[sys.argv.index("--acknowledge") + 1:]
                if not arg.startswith("-")]
        if not _ids:
            print("usage: watch.py --acknowledge <event_id> [<event_id> …]",
                  file=sys.stderr)
            sys.exit(1)
        _root = load_root()
        init_logging(_root)
        log.info("UniFiWatch acknowledge starting; root=%s ids=%s",
                 _root, ",".join(_ids))
        sys.exit(acknowledge(_root, _ids))
    sys.exit(main())
