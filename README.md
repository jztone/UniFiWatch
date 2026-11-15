# UniFiWatch

Polls a local UniFi Dream Machine every five minutes and records what changed —
devices going down and coming back, firmware updates, clients joining for the
first time, and internet outages. Events are written only on transition, so a
quiet network produces a quiet log.

```
~/UniFiWatch/
├── state.json      # what the last poll saw
├── events.jsonl    # every transition, ever (append-only)
├── pending.json    # the unacknowledged subset — what a consumer reads
├── ack-inbox.json  # what a consumer writes back to acknowledge them
├── notified.json   # which events the consumer has already been woken for
├── metrics/        # per-client traffic samples, one file per day
└── logs/unifiwatch.log
```

It also samples every connected client on each poll — bytes moved, link speed,
signal, satisfaction — into `metrics/`, for answering "who is using the
bandwidth?" rather than "what just broke?". See [Traffic statistics](#traffic-statistics).

Python 3, standard library only. macOS-only: it schedules with `launchd` and
notifies with `osascript`. See [CLAUDE.md](CLAUDE.md) for the full design.

## Prerequisites

- macOS with `python3` on `PATH`.
- A UniFi console reachable on the LAN (a Dream Router / Dream Machine).
- A **site-scoped API key**: UniFi console → Settings → Control Plane →
  Integrations → Create API Key. Copy it once; it is not shown again.

## Quickstart

```bash
# 1. Configure. Fill in apiKey; host and siteId are already set for this network.
#    (siteId comes from GET https://<host>/proxy/network/integration/v1/sites)
$EDITOR config.local.json

# 2. Run one poll by hand. The first run seeds state and writes no events.
python3 watch.py

# 3. Install the five-minute launchd agent.
./install.sh

# 4. Trigger a run immediately to confirm the schedule works.
launchctl kickstart -k gui/$(id -u)/com.jztone.unifiwatch
tail -n 20 ~/Library/Logs/UniFiWatch/unifiwatch.log
```

## Configuration

`config.local.json` — gitignored, because it holds the API key.

```json
{
  "host": "192.168.1.1",
  "apiKey": "…",
  "siteId": "<Integration-API site UUID>",
  "root": "~/UniFiWatch",
  "thresholds": {
    "default_offline_minutes": 5,
    "per_device": { "Express Mesh 1": 30 }
  },
  "clients": {
    "ignore_mac_prefixes": ["aa:bb:cc:dd"]
  },
  "notify": {
    "agent": "<openclaw agent>",
    "target": "<slack channel id>"
  },
  "stats": {
    "enabled": true,
    "retain_days": 90,
    "site": "default"
  }
}
```

A device must stay offline longer than its threshold before anything is
written, so brief unplugs never reach the log. `per_device` is keyed by the
name shown in the UniFi app — `Express Mesh 1` is set to 30 minutes because it
gets unplugged regularly; wired gear keeps the 5-minute default and alerts fast.

The `stats` block is optional; those are the defaults. `retain_days` sets how
long `metrics/` is kept — 90 days runs to roughly 200 MiB on a network this
size. `site` is the *legacy* site name (almost always `default`) and is not the
same thing as `siteId` above.

`clients.ignore_mac_prefixes` (optional) lists MAC prefixes, in whole octets,
whose new clients are remembered but never reported — for a device that keeps
inventing a fresh address, such as the zero-traffic phantom some access points
present as a wired client.

`notify` names the agent `notify.py` wakes and the Slack channel it reports
to. Neither has a default: without both, the poll runs but nobody is woken.
`message` (default `kör natverk-rapport`) and `channel` (default `slack`) can
be set there too.

Environment variables override the file: `UNIFIWATCH_ROOT`, `UNIFIWATCH_HOST`,
`UNIFIWATCH_API_KEY`, `UNIFIWATCH_SITE_ID`, `UNIFIWATCH_OPENCLAW`,
`UNIFIWATCH_NOTIFY_AGENT`, `UNIFIWATCH_NOTIFY_TARGET`.

