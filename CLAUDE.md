# CLAUDE.md — UniFiWatch

Project guide for Claude Code (and humans). Read this first before changing anything.

## What this is

A **five-minute poller** for a local UniFi Dream Machine. Every run asks the
console's Network Integration API what it sees, compares that against the
previous run, and writes one line per **transition** to an append-only event
log. Nothing is written for steady state: a device that is still offline on the
next poll produces no second event, and when it comes back the recovery event
carries the outage duration.

The consumer is an external agent, not a human. `pending.json` holds only the
unacknowledged events, so the agent reads a small file instead of the whole
history, and `notify.py` wakes that agent at the end of any poll that queued
something new — so an expensive agent turn fires when something happened and
not on a timer.

**macOS-only by design:** scheduling uses `launchd`; failure alerts use
`osascript` notifications.

## Language & dependencies

- Python 3, standard library only. No `pip install`, no virtualenv, no
  third-party HTTP client.
- External runtime dependencies: `/sbin/ping` and `osascript`, both part of
  macOS.
- Several scripts, one module. Everything beside `watch.py` `import`s it and
  reuses its resolvers rather than duplicating them, so there is exactly one
  definition of where the root is and what the state file means.

## Repository layout

```
UniFiWatch/
├── CLAUDE.md                   # this file
├── README.md                   # human quickstart
├── watch.py                    # the poller: config, API, collectors, events
├── status.py                   # read-only readout; no API calls
├── stats.py                    # read-only reports over metrics/; no API calls
├── notify.py                   # wakes the consumer when a poll queues events
├── com.jztone.unifiwatch.plist # launchd template (__PLACEHOLDER__s)
├── run.sh.template             # launchd wrapper template
├── install.sh                  # renders both, copies to local disk, loads
├── config.local.json           # gitignored: host, apiKey, siteId, thresholds,
│                               # notify agent/target
├── tools/test_notify.sh        # behavioural tests for notify.py
├── STATISTICS.md               # survey of the statistics the APIs offer
├── PLAN-weekly-report.md       # plan for the weekly report consumer
├── LICENSE                     # MIT
└── .gitignore
```

**No state, events, logs or credentials are ever committed.** Everything the
poller produces lives under `<root>` (default `~/UniFiWatch`), outside the repo.

## Data source — the UniFi Network Integration API

The official local API on the console itself. No cloud account, no UniFi OS
login: a site-scoped API key created in the console's control-plane settings.

```
Base:    https://<host>/proxy/network/integration/v1
Headers: X-API-KEY: <key>
         Accept: application/json
```

| Endpoint | Fields used |
|---|---|
| `GET /sites/<siteId>/devices` | `id`, `name`, `model`, `state`, `ipAddress`, `firmwareVersion`, `firmwareUpdatable` |
| `GET /sites/<siteId>/devices/<id>` | adds `uplink.deviceId`, `ports`, `radios` — **not used yet**; the detail call costs one request per device and nothing in the current collectors needs it |
| `GET /sites/<siteId>/clients` | `type`, `name`, `macAddress`, `ipAddress`, `connectedAt`, `uplinkDeviceId` |

### Pagination is not optional

Collection responses are wrapped:

```json
{ "offset": 0, "limit": 25, "count": 25, "totalCount": 63, "data": [ … ] }
```

The default page size is **25** and `totalCount` routinely exceeds it on the
clients endpoint. Reading only the first page would make every client past the
25th look like it had never been seen — and then look new again on the next
poll. `api_get_all` requests `limit=100` and follows `offset` until `offset >=
totalCount`, treating an empty `data` array as the end too, so a console that
reports a `totalCount` it cannot deliver costs one wasted request rather than
spinning. Devices are fetched the same way; they rarely paginate today, but the
envelope says they can.

### TLS verification is disabled — deliberately, for this host only

A UniFi console serves its local API under a **self-signed certificate issued
to a LAN IP address**. No CA can vouch for it, so verification cannot succeed
however it is configured, and the stock TLS stack refuses the connection
outright. `ssl_context()` therefore builds a context with
`check_hostname = False` and `verify_mode = CERT_NONE`.

This is acceptable *here* and nowhere else: the request never leaves the local
network, the host is pinned in `config.local.json`, and the `X-API-KEY` header —
not the certificate — is what authenticates us to the console. Do not reuse
that context for anything reached over the internet, and do not "fix" it by
disabling verification globally.

### One connection per poll

Every request in a poll goes to the same console, so all of them share a
single keep-alive `http.client.HTTPSConnection` (`console_connection`,
created with the console-only TLS context above): a poll costs one TLS
handshake however many pages and collectors run. A request that fails on an
already-established connection is retried once on a fresh one — a keep-alive
link the console silently closed is routine across a Mac sleep — but a
failure on a fresh connection raises at once, because an unreachable console
is routine here too and should not pay a doubled timeout. The shared piece
ends at the socket: error classification stays with the callers, `api_get`
raising `AuthError`/`RuntimeError` and `legacy_api_get` returning `None`.

## Second data source — the legacy API, for statistics only

Everything above is the official, versioned Integration API. The statistics
collector reads a **second, unofficial one**:

```
Base:    https://<host>/proxy/network/api
Used:    GET /s/<site>/stat/sta     one record per connected client
Headers: X-API-KEY: <key>           the same key; no cookie login needed
```

This is the interface the UniFi web app talks to. It is undocumented,
unversioned, and Ubiquiti is free to change or remove it in any firmware
update. **If it disappears, a graph stops updating.** No alert is missed, no
event is lost, no acknowledgement is dropped and the poll still exits 0 —
because the isolation rules below are what the whole design is organised
around, not a nicety.

- **It runs last**, after every official-API collector, and after
  `events.jsonl` and `pending.json` have been written and are durable.
