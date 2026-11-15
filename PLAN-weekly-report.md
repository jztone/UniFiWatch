# PLAN-weekly-report.md — Louisa delivers weekly network statistics

Goal: a weekly report in `#network`, written by Louisa Guy, summarising the
week's traffic, Wi-Fi quality, outages and new clients.

## The constraint that shapes the design

**Louisa has no shell.** She cannot run `stats.py`, and she cannot read a week
of raw metrics — ~14 MiB of JSONL is three orders of magnitude past her
context. Granting her `exec` for reporting would widen the injection surface
of the entire agent for zero gain, so that option is rejected outright.

The division of labour is therefore the one that already runs everything else:
**the script computes, the agent presents.** A rollup generator produces one
small JSON file; a scheduled wake hands it to her; a skill tells her how to
read it. Same pattern as `pending.json` → `natverk-rapport`, applied to
statistics.

A second constraint falls out of `FEATURE_IDEAS` and stays binding: the
generator is read-side only. It reads `metrics/` and `events.jsonl` and writes
under `rollups/` — it never touches events, pending, or state.

## Architecture

```
metrics/*.jsonl ─┐
                 ├─ rollup.py (launchd, Mon 07:30) ─→ rollups/2026-W32.json
events.jsonl ────┘                                 └→ rollups/weekly-latest.json

OpenClaw cron (Mon 08:00) ─→ agent <agent>, "kör veckorapport"
                              └─ skill veckorapport: reads weekly-latest.json,
                                 checks freshness, reports to #network
```

**Staleness is the alarm.** If `rollup.py` fails, `weekly-latest.json` still
holds last week. The skill's first step is a freshness check; a stale file
makes Monday's report say *the rollup did not run* instead of presenting old
numbers as new. No separate failure-notification path is needed — the cron
wake doubles as the watchdog, exactly like the daily 09:00 health check does
for the event path.

---

## Phase 1 — the weekly report itself

### 1. `rollup.py` (repo)

New script beside `stats.py`. Imports `watch` for root resolution, reuses
`write_json_atomic`. Stdlib only. Runs from the install directory —
`install.sh` gains it in the copy list (the `has_pending.py` omission is the
cautionary tale; verify with the repo unmounted).

Reads the previous ISO week (Mon–Sun, `Europe/Stockholm`) from
`metrics/*.jsonl` by filename date and `events.jsonl` by `ts`. Tolerates a
truncated final JSONL line (a poll may append mid-read) by skipping malformed
lines; takes no lock.

Writes `rollups/<ISO-week>.json` and a copy to `rollups/weekly-latest.json`,
both atomic. Rollups are derived state: reproducible from raw metrics, never
backed up, safe to delete. Target size: under 10 kB.

Shape, v1:

```json
{
  "week": "2026-W32",
  "range": {"from": "2026-08-03", "to": "2026-08-09"},
  "generated": "2026-08-10T07:30:12+02:00",
  "polls": {"expected": 2016, "seen": 1994},
  "traffic": {
    "down_bytes": 0, "up_bytes": 0,
    "top": [{"name": "", "mac": "", "down_bytes": 0, "up_bytes": 0}]
  },
  "wifi": {
    "clients_over_retry_threshold": [
      {"name": "", "mac": "", "avg_retries_pct": 0.0, "ap": ""}
    ],
    "worst_snr": [{"name": "", "snr_db": 0}],
    "by_band": {"2.4": {"clients": 0, "bytes": 0}, "5": {"clients": 0, "bytes": 0}}
  },
  "wan": {"outages": 0, "outage_minutes": 0, "address_changes": 0},
  "devices": {
    "offline_incidents": [{"name": "", "count": 0, "total_minutes": 0}],
    "firmware_changes": []
  },
  "clients": {
    "new": [{"mac": "", "name": "", "first_seen": "", "randomized": true,
             "uplink": ""}]
  }
}
```

Two deliberate choices in that shape:

- **`randomized` is computed by the script**, from the locally-administered
  bit. MAC classification has one demonstrated failure in an LLM's hands
  (`da:` read as universal); it is a bit test and belongs in code. The skill
  then only presents.
- **`polls.expected` vs `seen`** makes collection gaps visible. A week with
  300 missing polls is a week where the traffic totals undercount, and the
  report should say so rather than present false precision.

`by_band` and `worst_snr` come from tier-1 fields (`radio_proto`, `signal`,
`noise`) — on disk today, read by nothing. This is the cheapest real content
in the report and works retroactively.

Dropped from v1: `satisfaction` (documented unusable), `satisfaction_reason`
(undecoded bitmask — separate backlog item), `dev_vendor`/`os_name` (no lookup
table).

### 2. Scheduling (repo)

Second launchd plist, `com.jztone.unifiwatch.rollup.plist`:
`StartCalendarInterval { Weekday=1, Hour=7, Minute=30 }`, same wrapper
pattern, rendered and loaded by `install.sh`. Monday 07:30 clears the
01:00–03:45 window (PlaudeSync, Catherine, NAS backup) with hours to spare.

### 3. The wake (OpenClaw)

