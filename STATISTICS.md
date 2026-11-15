# STATISTICS.md — Data surface, what is collected, and the option space

Consolidates and replaces four earlier documents: `UNIFI_API_STATISTICS.md`,
`unifi-dream-machine-api-stats.md`, `PLAN-api-statistics.md` and
`FEATURE_IDEAS.md`. Feature proposals now live in `PLAN-weekly-report.md`;
everything descriptive lives here.

Confidence levels are kept from the survey: claims marked **[repo]** were
verified against this repository's code; claims marked **[unprobed]** come from
general UniFi controller knowledge and have not been tested against this
console.

---

## 1. API surfaces on the Dream Machine

Three exist. UniFiWatch uses the first two.

| Surface | Base | Used for | Stability |
|---|---|---|---|
| **Official Network Integration API** | `/proxy/network/integration/v1` | Events: device state, firmware, client roster, WAN address | Versioned, documented |
| **Legacy controller API** | `/proxy/network/api` | Statistics only: per-client traffic and RF | Undocumented, may vanish in any firmware update |
| UniFi Site Manager (cloud) | unifi.ui.com | Nothing | Not used — cloud dependency, limited data |

Both local APIs authenticate with the same site-scoped `X-API-KEY`. No cloud
account, no cookie login. TLS verification is disabled for this pinned LAN host
only — the key authenticates us, not the certificate.

**The isolation rule** [repo]: the legacy API runs last, has its own fetch
(`legacy_api_get`, returns `None` on every failure), sits outside the
`collectors` list, and can never touch `events.jsonl`, `pending.json` or the
exit code. If it disappears, graphs stop; alerting does not. Part of why:
the legacy API answers **401** (not 404) for an unknown site name, so a shared
fetch path would let a typo impersonate an expired API key.

## 2. What is collected today

Three GET requests per five-minute poll:

| Request | API | Feeds |
|---|---|---|
| `/sites/<siteId>/devices` | Official | device online/offline, firmware, WAN address |
| `/sites/<siteId>/clients` | Official | client roster, `new_client` events |
| `/s/<site>/stat/sta` | Legacy | all per-client statistics |

Events generated (transition-only, append-only): `device_offline` /
`device_online` (with outage duration), `firmware_available` /
`firmware_updated`, `new_client`. WAN outages are reported retrospectively.

### Per-client metrics row (legacy `stat/sta`, normalised)

22 fields are taken per client; wireless and wired records are flattened into
one shape. The operative subtleties:

- **Byte counters are relabelled to client POV.** The API is network-POV:
  its `tx` is the client's *download*. Recorded as `rx_delta` = download,
  `tx_delta` = upload.
- **Counters reset on reconnect.** Delta logic: no baseline → delta 0 (seed);
  current < stored → delta = current (wrap); else difference. No negative
  deltas, no installation-day spikes.
- **`link_mbps` is negotiated PHY rate**, not throughput. Wireless records
  only carry the TX direction today.
- **`tx_retries_pct` = `wifi_tx_retries_percentage`** directly. The raw
  counters (`tx_retries`, `wifi_tx_attempts`) are lifetime values and were
  deliberately rejected.
- **`satisfaction` is unusable as a quality signal** (98 observed on a client
  with 19 % retries). Use retry percentage.
- **Name resolution:** `name` → `hostname` → MAC, with two-poll confirmation
  before adopting a change, case-changes ignored, collisions suffixed with the
  last two MAC octets. `name_src` is stored per row. Identity is always the
  MAC.
- **Randomised MACs** have empty `oui` and cannot be unified across rotations.

### Storage

| File | Content | Growth |
|---|---|---|
| `metrics/YYYY-MM-DD.jsonl` | one line per poll, all clients | ~2 MiB/day @ 27 clients; pruned at `stats.retain_days` (default 90) |
| `state.json` → `stats` slice | per-MAC counter baselines, names | bounded |
| `events.jsonl` | transitions only | bytes/day, unbounded, rotation is a later concern |
| `pending.json` | unacknowledged events | regenerated per poll |

### Reports (`stats.py`, read-only, no API calls)

`--top [N]` bandwidth ranking · `--client <mac|name>` one client's history ·
`--links` wired links below gigabit · `--quality` worst satisfaction/retries.
Options: `--days N`, `--json`.

## 3. The option space, in tiers

### Tier 1 — already on disk, nothing reads it [repo]

Nine fields are written into every metrics row and consumed by no report:

`noise` · `channel` · `radio_proto` · `essid` · `oui` · `dev_vendor` ·
`os_name` · `satisfaction_reason` · `sw_mac`

They unlock, retroactively against up to 90 days of existing history, at zero
API cost: traffic per SSID and per band, channel congestion, true SNR
(`signal − noise`), per-switch-port utilisation, vendor/device mix, and
randomised-MAC identification (empty `oui`).

Caveats: `dev_vendor` / `os_name` are opaque integers into Ubiquiti's
fingerprint database with no local lookup table, and `satisfaction_reason` is
an undecoded bitmask (observed values 2, 8, 16, 2048, 32768).

### Tier 2 — in the response, not parsed [repo]

Costs no new request, but only accrues from the change forward:

- **`ap_mac` / `bssid`** — AP association; sampled per poll this yields
  roaming history. On a mesh network, the most interesting unrecorded signal.
- **`uptime` / `assoc_time`** — session length, silent-reconnect churn.
- **`rx_packets` / `tx_packets`** — frame-size profile.
- **`rx_rate`** — the missing PHY direction.
- `rssi`, `channel_width`, `vlan`, `authorized`, `anomalies`.

### Tier 3 — endpoints not called

Grounded in this repo:

- **Official `GET /sites/<siteId>/devices/<id>`** — `uplink.deviceId`,
  `ports`, `radios`: per-port and per-radio device statistics plus real mesh
  topology. One request per device; the only expansion that may legitimately
  produce new *events*, being on the official API.
- **Legacy `stat/device`** — per-device CPU/memory, uptime, per-radio channel
  utilisation (`cu_total`, `cu_self_rx`, `cu_self_tx`), TX power. Probed once:
  `max_speed` is null. The only way to tell channel-44 congestion caused by
  neighbours from own mesh backhaul.

[unprobed] — standard on legacy controllers generally, would need a probe:
`stat/report/{hourly,daily}.{site,ap,user}` (the console's own pre-aggregated
history — could backfill from before UniFiWatch existed), `stat/health`,
`stat/dpi`, `stat/event` / `stat/alarm`, `stat/sysinfo`.

### Tier 4 — confirmed unavailable [repo]

Switch-port *capability*. `max_speed` is null on every port, so a gigabit port
negotiating 100 Mbit cannot be told apart from a Fast Ethernet port. `--links`
words its output accordingly.

## 4. Constraints on all future work

The repo's own rules, not preferences:

1. **Metrics never become events.** Tier 1/2 work is read-side or metrics-side
   only — never `new_event`, `append_events`, `write_pending`.
2. **The legacy API never affects the official one.** Runs last, own fetch,
   own exception handling, outside `collectors`, cannot reach the exit code.
3. **Standard library only.** No pip, no third-party HTTP.
4. **Nothing on the scheduled path may assume the repo is reachable** —
   collectors live in `watch.py`; the installed copy stands alone.

Explicitly avoided: a web dashboard, a database, cloud dependencies, alerts
sourced from the legacy API, disconnect events for every client, per-poll
device-detail fetching without a concrete consumer.