- **It has its own fetch function.** `legacy_api_get` returns `None` for every
  possible failure and never raises. This is not defensive habit: the legacy
  API answers **HTTP 401** (`api.err.NoSiteContext`) for a site name it does
  not recognise, *not* 404. `api_get` turns 401 into `AuthError` and `main`
  turns `AuthError` into exit 2 plus an "API key rejected" notification, so a
  shared fetch would let a typo in `stats.site` impersonate an expired key and
  abort a poll with nothing wrong with it. Verified against the console.
- **It is not in the `collectors` list.** That list feeds the `errors` counter
  and a non-zero count returns 1. A statistics failure must never change the
  exit code, so it carries its own `except Exception` instead.
- **`stats.enabled: false` turns it off entirely** — no fetch, no directory, no
  `stats` key in `state.json`.

### Field normalisation

The legacy API names the byte counters differently per client type, and each
type carries whole fields the other lacks. Everything is flattened into one
row shape; neither set is assumed present.

| Recorded | Wireless source | Wired source |
|---|---|---|
| `rx_delta` / `tx_delta` | `rx_bytes` / `tx_bytes` | `wired-rx_bytes` / `wired-tx_bytes` |
| `link_mbps` | `tx_rate ÷ 1000` | `wired_rate_mbps` |
| `tx_retries_pct` | `wifi_tx_retries_percentage` | — |
| `sw_mac`, `sw_port` | — | as named |
| `satisfaction_reason` | as named | — |
| `signal`, `noise`, `channel`, `radio_proto`, `essid` | as named | — |
| `satisfaction`, `uplink`, `dev_vendor`, `os_name`, `oui` | both | both |

Four traps in that table, all confirmed against the console:

- **`rx`/`tx` are named from the network's point of view.** A client's
  `tx_delta` is what the network sent *to* it — the client's **download** —
  and `rx_delta` is its upload. `stats.py` relabels them client-side, because
  that is what "who is using the bandwidth?" means.
- **`rx_rate`/`tx_rate` are negotiated PHY rates in kbps, not throughput.**
- **`tx_retries_pct` does not exist in the API.** It is
  `wifi_tx_retries_percentage`, a recent-window sample already expressed as a
  percentage. It is *not* `tx_retries ÷ wifi_tx_attempts`, which are lifetime
  counters and give a materially different number.
- **`name` is set only on clients someone renamed by hand** — 1 of 27 on this
  network. `hostname` is universal, so the fallback is `name` → `hostname` →
  `mac`. `dev_vendor` and `os_name` are opaque fingerprint-database integers;
  `oui` is the readable vendor string and is captured alongside them.

A record with no MAC is skipped. A record with neither byte pair still produces
a row, marked `partial` — a client we can see but cannot measure is worth
recording as exactly that.

## Configuration

`config.local.json`, beside the script. Gitignored — it holds the API key.

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
  "stats": {
    "enabled": true,
    "retain_days": 90,
    "site": "default"
  },
  "notify": {
    "agent": "<openclaw agent>",
    "target": "<slack channel id>"
  }
}
```

- `CONFIG_FILE` resolves relative to `Path(__file__).parent`, so the installed
  copy under `~/Library/Application Support/UniFiWatch` reads the config
  `install.sh` placed next to it. That is why credentials are **not** baked into
  the launchd wrapper: `thresholds.per_device` is a nested object that cannot be
  expressed as an environment variable anyway, and a secret in a shell script is
  worse than a secret in a `chmod 600` JSON file.
- Optional `gatewayName` pins which device is the gateway; see WAN monitoring.
- **`notify` names who `notify.py` wakes**: `agent` (the openclaw agent) and
  `target` (the Slack channel id it replies to) have **no default** — they name
  somebody's agent and somebody's channel, and a guess would wake the wrong
  one. `message` (default `kör natverk-rapport`) and `channel` (default
  `slack`) are optional. Each resolves `UNIFIWATCH_NOTIFY_<KEY>` → this table →
  default, via `notify.load_notify_settings`, which reads through watch's
  cached `_read_config`. This repo is public: those values belong here and
  nowhere in the tracked files.
- **`stats.site` is the LEGACY site name**, almost always `default`, and is a
  different identifier space from `siteId` — which is the Integration API's
  UUID. Two site keys in one config file looks like a mistake until you know
  that. The whole `stats` block is optional and defaults to
  `{enabled: true, retain_days: 90, site: "default"}`.
- **Per-device thresholds are keyed by device name**, matching the UniFi app.
  Renaming a device in the app rebinds it to the default until this file is
  updated to match. `Express Mesh 1` is a mesh AP that gets
  unplugged regularly, so it must stay quiet for 30 minutes; wired
  infrastructure keeps the 5-minute default and alerts fast.

## State & idempotency

`<root>/state.json` — what the last poll saw. Written atomically, `last_run`
stamped inside `save_state`.

```json
{
  "schema": 1,
  "devices": {
    "<deviceId>": {
      "name": "Express Mesh 1", "model": "…", "state": "ONLINE",
      "state_since": "…", "offline_since": null, "offline_reported": false,
      "firmware_version": "…", "firmware_updatable": false, "ip_address": "…"
    }
  },
  "known_clients": { "<mac>": { "name": "…", "type": "…", "first_seen": "…" } },
  "wan": { "address": "…", "connectivity": "up", "down_since": null, "last_ok": "…" },
  "stats": { "<mac>": { "rx": 29035212, "tx": 927484964, "seen": "…",
                        "name": "laptop-a", "name_src": "hostname",
                        "name_pending": "laptop-b" } },
  "next_event_id": 17,
  "last_run": "…"
}
```

- **Devices are keyed by API id** (survives a rename), **clients by lowercased
  MAC** (client ids are reassigned across reconnects).
- **The offline dance.** `state == OFFLINE` sets `offline_since` and nothing
  else. Once `now - offline_since >= threshold`, the event is written and
  `offline_reported` is set so it is written exactly once. Coming back clears
  both, and only emits `device_online` **if the offline event was reported** —
  so a flap shorter than the threshold produces no event in either direction.
  Any state that is neither `ONLINE` nor `OFFLINE` (`UPDATING`, `ADOPTING`, …)
  is transitional: it neither starts a timer nor clears a running one, so a
  firmware update cannot fake an outage or mask one already in progress.
- **First-run seeding.** Each collector's slice starts as `null`, not `{}`, and
  `null` means "record what you see and emit nothing". A fresh install produces
  a state file and zero events instead of one event per device and client on the
  network. This is also the corruption-recovery path: a lost or unparseable
  `state.json` degrades to a fresh skeleton, so the next poll re-seeds silently
  rather than flooding the log with `new_client` events for a network that has
  not changed.
- **A device missing from the API response is not offline** — an offline device
  is still listed, with `state: OFFLINE`. Dropping out of the list means
  un-adopted or removed, which is logged once (via an `absent` marker on the
  entry) rather than on every poll for the rest of time.
- **`stats` is a separate slice, deliberately.** It holds the byte-counter
  baselines and the remembered display name, keyed by MAC. Nothing else reads
  it, and the collector writes nothing else — so a bug there cannot disturb
  device or client history. Entries not seen inside `retain_days` are dropped,
  since a baseline older than the metrics it could ever feed is dead weight and
  every guest device that ever joined would otherwise accumulate for good. An
  entry with an unreadable `seen` stamp is kept: dropping a live client's
  baseline would make its next poll look like a first sighting and silently
  lose an interval of traffic.
- **The lock.** `run_lock` holds an exclusive `flock` on `<root>/.lock` around
  the whole read-modify-write, in the poller and in `--acknowledge` alike. The
  five-minute poll appends to `events.jsonl` while an acknowledgement may be
  rewriting it; without the lock the rewrite would silently drop an event that
  landed between its read and its write. Read-only tools such as `status.py`
  take no lock on purpose — every rewrite lands via `os.replace`, so they can
  only ever see a complete file.

## Events

`<root>/events.jsonl` — append-only, one JSON object per line:

```json
{ "id": "e17", "ts": "2026-07-27T09:41:02+00:00", "kind": "device_offline",
  "subject": "Express Mesh 1",
  "detail": { "device_id": "…", "offline_since": "…", "threshold_minutes": 30 },
  "acknowledged": false }
