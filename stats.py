#!/usr/bin/env python3
"""UniFiWatch statistics — reports over the per-client metrics history.

Reads only `<root>/metrics/YYYY-MM-DD.jsonl`, which the poller appends to on
every run. Makes no API calls and writes nothing, so it is safe to run at any
moment, including while a poll is in flight.

    python3 stats.py --top                  # busiest clients, last 7 days
    python3 stats.py --top 20 --days 30
    python3 stats.py --client Mac --days 14 # daily totals for one client
    python3 stats.py --links                # wired link speeds, now
    python3 stats.py --quality              # worst average satisfaction

Add `--json` to any of them for machine-readable output.

**Direction.** The console names byte counters from the network's point of
view, so a client's `tx_delta` is what the network sent *to* it — the client's
download — and `rx_delta` is its upload. Reports here are labelled from the
client's side, because that is what anyone asking "who is using the bandwidth?"
means.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import date, datetime, timedelta

import watch  # reuse the same root resolution as the poller

GIGABIT_MBPS = 1000
DEFAULT_TOP = 10
DEFAULT_DAYS = 7


# --- Reading the history ---------------------------------------------------

def metrics_files(root, days: int, today: date) -> list:
    """Metrics files covering the last `days` calendar days, oldest first.

    Selected by the date in the filename rather than by mtime, matching how
    `prune_metrics` decides what to delete — the two must agree about which day
    a file belongs to or the reporting window and the retention window drift
    apart.
    """
    directory = root / watch.METRICS_DIRNAME
    if not directory.is_dir():
        return []
    horizon = today - timedelta(days=days - 1)
    chosen = []
    for path in directory.glob("*.jsonl"):
        try:
            covered = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if covered >= horizon:
            chosen.append((covered, path))
    return [path for _covered, path in sorted(chosen)]


def iter_polls(paths, warn: bool = True):
    """Yield every poll document across the given files, in order.

    Streams instead of materialising the window: a 90-day history parses to
    several hundred MB of dicts, and no report needs more than one poll at a
    time — `resolve_labels` must see every row before choosing labels, but it
    keeps only the per-MAC label state. Each pass re-reads and re-parses the
    files; that CPU is the price of keeping a wide window off the heap.

    A line that will not parse is skipped with a warning rather than aborting:
    the file is append-only and a truncated final line is the expected shape of
    a machine that lost power mid-write. Later passes re-read the same files,
    so they pass warn=False rather than report the same skipped line again.
    """
    for path in paths:
        try:
            handle = path.open(encoding="utf-8")
        except OSError as exc:
            if warn:
                print(f"stats: cannot read {path}: {exc}", file=sys.stderr)
            continue
        with handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    if warn:
                        print(f"stats: unparseable line {path.name}:{number}, "
                              "skipped", file=sys.stderr)
                    continue
                if isinstance(document, dict) and isinstance(
                        document.get("clients"), list):
                    yield document


def iter_rows(polls):
    """Yield (poll timestamp, client row) for every client in every poll."""
    for poll in polls:
        timestamp = poll.get("ts")
        for row in poll["clients"]:
            if isinstance(row, dict) and row.get("mac"):
                yield timestamp, row


def normalise_mac(text: str) -> str:
    """Lowercase a MAC and accept `-` as a separator as well as `:`."""
    return text.strip().lower().replace("-", ":")


def resolve_labels(polls) -> tuple[dict[str, str], int]:
    """One display label per MAC, disambiguated where two MACs share a name.

    Every report groups by MAC — a label is only ever printed, never keyed on.
    But a MAC can still carry more than one label across a window, because the
    console can change its mind about a client's hostname, so the label to
    print has to be *chosen* rather than taken from whichever row happened to
    be read last.

    The best source seen wins, and the most recent row at that source breaks
    the tie. That means a name someone set in the controller beats a hostname
    however often the hostname appeared — for the client that prompted this,
    picking the most *frequent* name would have printed a hostname it stopped
    using hours earlier. Rows written before `name_src` existed are ranked as
    hostnames, which is what they almost always were, so old files resolve
    correctly with no migration.

    Then the reverse problem: seven separate speakers here all report
    `SonosZP`, and seven identical rows are unreadable. Any label held by more
    than one MAC gets the last two octets appended; unique labels are left
    alone.

    Takes a one-shot iterator (see `iter_polls`) and walks it inline rather
    than via `iter_rows` so the same single pass can also count the polls —
    callers need the count and a pass just for it would re-parse the whole
    window. Returns (labels, poll count).
    """
    best: dict[str, tuple[tuple[int, int], str]] = {}
    poll_count = 0
    order = 0
    for poll in polls:
        poll_count += 1
        for row in poll["clients"]:
            if not isinstance(row, dict) or not row.get("mac"):
                continue
            mac = row["mac"]
            # Better source first (so negated, since larger wins), then later.
            key = (-watch.name_rank(row.get("name_src")), order)
            order += 1
            if mac not in best or key > best[mac][0]:
                best[mac] = (key, str(row.get("name") or mac))

    labels = {mac: entry[1] for mac, entry in best.items()}
    shared = collections.Counter(labels.values())
    return ({mac: (f"{name} ({mac[-5:]})" if shared[name] > 1 else name)
             for mac, name in labels.items()}, poll_count)


# --- Formatting ------------------------------------------------------------

def human_bytes(count) -> str:
    """Render a byte count in binary units, three significant figures."""
    try:
        value = float(count)
    except (ValueError, TypeError):
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,.0f} B"
        value /= 1024
    return f"{value:,.1f} TiB"


def day_of(timestamp) -> str:
    """The calendar day of a poll timestamp, or '?' when it is unreadable."""
    try:
        return datetime.fromisoformat(str(timestamp)).date().isoformat()
    except (ValueError, TypeError):
        return "?"


def emit(payload: dict, lines: list[str], as_json: bool) -> int:
    """Print either the JSON document or the prepared text, and return 0."""
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(lines))
    return 0


def window_note(paths, days: int) -> str:
    """A human description of the window actually covered by the files read."""
    if not paths:
        return f"last {days} day(s) · no data"
    return (f"last {days} day(s) · {paths[0].stem} → {paths[-1].stem} "
            f"· {len(paths)} file(s)")


# --- Reports ---------------------------------------------------------------

def report_top(paths, days: int, limit: int, as_json: bool) -> int:
    """Busiest clients over the window, by total bytes in both directions."""
    labels, poll_count = resolve_labels(iter_polls(paths))
    totals: dict[str, dict] = {}
    for _ts, row in iter_rows(iter_polls(paths, warn=False)):
        entry = totals.setdefault(row["mac"], {
            "mac": row["mac"], "name": labels[row["mac"]],
            "type": row.get("type"), "down": 0, "up": 0, "samples": 0})
        entry["down"] += row.get("tx_delta") or 0
        entry["up"] += row.get("rx_delta") or 0
        entry["samples"] += 1

    ranked = sorted(totals.values(), key=lambda e: e["down"] + e["up"],
                    reverse=True)
    grand = sum(entry["down"] + entry["up"] for entry in ranked)
    for entry in ranked:
        entry["total"] = entry["down"] + entry["up"]
        entry["share_pct"] = round(100 * entry["total"] / grand, 1) if grand else 0.0

    payload = {"window_days": days, "files": [p.name for p in paths],
               "polls": poll_count, "total_bytes": grand,
               "clients": ranked[:limit]}
    lines = [f"Top {limit} clients by traffic · {window_note(paths, days)}",
             f"{poll_count} poll(s), {len(ranked)} client(s) seen", ""]
    lines.append(f"  {'#':>2}  {'Client':<24} {'Total':>11} {'Down':>11} "
                 f"{'Up':>11}  {'Share':>6}")
    for rank, entry in enumerate(ranked[:limit], 1):
        lines.append(f"  {rank:>2}  {entry['name'][:24]:<24} "
                     f"{human_bytes(entry['total']):>11} "
                     f"{human_bytes(entry['down']):>11} "
                     f"{human_bytes(entry['up']):>11}  "
                     f"{entry['share_pct']:>5.1f}%")
    lines += ["", f"  Total across all clients: {human_bytes(grand)}",
              "  Down/Up are from the client's point of view."]
    return emit(payload, lines, as_json)


def report_client(paths, days: int, needle: str, as_json: bool) -> int:
    """Daily totals for one client, matched by MAC or by any name it has worn.

    Matching deliberately considers *every* name seen in the window, not just
    the label the client ended up with, so a device whose hostname changed
    half way through is still found by the name it had at the start. The match
    resolves to a MAC and everything after that is keyed on the MAC alone.
    """
    labels, _poll_count = resolve_labels(iter_polls(paths))
    wanted = normalise_mac(needle)
    loose = needle.strip().lower()
    matches: set[str] = set()
    for _ts, row in iter_rows(iter_polls(paths, warn=False)):
        if row["mac"] == wanted or loose in str(row.get("name") or "").lower():
            matches.add(row["mac"])
    if not matches:
        print(f"stats: no client matches {needle!r} in the last {days} day(s)",
              file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"stats: {needle!r} matches {len(matches)} clients:",
              file=sys.stderr)
        for candidate in sorted(matches, key=lambda m: labels[m]):
            print(f"  {candidate}  {labels[candidate]}", file=sys.stderr)
        print("Re-run with the MAC address.", file=sys.stderr)
        return 1

    mac = next(iter(matches))
    name = labels[mac]
    per_day: dict[str, dict] = {}
    for timestamp, row in iter_rows(iter_polls(paths, warn=False)):
        if row["mac"] != mac:
            continue
        day = per_day.setdefault(day_of(timestamp), {
            "day": day_of(timestamp), "down": 0, "up": 0, "samples": 0,
            "satisfaction": [], "link_mbps": None})
        day["down"] += row.get("tx_delta") or 0
        day["up"] += row.get("rx_delta") or 0
        day["samples"] += 1
        if row.get("satisfaction") is not None:
            day["satisfaction"].append(row["satisfaction"])
        if row.get("link_mbps") is not None:
            day["link_mbps"] = row["link_mbps"]

    days_sorted = [per_day[key] for key in sorted(per_day)]
    for day in days_sorted:
        scores = day.pop("satisfaction")
        day["satisfaction_avg"] = (round(sum(scores) / len(scores), 1)
                                   if scores else None)
    grand_down = sum(day["down"] for day in days_sorted)
    grand_up = sum(day["up"] for day in days_sorted)

    payload = {"mac": mac, "name": name, "window_days": days,
               "total_down_bytes": grand_down, "total_up_bytes": grand_up,
               "days": days_sorted}
    lines = [f"{name}  ({mac}) · {window_note(paths, days)}", "",
             f"  {'Day':<12} {'Down':>11} {'Up':>11} {'Sat':>5} "
             f"{'Link':>7}  Polls"]
    for day in days_sorted:
        satisfaction = ("—" if day["satisfaction_avg"] is None
                        else f"{day['satisfaction_avg']:.0f}")
        link = "—" if day["link_mbps"] is None else f"{day['link_mbps']}M"
        lines.append(f"  {day['day']:<12} {human_bytes(day['down']):>11} "
                     f"{human_bytes(day['up']):>11} {satisfaction:>5} "
                     f"{link:>7}  {day['samples']}")
    lines += ["", f"  Total  down {human_bytes(grand_down)}"
                  f"  ·  up {human_bytes(grand_up)}"]
    return emit(payload, lines, as_json)


def report_links(paths, as_json: bool) -> int:
    """Negotiated link speed for every wired client, as of the newest poll.

    Anything below a gigabit is flagged. The API does **not** report what a
    switch port is capable of — `max_speed` is null on every port — so this
    cannot claim the port is gigabit-capable, only that the link came up below
    gigabit. On this hardware that is usually a cable or a NIC.
    """
    labels, _poll_count = resolve_labels(iter_polls(paths))
    latest = None
    for poll in iter_polls(paths, warn=False):
        latest = poll
    if latest is None:
        print("stats: no metrics recorded yet", file=sys.stderr)
        return 1
    wired = [row for row in latest["clients"]
             if isinstance(row, dict) and row.get("mac")
             and row.get("type") == watch.WIRED]
    wired.sort(key=lambda row: (row.get("link_mbps") or 0, labels[row["mac"]]))
    slow = [row for row in wired if (row.get("link_mbps") or 0) < GIGABIT_MBPS]

    payload = {"ts": latest.get("ts"), "wired": wired,
               "below_gigabit": [row["mac"] for row in slow]}
    lines = [f"Wired links · poll of {latest.get('ts')}", "",
             f"    {'Client':<24} {'Link':>9}  {'Switch':<16} Port"]
    for row in wired:
        mbps = row.get("link_mbps")
        flag = "⚠ " if (mbps or 0) < GIGABIT_MBPS else "  "
        port = row.get("sw_port")
        lines.append(f"{flag}  {labels[row['mac']][:24]:<24} "
                     f"{('—' if mbps is None else f'{mbps} Mbps'):>9}  "
                     f"{str(row.get('uplink') or '—')[:16]:<16} "
                     f"{'—' if port is None else port}")
    if not wired:
        lines.append("  (no wired clients in the most recent poll)")
    lines += ["", f"  {len(slow)} of {len(wired)} link(s) below gigabit. "
                  "The API does not report port",
              "  capability, so this is the negotiated speed only."]
    return emit(payload, lines, as_json)


def report_quality(paths, days: int, as_json: bool) -> int:
    """Clients ranked by worst average satisfaction over the window."""
    labels, poll_count = resolve_labels(iter_polls(paths))
    scores: dict[str, dict] = {}
    for _ts, row in iter_rows(iter_polls(paths, warn=False)):
        entry = scores.setdefault(row["mac"], {
            "mac": row["mac"], "name": labels[row["mac"]],
            "type": row.get("type"), "satisfaction": [], "signal": [],
            "retries": []})
        for key, field in (("satisfaction", "satisfaction"),
                           ("signal", "signal"),
                           ("retries", "tx_retries_pct")):
            if row.get(field) is not None:
                entry[key].append(row[field])

    def mean(values):
        return round(sum(values) / len(values), 1) if values else None

    ranked = []
    for entry in scores.values():
        if not entry["satisfaction"]:
            continue
        ranked.append({
            "mac": entry["mac"], "name": entry["name"], "type": entry["type"],
            "satisfaction_avg": mean(entry["satisfaction"]),
            "satisfaction_min": min(entry["satisfaction"]),
            "signal_avg": mean(entry["signal"]),
            "tx_retries_pct_avg": mean(entry["retries"]),
            "samples": len(entry["satisfaction"]),
        })
    ranked.sort(key=lambda e: (e["satisfaction_avg"], e["satisfaction_min"]))

    payload = {"window_days": days, "polls": poll_count, "clients": ranked}
    lines = [f"Link quality, worst first · {window_note(paths, days)}", "",
             f"  {'Client':<24} {'Avg':>5} {'Min':>5} {'Signal':>7} "
             f"{'Retries':>8}  Polls"]
    for entry in ranked:
        signal = ("—" if entry["signal_avg"] is None
                  else f"{entry['signal_avg']:.0f} dBm")
        retries = ("—" if entry["tx_retries_pct_avg"] is None
                   else f"{entry['tx_retries_pct_avg']:.1f}%")
        lines.append(f"  {entry['name'][:24]:<24} "
                     f"{entry['satisfaction_avg']:>5.0f} "
                     f"{entry['satisfaction_min']:>5} {signal:>7} "
                     f"{retries:>8}  {entry['samples']}")
    if not ranked:
        lines.append("  (no satisfaction scores recorded in this window)")
    else:
        lines += ["", "  Signal and retries are wireless-only; a wired client "
                      "reports neither."]
    return emit(payload, lines, as_json)


# --- Main ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stats.py", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--top", nargs="?", type=int, const=DEFAULT_TOP,
                      metavar="N",
                      help=f"busiest N clients by total bytes "
                           f"(default {DEFAULT_TOP})")
    mode.add_argument("--client", metavar="MAC|NAME",
                      help="daily totals for one client")
    mode.add_argument("--links", action="store_true",
                      help="negotiated link speed per wired client, right now")
    mode.add_argument("--quality", action="store_true",
                      help="clients with the lowest average satisfaction")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, metavar="D",
                        help=f"window in days (default {DEFAULT_DAYS})")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit JSON instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.days < 1:
        print("stats: --days must be at least 1", file=sys.stderr)
        return 1

    root = watch.load_root()
    # --links reads only the newest poll, but still needs a file to read it
    # from; one day's window is enough and keeps a 90-day history off the heap.
    days = 1 if args.links else args.days
    paths = metrics_files(root, days, datetime.now().date())
    if not paths:
        directory = root / watch.METRICS_DIRNAME
        print(f"stats: no metrics under {directory} for the last "
              f"{days} day(s).", file=sys.stderr)
        if not directory.is_dir():
            print("Run `python3 watch.py` once to start collecting.",
                  file=sys.stderr)
        return 1

    if args.links:
        return report_links(paths, args.as_json)
    if args.quality:
        return report_quality(paths, args.days, args.as_json)
    if args.client:
        return report_client(paths, args.days, args.client, args.as_json)
    return report_top(paths, args.days, args.top, args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