Created together with the still-pending daily health check — both are
unconditional cron jobs, and `--fallbacks` is carried explicitly on both
because the agent-config fallback chain remains unverified:

```bash
openclaw cron add \
  --name "Veckorapport nätverk" \
  --cron "0 8 * * 1" \
  --tz "Europe/Stockholm" \
  --session isolated --agent <agent> \
  --message "kör veckorapport" \
  --timeout-seconds 300 --expect-final \
  --announce --channel slack --to <slack channel id> \
  --fallbacks "mistral/mistral-small-latest,openrouter/anthropic/claude-sonnet-5"
```

Verify with `openclaw cron list` that both jobs exist and neither carries a
trigger script.

### 4. Skill `veckorapport` (workspace)

`~/.openclaw/workspace-guy/skills/veckorapport/SKILL.md`. Procedure:

1. Read `~/UniFiWatch/rollups/weekly-latest.json`. Absolute path.
2. **Freshness first:** `week` must be the ISO week that ended yesterday and
   `generated` within 48 h. Otherwise report that the rollup has not run —
   that is the whole report — and stop.
3. Present in Swedish, phone-readable, this order: traffic and top clients →
   Wi-Fi quality → outages → new clients. Flag a poll gap if
   `seen`/`expected` is materially short.
4. Context from `TOOLS.md`, not invention: the four clients above 14 %
   retries are the established baseline and only worth words if the set
   *changed*; JUNI-AP offline incidents are usually the socket; an unchanged
   channel-44 picture is not news.
5. **No acknowledgement.** The rollup is not the event queue; `ack-inbox.json`
   is never touched by this skill.

Tone rules unchanged: numbers over adjectives, one line per fact, nothing
raised unless the week actually contained something.

### 5. Workspace consistency (small but required)

- `AGENTS.md`: "You have one skill" becomes a two-skill table
  (`natverk-rapport` = events, `veckorapport` = statistics); the wake-path
  section gains the Monday cron.
- `TOOLS.md`: `rollups/weekly-latest.json` added to the paths table,
  read-only.

### 6. Tests (repo, with the change — not after)

Extend the pattern `tools/test_notify.sh` set: canned metrics and events under
a scratch root, then assert — correct ISO-week windowing across a month
boundary; counter math matches a hand-computed fixture; truncated last line
skipped without error; empty week produces a valid all-zero rollup; `randomized`
correct for `da:`/`5a:`/`b4:` prefixes; atomic write leaves no partial file.

### Definition of done

Monday morning: rollup file written 07:30, report in `#network` 08:00, and a
deliberately staled `weekly-latest.json` produces the "rollup did not run"
report instead of last week's numbers.

---

## Phase 2 — make the report earn its place

In value order, all read-side:

1. **Week-over-week deltas.** `rollup.py` reads the previous week's rollup and
   embeds a `previous` block; the skill reports direction, not just level.
   Traffic up 40 % is a sentence; 212 GB is a number.
2. **Retry-set change detection.** The baseline four are config, in
   `config.local.json` under a `report` key; the rollup lists entrants and
   leavers. This is backlog item "retry threshold on real numbers" landing in
   its natural place.
3. **Channel load** (`channel` field, tier 1) — clients and bytes per channel,
   which quantifies the 44-vs-116 imbalance instead of asserting it.
4. **`stats.py --wifi` / `--aps`** as CLI twins of the rollup content, for
   ad-hoc use. Same aggregation code, two consumers.

## Phase 3 — beyond existing data

Ordered, each independent, none required for the weekly report:

1. **`ap_mac`/`bssid` + `assoc_time` into metrics** (tier 2, one-line-ish
   parse change): roaming and reconnect churn per week. Accrues from the
   change forward — the earlier it lands, the sooner a week of history exists.
2. **WAN latency/DNS sampling** → `wan-metrics/`, p50/p95 into the rollup.
   Degradation stays a metric, not an event, per the constraints.
3. **`summary.json` every poll** — context for *event* reports ("3 devices
   online, WAN fine"), not the weekly path. Separate consumer, separate file.
4. **Official device-detail collector** (tier 3, grounded) on a slow cadence:
   per-port and per-radio stats, real mesh topology. The only expansion
   allowed to produce new events — port-flap and uplink-change candidates.
5. **Opt-in important-client monitoring** (`clients.watch` config) —
   `client_offline` for the NVR, the cameras, the Mac mini itself.
6. **Probe `stat/report/*`** [unprobed]: if the console keeps its own daily
   aggregates, rollups can be cross-checked and history backfilled from before
   UniFiWatch existed.

## Not doing

Dashboard, database, cloud, legacy-sourced alerts, per-client disconnect
events, per-poll device-detail fetching — unchanged from `STATISTICS.md`.
`satisfaction_reason` decoding stays a standalone curiosity, not a dependency.

## Open decisions

1. **Report slot.** Monday 08:00 assumed. Sunday evening is the alternative if
   the week should close while it is still mentally "this week".
2. **Top-N size.** 5 assumed.
3. **Rollup retention.** Keep forever (they are tiny and make long-term
   questions cheap) — or mirror `stats.retain_days`. Forever assumed.