```

| kind | subject | detail |
|---|---|---|
| `device_offline` | device name | `device_id, model, last_ip, offline_since, offline_minutes, threshold_minutes` |
| `device_online` | device name | `device_id, offline_since, outage_minutes` |
| `firmware_available` | device name | `device_id, firmware_version` |
| `firmware_updated` | device name | `device_id, old_version, new_version` |
| `new_client` | client name or MAC | `mac, ip_address, type, uplink_device, uplink_device_id, connected_at` |
| `wan_address_changed` | `wan` | `old_address, new_address, gateway` |
| `wan_down` | `wan` | `mode` |
| `wan_recovered` | `wan` | `down_since, recovered_at, outage_minutes, mode` |

**Event ids** are `e<N>` from a counter in `state.json` — short enough to type
into `--acknowledge`, and stable because the id is written into the line.
`repair_event_counter` advances the counter past the highest id already in
`events.jsonl` at the start of every run, so a lost or restored state file
cannot reissue an id that is already on disk. That scan reads the whole log on
every poll; its cost is bounded by the same future log rotation that bounds
`--acknowledge`'s whole-file rewrite (see Gotchas / when editing), so the two
are never optimised independently.

`<root>/pending.json` is the unacknowledged subset, **always regenerated from
the log** rather than edited in place, so it can never disagree with the log and
a consumer that deletes it gets it back on the next poll:

```json
{ "generated": "2026-07-27T11:41:02+02:00", "count": 2, "events": [ … ] }
```

`generated` is local time with an offset (a human-facing agent reads it);
everything inside an event is UTC.

**Acknowledging** — `watch.py --acknowledge e17 [e18 …]` or
`--acknowledge-all` — rewrites `events.jsonl` with the flag flipped and
regenerates `pending.json`. Lines that could not be parsed are written back
byte for byte: a line we cannot read is still history. An unknown id is
reported and exits 1, after the ids that did match are acknowledged.

Both the CLI and the inbox below go through `_apply_acknowledgement`, so there
is one definition of what acknowledging means; they differ only in how they
report an id that matched nothing. The rewrite is **skipped entirely when
nothing changed**, which is what makes re-applying an inbox a genuine no-op
rather than a no-op-shaped rewrite.

### The acknowledgement inbox — for a consumer that cannot run commands

`<root>/ack-inbox.json`, written by the consumer, drained by the poller:

```json
{ "acknowledge": [12, 13, 14] }
{ "acknowledge": "all" }
```

**This exists because the consumer is a sandboxed agent with `exec` denied by
policy.** It can read and write files and nothing else, so it cannot run
`--acknowledge-all`; without a file-based path it could never acknowledge
anything, `pending.json` would grow forever, and every wake would re-report the
same events. The CLI path stays for human use.

- **Ids are normalised on read.** `12`, `"12"`, `"e12"` and `"E12"` all mean
  `e12` — the consumer writes whichever form is natural to it rather than
  having to know how ids are minted. Entries that normalise to nothing are
  dropped silently.
- **Idempotent by contract.** Unknown and already-acknowledged ids are ignored
  without complaint, so applying the same file twice changes nothing. A
  consumer that cannot read our exit code has no way to react to an error, so
  refusing a replay would only strand it.
- **Rename, then read.** The inbox is `os.replace`d to
  `ack-inbox.processing.json` *before* being parsed. The rename is atomic, so a
  consumer writing a fresh inbox while a poll is in flight lands on a clean
  path and is picked up by the next run instead of being half-read or silently
  overwritten.
- **A leftover processing file is drained first**, before the new inbox is
  renamed into place — it means a previous run died between the rename and the
  apply, and renaming onto it would destroy the ids it holds.
- **The processing file is deleted *after* `pending.json` is regenerated**, not
  before. Anything that subtracts inbox ids from `pending.json` — the way
  `suppressed_ack_ids` is meant to be used — would otherwise hit a window in
  which the ids are gone from the inbox but still listed as pending, and a
  consumer waking in that window would report them a second time. That double
  report is the entire reason this feature exists, so the ordering is
  load-bearing. It also constrains the *reader*: take the suppression set
  first and the queue second, or the same window reopens from the other side.
- **Malformed input is set aside, never fatal.** Unparseable JSON — or a
  document that parses but cannot be acted on, such as a missing `acknowledge`
  key — is renamed to `ack-inbox.bad-<timestamp>.json` with a warning. The file
  is kept rather than deleted because the consumer believed it was writing
  something meaningful and that is worth being able to debug. A later valid
  inbox is unaffected.
- The whole drain is wrapped in its own `try/except` in `main()`: a broken
  inbox can never abort a poll.

**`suppressed_ack_ids` is how a reader subtracts the inbox.** `pending.json` is
only regenerated by a poll, so for up to five minutes after the consumer
acknowledges, it still lists events that have already been reported. Reading it
naively would keep saying "yes, report these" and the consumer would report them
twice. `suppressed_ack_ids(root)` returns `(acknowledge_everything, ids)` merged
across `ack-inbox.json` **and** `ack-inbox.processing.json` — both, because
exactly one exists at any instant during a drain — so an acknowledgement takes
effect the moment it is written rather than at the next poll. A malformed inbox
counts as empty there: for a reader, refusing to suppress is the safe direction.

`--acknowledge-all` also deletes both inbox files, so the two paths cannot
disagree. A targeted `--acknowledge` deliberately does not, since the inbox may
name ids that invocation did not.

### Waking the consumer — `notify.py`

`run.sh` runs `notify.py` immediately after `watch.py`, in the same poll. It
makes no API calls, reads only what that run wrote, and spawns one detached
command:

```
openclaw agent --agent <agent> --message "kör natverk-rapport" \
  --deliver --reply-channel slack --reply-to <target>
