#!/usr/bin/env python3
"""UniFiWatch status — a quick health/summary readout.

Shows what the last poll saw: devices up and down, known clients, the WAN
address and connectivity, the event tally, and whether the five-minute launchd
agent is loaded. Read-only: it never calls the UniFi API and never writes
anything.

    python3 status.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone

import watch  # reuse the same config/state resolution as the poller

LABEL = "com.jztone.unifiwatch"


def local_str(iso: str | None) -> str:
    """Render a stored UTC timestamp as local time plus a relative age."""
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - then
    minutes = delta.total_seconds() / 60
    if minutes < 90:
        ago = f"{minutes:.0f}m ago"
    elif minutes < 60 * 48:
        ago = f"{minutes/60:.1f}h ago"
    else:
        ago = f"{minutes/1440:.1f}d ago"
    return f"{then.astimezone().strftime('%Y-%m-%d %H:%M')} ({ago})"


def agent_loaded() -> str:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "not loaded (run ./install.sh)"
        for line in result.stdout.splitlines():
            if "state = " in line:
                return f"loaded, {line.split('state = ')[1].strip()}"
        return "loaded"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def device_line(state: dict) -> str:
    """Online/offline tally, naming whatever is currently down."""
    devices = state.get("devices")
    if not devices:
        return "⚠ none recorded yet — run: python3 watch.py"
    online = [d for d in devices.values() if d.get("state") == watch.ONLINE]
    down = [d for d in devices.values() if d.get("state") == watch.OFFLINE]
    other = len(devices) - len(online) - len(down)
    line = f"{len(online)} online · {len(down)} offline"
    if other:
        line += f" · {other} in another state"
    if down:
        names = ", ".join(sorted(str(d.get("name")) for d in down))
        line += f"   ⚠ {names}"
    return line


def wan_line(state: dict) -> str:
    """WAN address plus connectivity, and how long it has been down."""
    wan = state.get("wan")
    if not wan:
        return "⚠ not checked yet — run: python3 watch.py"
    parts = [str(wan.get("address") or "address unknown")]
    status = wan.get("connectivity") or "unknown"
    parts.append(status if status == watch.UP else f"⚠ {status}")
    if wan.get("down_since"):
        parts.append(f"down since {local_str(wan['down_since'])}")
    return " · ".join(parts)


def events_line(root) -> tuple[str, dict | None]:
    """Total/unacknowledged event counts, and the pending document itself."""
    total = len(watch.load_events(root))
    path = root / watch.PENDING_FILENAME
    if not path.exists():
        return f"{total} recorded · ⚠ no pending.json — run: python3 watch.py", None
    try:
        pending = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return f"{total} recorded · ⚠ unreadable pending.json ({exc})", None
    count = pending.get("count", 0)
    flag = "⚠ " if count else ""
    return (f"{total} recorded · {flag}{count} unacknowledged "
            f"· {local_str(pending.get('generated'))}"), pending


def inbox_line(root) -> str:
    """Whether a consumer has queued acknowledgements the next poll will apply."""
    parts = []
    for name, when in ((watch.ACK_INBOX_FILENAME, "queued for the next poll"),
                       (watch.ACK_PROCESSING_FILENAME, "mid-drain")):
        status, ids = watch.read_ack_inbox(root / name)
        if status == watch.INBOX_MISSING:
            continue
        if status == watch.INBOX_BAD:
            parts.append("⚠ unreadable — the next poll will set it aside as "
                         "ack-inbox.bad-*.json")
        elif status == watch.INBOX_ALL:
            parts.append(f'"all" {when}')
        else:
            parts.append(f"{len(ids)} id(s) {when}")
    return " · ".join(parts) if parts else "empty"


def main() -> int:
    # Surface the warnings watch.load_events emits for unparseable lines rather
    # than letting them vanish through the unconfigured root logger.
    logging.basicConfig(level=logging.WARNING, format="  ⚠ %(message)s")

    root = watch.load_root()
    host, api_key, site_id = watch.load_api_settings()
    state = watch.load_state(root)
    events, pending = events_line(root)

    print("UniFiWatch status")
    print("─" * 60)
    print(f"  Root           : {root}")
    # The API key is deliberately never printed, only whether one is configured.
    print(f"  Console        : {host or '⚠ unset'}"
          f" · site {site_id or '⚠ unset'}"
          f" · key {'configured' if api_key else '⚠ missing'}")
    print(f"  Last poll      : {local_str(state.get('last_run'))}")
    print(f"  Agent          : {agent_loaded()}")
    print(f"  Devices        : {device_line(state)}")
    print(f"  Known clients  : {len(state.get('known_clients') or {})}")
    print(f"  WAN            : {wan_line(state)}")
    print(f"  Events         : {events}")
    print(f"  Ack inbox      : {inbox_line(root)}")

    if pending and pending.get("events"):
        print("\n  Unacknowledged:")
        for event in pending["events"][:5]:
            print(f"    {event.get('id'):<5} {local_str(event.get('ts')):<26} "
                  f"{event.get('kind')}  {event.get('subject')}")
        if pending["count"] > 5:
            print(f"    … and {pending['count'] - 5} more")
        print("\n  Clear them with: python3 watch.py --acknowledge-all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