**Re-run `./install.sh` after editing `config.local.json`, `watch.py` or
`notify.py`** — the scheduled job runs from copies on local disk, because
launchd cannot read this repo's CloudStorage mount.

## How events work

Every event is one line in `events.jsonl` with an id (`e17`), a UTC timestamp,
a kind, a subject and a detail object. `pending.json` is the unacknowledged
subset, regenerated on every poll that writes something.

The intended consumer loop: read `pending.json`, report the events, and
acknowledge them.

```bash
cat ~/UniFiWatch/pending.json
python3 watch.py --acknowledge e17 e18
python3 watch.py --acknowledge-all
```

The consumer does not have to poll for that. `notify.py` runs at the end of
every poll and wakes it — one detached `openclaw agent` turn — as soon as a poll
queues an event the consumer has not been told about yet. Ids already handed
over are recorded in `notified.json` and pruned once they leave the queue, so
the same events never trigger a second wake while they wait to be acknowledged.
Point `UNIFIWATCH_OPENCLAW` somewhere else to test it without waking anyone;
`tools/test_notify.sh` does exactly that.

### Acknowledging without a shell

A consumer that can only read and write files acknowledges by writing
`~/UniFiWatch/ack-inbox.json`:

```json
{ "acknowledge": [17, 18] }
{ "acknowledge": "all" }
```

Ids may be written as `17`, `"17"` or `"e17"`. The next poll applies them and
deletes the file; unknown or already-acknowledged ids are ignored, so writing
the same file twice is harmless.

It takes effect **immediately** as far as waking goes, not at the next poll:
`notify.py` subtracts the inbox from `pending.json` itself, so an acknowledged
event stops counting as something to wake the agent for the moment the file is
written.

Internet outages are reported *after* they end: while the line is down nothing
can be sent, so the recovery event carries the whole window and duration.

## Traffic statistics

Every poll also records what each connected client moved since the last one,
plus its link speed and signal, into `~/UniFiWatch/metrics/YYYY-MM-DD.jsonl`.
These are never events — nothing here is ever reported or alerted on, it just
accumulates so the questions below have an answer.

```bash
python3 stats.py --top                  # busiest clients, last 7 days
python3 stats.py --top 20 --days 30     # …top 20 over a month
python3 stats.py --client Mac --days 14 # daily totals for one client
python3 stats.py --links                # wired link speeds right now
python3 stats.py --quality              # worst average satisfaction
```

Add `--json` to any of them. `stats.py` reads only the files — it makes no API
calls and writes nothing, so it is safe to run at any time.

`--links` flags anything negotiating below gigabit, which usually means a cable
or a NIC. It says "negotiated below gigabit" rather than "this gigabit port is
underperforming" because the console does not report what a port is capable of.

**Clients are identified by MAC address, never by name.** A device's reported
name can change — the console sometimes alternates between two hostnames for
the same machine — and one name can belong to several devices, as seven Sonos
speakers here all call themselves `SonosZP`. Reports therefore group by MAC and
treat the name purely as a label, picking one per device and adding a short MAC
suffix when two devices would otherwise print the same name. `--client` accepts
a MAC or any name the device has used.

This data comes from UniFi's **unofficial** API, so a firmware update could
remove it. If that happens the graphs stop updating and nothing else changes —
device, client and WAN monitoring are on the official API and are unaffected.

## Status & management

```bash
python3 status.py                                     # devices, WAN, pending count
python3 stats.py --top                                # busiest clients
python3 watch.py                                      # poll now, in the foreground
python3 watch.py --acknowledge-all                    # clear pending.json
tail -f ~/Library/Logs/UniFiWatch/unifiwatch.log      # app log (scheduled runs)
launchctl print gui/$(id -u)/com.jztone.unifiwatch    # agent status
./install.sh uninstall                                # remove the agent
```

Exit codes: `0` success · `1` general or partial error · `2` the console
rejected the API key.