```

**This is a push, and it replaced a pull.** The consumer's scheduler used to run
a condition script on its own cadence to decide whether an agent turn was worth
firing. That could never work: trigger scripts are evaluated in a sandbox that
forbids module imports (`code mode module access is disabled`), so the script
could not `import watch`, and the sandbox is not configurable — the trigger
schema accepts only `enabled` and `minIntervalMs` under
`additionalProperties: false`. The poll already knows the answer at the moment
it writes the queue, so it says so. Latency drops from the cron interval to the
poll interval as a side effect.

- **`<root>/notified.json` is why it does not repeat itself.** Waking the agent
  does not drain the queue: the agent reports, writes `ack-inbox.json`, and the
  poller applies that inbox on the *next* poll — so for at least one cycle
  `pending.json` still lists events already reported. Deciding from
  `pending.json` alone would wake the agent every five minutes on the same
  events, forever. The file records handed-over ids and is **pruned against the
  queue on every run**, so it cannot grow for the life of the install. It is
  derived state: delete it whenever `events.jsonl` or `state.json` is deleted by
  hand, or a restarted `e1` would be suppressed as already reported.
- **Read the inbox first, the queue second.** `suppressed_ack_ids` before
  `pending.json`, never the other way round — see the drain ordering above. A
  reader that takes the queue first can pair a stale queue with an inbox that
  has already been deleted, and wake the agent for events reported minutes ago.
  In the correct order the worst case is suppressing an id for one cycle.
- **A missing or unreadable `pending.json` means *unknown*, not *empty*.**
  `queue_ids` returns `None` and the run does nothing at all — no prune, no
  wake. Absence is the normal state before the first poll lands, and a consumer
  is allowed to delete the file; pruning against it would discard exactly the
  state that stops the next run re-reporting everything. For the same reason
  there is no fallback to the `count` field: a count with no ids gives nothing
  to record as reported, so the wake could not be deduplicated.
- **`notified.json` is written *before* the spawn, never after.** A failed write
  must mean no wake at all — an unrecorded wake repeats every five minutes for
  as long as the events sit in the queue. The opposite failure is **accepted,
  not solved**: if the agent turn dies on the way out, its report dies with it.
  That is bounded, because the events stay in `pending.json` and a woken agent
  reads the whole queue rather than only the ids that triggered it, so the next
  report carries them out.
- **Its own lock, non-blocking.** `<root>/.notify.lock`, `LOCK_NB` — an
  overlapping run skips rather than queues. It guards the read-modify-write of
  `notified.json` against a second notifier (a manual run beside a scheduled
  one), which would otherwise read the same state and wake the agent twice.
- **A corrupt `notified.json` counts as empty**, with a warning, and is rewritten
  even when nothing else changed. One duplicate report beats permanent silence.
- **Exit codes are the notifier's own** and never reach launchd: the wrapper
  exits with `watch.py`'s code. `0` covers nothing-to-do, a prune and a skipped
  lock alike; `1` is a missing `openclaw`, an unset `notify.agent` or
  `notify.target`, or a failed write. The first two are preflight checks that
  run before anything is locked or recorded, so a fresh id is never marked as
  notified by a run that could not wake anyone; `install.sh` also warns about
  both at install time. It logs to stderr,
  which launchd captures in `launchd.err.log`; the quiet paths log at DEBUG, so
  the log holds only wakes, prunes, skips and errors.

## WAN monitoring

Two independent halves, in one collector that **runs even when the API call
failed** — an unreachable console is exactly when connectivity is most
interesting.

- **Address.** The gateway reports its *public* WAN address in the same
  `ipAddress` field every other device uses for a LAN address, so `find_gateway`
  picks the device whose address is not RFC1918 rather than hardcoding a model.
  The test is `not ipaddress.is_private`, not `is_global`: an ISP using
  carrier-grade NAT hands out a `100.64.0.0/10` address, which `is_global`
  rejects even though it is exactly the address we want to record. Note that
  Python also classifies the documentation ranges (`203.0.113.0/24`,
  `198.51.100.0/24`) as private, so those are useless as test fixtures — use a
  real public address. `gatewayName` in the config overrides the heuristic for
  double-NAT setups, where no device has a public address at all.
- **Reachability.** `ping 1.1.1.1` (up to three single-packet attempts — one
  dropped packet on a wireless uplink is not an outage) plus a `getaddrinfo`
  lookup of a stable hostname. They fail for different reasons and deserve
  different responses, so the classification distinguishes them: ping fails →
  `no_route` (the line or the ISP); ping succeeds and DNS fails →
  `dns_failure` (a working line with broken name resolution, usually a resolver
  setting). If ping fails the DNS result says nothing useful, so `no_route`
  wins.

**Outages are reported retrospectively, because they must be.** While the link
is down there is nobody to notify and no agent turn to fire. So `down_since` is
persisted to `state.json` on every poll — including polls where the API call
failed — and the `wan_recovered` event carries the full window and duration for
the consumer to report after the fact. A six-hour outage spanning 72 polls
produces exactly two events. An install that happens *during* an outage seeds
the down state silently and still reports the gap on recovery.

## Statistics

`<root>/metrics/YYYY-MM-DD.jsonl` — one line per poll, appended, never
rewritten. Each line is a full snapshot of every connected client:

```json
{"ts":"…","clients":[{"mac":"…","name":"SonosZP","type":"WIRELESS",
  "rx_delta":12345,"tx_delta":600000,"satisfaction":96,"link_mbps":54,
  "uplink":"Express Mesh 1","tx_retries_pct":3.4,"signal":-46,"channel":44,
  "essid":"DARKNET","oui":"Sonos, Inc."}]}
```

**Metrics are not events, and must never become them.** `events.jsonl` answers
"what changed?" — it is small, transition-only, read start to finish by a
human-facing agent, and every line in it is something worth someone's
attention. Metrics answer "what is the trend?": they are written on *every*
poll whether anything happened or not, at roughly 8 KB a line. Mixing them
would bury the events in a hundredfold of routine samples and destroy the one
property that makes `events.jsonl` useful. The statistics collector therefore
never calls `new_event`, `append_events` or `write_pending`, and by the time it
runs both files are already written and durable.

**A client is its MAC, and only ever its MAC.** Baselines, metrics rows and
every aggregation in `stats.py` key on `mac`. A display name is presentation:
it is never a key, never a grouping, never a count. Both directions of that
break on real data here:

- **One device, several names.** one wired Mac was written under
  three names (two hostnames, plus one of them in different casing) across 18 changes in 56
  polls, on the same switch port at the same speed. The first two were both
  arriving as `hostname` — the console alternated between two values for the
  same field — so a fixed `name` → `hostname` → `mac` precedence cannot fix
  this on its own. It is deterministic for a given record; the record moves.
- **One name, several devices.** `SonosZP` is the hostname of **seven**
  separate speakers, and `Mac` of two. Anything that grouped by name would
  merge them into one.

So the name is resolved once by `_resolve_display_name` and then *held still*
by `_stabilise_name`: a better source (an alias someone set beats a hostname
the device announced) is adopted immediately, a change of casing is not
treated as a rename, and anything else must appear on **two consecutive polls**
before it replaces the stored name. Applying that last rule to a downgrade too
is what lets a genuinely deleted alias fall back to the hostname instead of
pinning a name the console no longer knows. `name_src` is written into every
metrics row so a name that does move is attributable rather than mysterious.

`stats.py: resolve_labels` then picks **one label per MAC** for printing: the
best source seen in the window, most recent row breaking the tie. That is why
old files need no migration — rows written before `name_src` existed rank as
hostnames, which is what they were. Picking the most *frequent* name instead
would have printed a hostname this client stopped using hours earlier. Labels
shared by more than one MAC get the last two octets appended, so seven
identical `SonosZP` rows become seven readable ones.

**Randomized MACs do not change any of this.** Several clients here report an
empty `oui`, which is the marker. A randomized MAC is stable for as long as the
device keeps using it, so it remains the correct key; clients are deliberately
**not** unified across a MAC change, because there is nothing trustworthy to
unify them by.

**A rotating phantom is silenced by prefix, never by heuristic.** One access
point (an AC Pro) periodically presents a client `aa:bb:cc:dd:XX:YY` — wired, no IP,
zero bytes ever, `sw_mac` equal to the AP's own MAC — and the last two octets
change every few days or weeks, so each rotation used to be a fresh
`new_client`. `clients.ignore_mac_prefixes` in `config.local.json` lists such
prefixes: a matching MAC is still added to `known_clients`, with
`"ignored": true`, but emits no event, so dropping the prefix later cannot
release a backlog of alerts. Prefixes must be whole colon-separated octets; an
empty or malformed one is warned about and dropped. A rule like "wired,
randomized, behind an AP" was rejected on purpose: it also describes an
unknown machine plugged into an AP's second port, which is what the event is
for. Statistics are unaffected — the phantom still appears in `metrics/`.

**Counter resets are the whole difficulty.** The byte counters are cumulative
for the life of a client's *session* and start again at zero when it
reconnects. Summing them naively measures uptime rather than usage;
subtracting naively goes negative across a reconnect. `_counter_delta`:

- **No stored baseline at all → delta 0**, and the counter is merely recorded.
  This is the same "seed silently" rule as `devices` and `known_clients`
  starting at `null`. Reporting the raw counter would attribute a week of
  accumulated traffic to one five-minute poll and put a false spike in every
  chart on the day the collector was installed.
- **Current below the stored value → the whole of it is the delta**, and the
  baseline resets. A counter can only go down by restarting.
- Otherwise the delta is `current - previous`.

A client that vanishes for a poll and returns is correct either way: if the
session survived the counter is higher (ordinary delta), if it reconnected the
counter is lower (reset rule). Its baseline is kept while it is away.

**Rotation is by filename date, not mtime.** Appending moves an mtime every
five minutes, so an mtime horizon would measure when a file was last written
rather than which day it covers, and a file restored from a backup would carry
a timestamp with nothing to do with its contents. A name that does not parse is
left alone rather than guessed at. `write_metrics` also does not `fsync`,
unlike `append_events`: an event is a fact that must survive a crash, whereas a
lost metrics line is one missing sample in a trend.

`stats.py` reports over this history and **makes no API calls at all** — it
reads the files and nothing else, so it is safe to run mid-poll:
`--top [N]`, `--client <mac|name>`, `--links`, `--quality`, each with `--days`
and `--json`. `--links` flags any wired client negotiating below 1000 Mbps and
says so carefully: `stat/device` reports `max_speed` as `null` on every port,
so the API cannot tell us whether a port is gigabit-*capable*, only what the
link came up at.

## Scheduling

`install.sh` renders the wrapper and plist with absolute paths, installs to
`~/Library/LaunchAgents/`, and loads the agent:

```bash
./install.sh
launchctl kickstart -k gui/$(id -u)/com.jztone.unifiwatch   # poll now
launchctl print gui/$(id -u)/com.jztone.unifiwatch | head -n 30
./install.sh uninstall
```

`StartInterval` 300, not `StartCalendarInterval`: this is a cadence, not an
appointment. While the Mac is asleep no poll runs and launchd fires once on
wake rather than replaying the misses — which is what we want, since the poller
compares against the last state it saw. A gap costs resolution, not
correctness: a device that went down and came back inside the gap is missed
entirely, and one that is still down is reported on the first poll after wake.

**This job is independent of every other agent on this machine.** It holds no
shared resource and depends on no other job, so it keeps collecting while the
01:00 sync runs and while the 03:30 stop job has other services down. If those
jobs ever take the *network* down, that is a WAN event like any other and is
reported as such.

### The scheduled job never touches the repo

The repo lives on the ProtonDrive CloudStorage mount, which a launchd job
**cannot read**:

```
python3: can't open file '…/UniFiWatch/watch.py': [Errno 1] Operation not permitted
```

An interactive shell reads the same path fine, so this is not a permissions bug
you can chmod away, and the mount is often unhydrated after a deferred wake
besides. So `install.sh` copies `watch.py`, `notify.py` **and**
`config.local.json` to `~/Library/Application Support/UniFiWatch/` (the config
`chmod 600`), renders `run.sh` there, and points the plist at that wrapper. Logs
go to `~/Library/Logs/UniFiWatch/`, also local — flushing a log on the
CloudStorage mount intermittently raises `EDEADLK` ("Resource deadlock
avoided").

`notify.py` is installed because the wrapper runs it on every poll, so the repo
copy is as unreachable to it as `watch.py` is. It needs no path wiring: Python
puts a script's own directory first on `sys.path`, so its `import watch` finds
the installed copy, and that copy resolves `CONFIG_FILE` relative to its own
location and reads the installed config. That is also why `run.sh` **derives**
the path as `dirname(watch.py)/notify.py` rather than taking a `__NOTIFY_PY__`
placeholder: sitting beside `watch.py` is a correctness requirement, not a
convention, and a fourth templated path would be one more thing to drift.
`status.py` and `stats.py` are *not* installed — they are interactive tools, run
by a human from the repo.

**The interpreter must be on local disk as well**, and this is easy to get
wrong. `install.sh` takes `command -v python3`, which picks up whatever venv
happens to be first on `PATH` — and a project venv under CloudStorage is a
symlink whose resolution needs the very mount launchd may be denied. That fails
at `execvp` before `watch.py` starts:

```
sandbox-exec: execvp() of '…/.venv/bin/python3' failed: Operation not permitted
```

So the script resolves through any venv shim to the real interpreter
(`sys._base_executable`, else `realpath(sys.executable)`) and falls back to
`/usr/bin/python3` if the result still sits on the mount. UniFiWatch is
stdlib-only, so it loses nothing by not using a project venv.

The consequence: **the installed copies are build artifacts.** Editing
`watch.py`, `notify.py` or `config.local.json` in the repo changes nothing until
`./install.sh` is re-run. The wrapper exits `78` (`EX_CONFIG`) if `watch.py` or
its config is missing, so a broken install says so in `wrapper.log` instead of
failing silently every five minutes. A missing `notify.py` is deliberately *not*
fatal — it is logged and skipped, because losing the wake must not also lose the
monitoring, and the poll has already written everything by that point.

## Conventions

- **Config precedence:** environment variable → `config.local.json` key →
  module constant. Every path from either source gets `.expanduser()`.
- **Env overrides:** `UNIFIWATCH_ROOT`, `UNIFIWATCH_HOST`,
  `UNIFIWATCH_API_KEY`, `UNIFIWATCH_SITE_ID`,
  `UNIFIWATCH_DEFAULT_OFFLINE_MINUTES`, `UNIFIWATCH_LOG_DIR` (set by the
  launchd wrapper), `UNIFIWATCH_OPENCLAW`, `UNIFIWATCH_NOTIFY_AGENT`,
  `UNIFIWATCH_NOTIFY_TARGET`, `UNIFIWATCH_NOTIFY_MESSAGE`,
  `UNIFIWATCH_NOTIFY_CHANNEL`. Env vars are also the test seam:
  point `UNIFIWATCH_ROOT` at a scratch directory to exercise the collectors
  without touching real state, and `UNIFIWATCH_OPENCLAW` at a stub to exercise
  `notify.py` without waking a real agent (`tools/test_notify.sh` also sets
  the notify agent and target to neutral values, so its asserted argv never
  depends on a real config). `UNIFIWATCH_OPENCLAW` is env-only,
  with no `config.local.json` key — the one deliberate exception to the
  precedence rule above, because that file is the *site's* credentials and this
  is a property of the *machine*.
- **Logging:** to stdout and `<log_dir>/unifiwatch.log`, where `<log_dir>` is
  `UNIFIWATCH_LOG_DIR` if set, else `<root>/logs`. `%`-style lazy interpolation
  in log calls, never f-strings.
- **Exit codes:** `0` success · `1` general/partial error (including a missing
  or incomplete config, and an unreachable console) · `2` auth failure.
- **Notifications are for failures only** — auth and config errors. Network
  events go to `pending.json` for the consumer to report; notifying on those
  too would mean every incident arrives twice.
- **No collected state is written on an auth failure.** Both API calls happen
  before the lock is taken and before `state.json` is read, so a rejected key
  returns 2 without touching `state.json` and without appending an event — a
  bad key can never be recorded as a network that suddenly has no devices and
  no clients. The acknowledgement inbox is the one thing that *does* run first,
  deliberately: it is a purely local file operation that owes nothing to the
  console, and draining it after the fetch would mean an expired key — which
  returns 2 on every poll thereafter — left the consumer permanently unable to
  acknowledge anything. It only ever flips flags on events already on disk.
- **Per-collector resilience:** collectors are run from a list and each is
  wrapped in its own `except Exception` (`# noqa: BLE001` with the reason). One
  that raises is logged and counted; the rest of the poll continues and the run
  exits 1. Adding a check means adding a function and one line in `main`.
- **The legacy API may never affect the official one.** Anything reading
  `/proxy/network/api` runs last, catches everything, stays out of the
  `collectors` list so it cannot reach the exit code, and never touches
  `events.jsonl` or `pending.json`. If a firmware update takes the endpoint
  away, a graph stops updating and nothing else changes.
- **Never commit** `config.local.json`, logs, state, or events.
- Never add Claude/Anthropic **attribution** to commit messages or PR text — no
  `Co-Authored-By` trailers, no "Generated with …" footers, no tool credit in
  prose. This is about authorship claims, not tooling: `.claude/settings.json`
  is tracked deliberately (`settings.local.json` is gitignored, since per-user
  permission approvals accumulate there).

## Issue tracking

All bugs and feature requests are tracked on GitHub:
https://github.com/jztone/UniFiWatch/issues

**Labels:**
- Type: `bug`, `feature`, `known-limitation`
- Priority: `priority:high`, `priority:medium`, `priority:low`
- Area: `area:poller`, `area:events`, `area:stats`, `area:install`, `area:docs`
- Status: `cannot-reproduce`

Area meanings: `area:poller` — `watch.py` collection loop, both API clients, the
device/client/WAN collectors. `area:events` — event generation, the
acknowledgement inbox, `notify.py`. `area:stats` — metrics collection in
`watch.py` and the `stats.py` reports. `area:install` — `install.sh`,
`run.sh.template`, the plist, launchd scheduling. `area:docs` — the markdown.

**Workflow:**
- New bugs/features → open issue with body, labels, and acceptance criteria
- Fixes → comment on issue with commit hash + verification output (the static
  checks from *Verifying a change*, plus a real `python3 watch.py` run or the
  relevant scratch-root scenario), then close
- Do NOT close stale issues without verifying the fix works against the live
  console or a scratch root — a network-dependent bug that "looks fixed" in the
  code is not fixed

**Source of truth rule:** GitHub issues are the canonical bug list. Do not
duplicate bug tracking in other markdown files, code comments, or memory. If a
bug is mentioned in chat or in code TODOs, it should be opened as a GitHub issue.

**Listing open issues:**
```bash
gh issue list --state open
gh issue list --label "priority:high"
gh issue list --label "area:poller"
```

## Gotchas / when editing

- **Stay stdlib, and keep `watch.py` self-contained.** The installed copy sits
  alone in a support directory with nothing but its config beside it; a local
  import would break the scheduled job and not the manual one.
- **Re-run `./install.sh` after editing `watch.py`, `notify.py` or
  `config.local.json`.**
- `status.py`, `stats.py` and `notify.py` import `watch`, so they must be run
  from a directory where it is importable — the repo, or for `notify.py` the
  install directory, where `install.sh` puts a copy beside `watch.py` for the
  scheduled poll. This is also why the statistics *collector* lives in
  `watch.py` and only the *reporting* lives in `stats.py`: nothing may assume
  the repo is reachable at poll time.
- **`notify.py` must not take `run_lock`.** It is blocking and not reentrant,
  and it belongs to the poll; a notifier waiting on it would be blocking on the
  very files it wants to read. `notify.py` has its own `.notify.lock`, taken
  `LOCK_NB` so an overlap skips instead of queueing.
- **A legacy-API 401 does not mean the key is bad.** `/proxy/network/api`
  answers 401 for an unrecognised site name, so anything reading it must not
  route errors through `api_get`/`AuthError` — that path exits 2 and fires a
  notification. `legacy_api_get` exists to keep the two apart; do not
  "simplify" them back together.
- `metrics/` grows by roughly 2 MiB a day at 27 clients, so ~200 MiB at the
  90-day default. `stats.retain_days` is the lever. Unlike `events.jsonl` this
  one is pruned automatically.
- **`run_lock` is not reentrant, and nesting it hangs rather than fails.** It
  opens a fresh descriptor per call and `flock` locks per open-file-description,
  so a second acquire from inside a block already holding it blocks forever.
  This is why `process_ack_inbox` runs *before* the poll's `with run_lock(root)`
  block instead of inside it, and takes the lock itself. If you add another
  step that needs the lock, keep the acquisitions sequential.
- Device `state` is a string with more values than `ONLINE`/`OFFLINE`. Treat
  anything unrecognised as transitional rather than assuming it means down.
- `events.jsonl` grows without bound. Transitions are rare enough that this is
  bytes per day, but `--acknowledge` rewrites the whole file, so if it ever
  reaches a size where that matters, rotate it rather than making the ack
  incremental.
- Timestamps in state and events are UTC ISO 8601; only `pending.json`'s
  `generated` is local. Durations are computed from stored timestamps, so they
  stay correct across DST and across a sleeping Mac.

## Verifying a change

Static, from the repo, no key required:

```bash
python3 -m py_compile watch.py status.py stats.py notify.py
bash -n install.sh && bash -n run.sh.template && bash -n tools/test_notify.sh
plutil -lint com.jztone.unifiwatch.plist
bash tools/test_notify.sh    # notify.py end to end, against a stub openclaw
```

`tools/test_notify.sh` is the one behavioural suite in the repo, because the
deduplication in `notify.py` is the one thing nothing else would notice
breaking: a regression there wakes the agent every five minutes on events it
already reported. It runs under `/tmp/uw-test` with `UNIFIWATCH_OPENCLAW`
pointed at a stub that records its argv, so it touches no real state and wakes
nobody.

Behavioural, without touching real state — point the root at a scratch
directory (`export UNIFIWATCH_ROOT=/tmp/uw-test`) and drive the collectors with
canned payloads:

- **Bad API key** → exits 2, notification fires, and the scratch root contains
  no `state.json`, `events.jsonl` or `pending.json`.
- **Two runs, nothing changed** → the second writes no new events.
- **Device offline** → nothing at first; backdate `offline_since` in the scratch
  `state.json` past the threshold and re-run for exactly one `device_offline`;
  flip it back online for one `device_online` carrying the duration.
- **New client** → one `new_client` naming the uplink device.
- **`--acknowledge-all`** → `pending.json` drops to `count: 0`.
- **Inbox** → write `{"acknowledge": [<id>, <id>, 9999]}`; the next poll
  acknowledges the real ids, ignores the bogus one, deletes the inbox and
  shrinks `pending.json`. Applying the same file twice leaves `events.jsonl`
  byte-identical and un-rewritten.
- **Inbox, without polling** → write one and call `suppressed_ack_ids`: it must
  return the id immediately, while `pending.json` still lists the event. That
  staleness is the point — it proves the subtraction, not the poll, is what
  answered.
- **Malformed inbox** → the poll still exits 0, the file becomes
  `ack-inbox.bad-<ts>.json`, and a later valid inbox still applies.
- **Statistics, two polls apart** → the first line is all zeros (seeding), the
  second carries small positive deltas. A delta anywhere near the size of a
  cumulative counter means the reset rule or the first-sighting rule broke.
- **Counter reset** → raise a `state["stats"]["<mac>"]` counter above what the
  API reports and re-run: the delta must equal the current value, never be
  negative, and the baseline must be rewritten downwards.
- **A client in only one of two polls** → no crash, no row in the poll where it
  was absent, and its baseline survives for its return.
- **Broken legacy endpoint** → point `stats.site` at a name that does not exist
  (which answers **401**, not 404). The run must still exit **0** — not 2 —
  with no notification, and `events.jsonl` and `pending.json` byte-identical
  before and after. This is the isolation regression test; run it whenever
  anything near the fetch path changes.
- **Both client types** → a wired row carries `sw_mac`/`sw_port` and no RF
  fields; a wireless row carries `signal`/`channel`/`essid`/`tx_retries_pct`
  and no switch fields.
- **A flapping hostname** → feed the same MAC `A, B, A, B, B`. The recorded
  name must stay `A` until `B` has appeared twice in a row, and `--top` must
  show one row carrying the combined total throughout. An alias must be adopted
  on the first poll that carries it, and a case-only change must not start a
  confirmation cycle.
- **One name on several MACs** → seven clients all called `SonosZP` must
  produce seven distinct labels; a name held by one MAC prints unchanged.
- **A client with neither `name` nor `hostname`** → labelled with its MAC, and
  all four reports run.
- **`--client`** → by MAC, by the current label, by a name the client stopped
  using earlier in the window, in either case and with `-` or `:` separators,
  must all resolve to the same MAC with identical totals.

- **The installed copy with the repo gone** → the point of installing at all,
  so test it with the mount actually denied rather than by reading the code:

  ```bash
  printf '(version 1)\n(allow default)\n(deny file-read* file-write* (subpath "%s/Library/CloudStorage"))\n' "$HOME" > /tmp/norepo.sb
  cd / && sandbox-exec -f /tmp/norepo.sb env -i HOME="$HOME" PATH=/usr/bin:/bin \
      "$HOME/Library/Application Support/UniFiWatch/run.sh"
  ```

  That reproduces launchd's exact `Operation not permitted` and must still
  complete a full poll — proving every script on the scheduled path resolves
  its root and config from the install directory and not from the repo.

Against the real network: run `python3 watch.py` twice (the second must be
silent), then unplug the mesh AP for less than its threshold (nothing), then
for longer (one event, and a recovery event with the right duration on
replug).
