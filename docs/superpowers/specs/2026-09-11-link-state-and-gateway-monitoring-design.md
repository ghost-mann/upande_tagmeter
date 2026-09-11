# Link state and gateway monitoring

**Date:** 2026-09-11
**Status:** approved, not yet implemented

## The problem

`Water Meter.online` is a single boolean derived from one rule (`sync.py:235`):

```python
online = 1 if (meter.last_seen and meter.last_seen >= _offline_cutoff()) else 0
```

where the cutoff is a flat `now - 30h` for every meter. It answers exactly one
question — "did a reading arrive recently?" — and it collapses causes that
demand completely different responses:

| What actually happened | Today's flag | What should happen |
|---|---|---|
| Meter faulty or removed | `offline` | Send a technician |
| Gateway down, meters fine | `offline` | Fix the backhaul; ignore the meters |
| Meter joined the other network server | `offline` | Re-join it to the SMP |
| SMP holds no AMR record yet | `offline` | Wait, or chase the vendor |
| Meter died 20 minutes ago | **`online`** | Nothing, for up to 30 more hours |

The last row is the reason the flat window is wrong, and the middle rows are
the reason a boolean is not enough.

## What we measured (2026-09-11)

Evidence this design rests on. All of it was verified live against the SMP.

**Gateways.** Two exist. `get_gateway_status` is already implemented and parsed
(`vendor/client.py:404`, `vendor/parse.py:170`) and has never been called by
anything:

| | `F04CD5FFFE01CF70` | `0C4EC0FFFE00E97F` |
|---|---|---|
| `online` | **`False`** | `True` |
| `statTime` | 2026-09-09 12:21 | 2026-09-11 10:23 |
| GPS | `0, 0` — no fix | `-1.2970528, 36.7771403` @ 1814 m |
| `gpsTimeSync` | false | true |

One of the two gateways is genuinely down and has been since 09 Sep. The SMP
web console's gateway *list* shows `1` in a column for both, so that column is
not liveness — only the API's `online` field is.

**Binding.** The SMP models meters as explicitly *bound* to a gateway. The
console shows `Binding Information（100/100）` on `0C4EC0FFFE00E97F`: all 100
meters are bound to the healthy gateway. This mapping is declared by the
vendor, not inferred from radio, and is authoritative for our purposes.

**Fleet state.** 96 of 100 meters reported within 24h. The four exceptions:

```
68750000076885   last_seen 2026-08-20 06:51   outcome ok            -> live on ChirpStack
68750000076921   last_seen 2026-08-20 06:49   outcome ok            -> live on ChirpStack
68750000076919   last_seen 2026-08-20 04:22   outcome ok            -> cause unconfirmed
68750000076954   last_seen NULL               outcome server_error  -> no AMR record
```

Per the README's "Fleet reality" section, the fleet is split across two network
servers per device via an OTAA join race, and `…076885` / `…076921` are
documented as live on ChirpStack. They are **not** dead hardware. Any design
that calls them "faulty" is wrong.

**`server_error` does not mean a platform fault.** The README records that
`get_latest_amr` returns HTTP 500 with an empty body for a meter that has no
AMR record. So `server_error` means "the SMP knows this meter but holds nothing
for it" — which is what the existing workspace tile already calls *No Data on
SMP*.

**Cadence.** The console shows `Meter reading cycle: 1` on every meter —
uniform across the fleet. A single configured default is therefore sufficient;
per-meter learned cadence is more machinery than this fleet needs.

## Design

### New doctype: `TagMeter Gateway`

Named by `gateway_id`.

| Field | Type | Source |
|---|---|---|
| `gateway_id` | Data (name) | seeded |
| `label` | Data | seeded |
| `site` | Data | seeded |
| `online` | Check | `get_gateway_status.online` |
| `last_heartbeat` | Datetime | `statTime`, converted to system tz |
| `latitude` / `longitude` / `altitude` | Float | API |
| `gps_time_sync` | Check | API |
| `mqtt_protocol` | Check | API |
| `last_polled_at` | Datetime | set by the sync pass |
| `last_poll_outcome` | Data | `Outcome.value` |

`statTime` arrives in `Europe/Amsterdam`, not UTC and not the meter timezone.
`parse_gateway_status` already handles this via `SERVER_TZ`; the storage path
must go through `_to_system_naive` like every other datetime in `sync.py`.

### New field: `Water Meter.gateway`

A Link to `TagMeter Gateway`. Nullable — a meter with no binding recorded
degrades to the meter-side rules below rather than erroring.

### New field: `Water Meter.link_state`

A Select, alongside the existing `online` checkbox. **`online` keeps its
current meaning and its current rule** so the existing `?online=1` / `?online=0`
workspace tiles, filters and `refresh_online_flags` callers continue to work
untouched. `link_state` is the diagnostic detail.

| State | Condition |
|---|---|
| `Reporting` | `last_seen` within one cadence |
| `Late` | missed one expected report |
| `Silent` | missed three, and its gateway is healthy |
| `Gateway Down` | missed three, and its bound gateway is unhealthy |
| `Never Seen` | `last_seen` is NULL and outcome is not `server_error` |
| `No Data on SMP` | `last_sync_outcome == "server_error"` |

Evaluated in that order, first match wins, so `No Data on SMP` and `Never Seen`
take precedence over any staleness verdict.

**Gateway health is consulted only at the `Silent` threshold.** A `Late` meter
reads `Late` whether or not its gateway is healthy — one missed report is not
yet evidence of anything, and promoting it to `Gateway Down` would raise a
network alarm on normal jitter.

`Silent` versus `Gateway Down` is the entire point of this work.

### Gateway health is heartbeat age, not the flag

A gateway counts as unhealthy when **either** `online` is false **or**
`last_heartbeat` is older than `gateway_stale_after_hours` (default 6,
overridable in `site_config.json`).

`F04CD5FFFE01CF70` is exactly why both conditions are needed: a gateway whose
heartbeat is two days old is down regardless of any flag. This is the same
failure of trust-the-flag reasoning that motivated the whole change — applying
the fix on the meter side while trusting a vendor boolean on the gateway side
would be inconsistent.

### Cadence

`tagmeter_expected_cycle_hours` in `site_config.json`, default 24, matching the
observed `Meter reading cycle: 1`.

- `Late` at **1.25×** the cycle (30h at the default) — the 25% grace absorbs
  normal jitter. This is deliberately the same 30h the current flat window
  uses, so `Late` begins exactly where `online` flips to 0 today. Nothing
  becomes noisier than it already is.
- `Silent` at **3×** the cycle (72h at the default).

Per-meter learned cadence is explicitly **out of scope** — revisit only if the
fleet stops being uniform.

### New sync pass: `sync_gateways()`

In a new module `gateway.py`, mirroring the shape of `sync.py`:

```python
@frappe.whitelist()
def sync_gateways() -> dict:
    """Poll every gateway. Two API calls, so it can run often."""
```

Scheduled **hourly** — two calls per hour is negligible against the SMP,
and gateway state is the input to every `Gateway Down` verdict, so it must
not lag the meter sweep.

`refresh_online_flags()` gains a second pass that computes `link_state` after
`online`, reading gateway health from the `TagMeter Gateway` rows rather than
calling the API. That keeps it pure SQL and free of API calls, as it is today.

### Seeding

`setup/import_gateways.py`, mirroring `setup/import_meters.py`: idempotent,
re-runnable, reads `data/kiwasco-gateways.tsv` (2 rows). Meter binding is set
from a `gateway` column added to `data/kiwasco-devices.tsv` — all 100 rows
currently `0C4EC0FFFE00E97F`.

### Alarms are unaffected

**Decision: alarm flags surface regardless of `link_state`.** A meter in
`Gateway Down` still shows its empty-pipe, reverse-flow, leak and burst flags,
and still appears in the Alarms tiles.

Rationale: alarm flags are the *last known* physical state of the pipe. A
backhaul outage does not make a burst pipe less real — suppressing it would
hide a physical hazard behind a network fault. The staleness is already
communicated by `link_state` and `last_seen`, which is the honest place for it.

### Workspace

Add a **Network** group to `workspace_block.py`:

- Gateways — all `TagMeter Gateway` records
- Gateway Down — `Water Meter?link_state=Gateway Down`
- Silent — `Water Meter?link_state=Silent`
- Late — `Water Meter?link_state=Late`

Existing Fleet, Telemetry, Valve Control, Dashboard and Alarms groups are
unchanged. The existing Online/Offline tiles keep working because `online` is
preserved.

### Dashboard (`/tagmeter`)

`www/tagmeter.py` `build_payload()` gains:

- `link_state` and `gateway` on each meter dict.
- A `gateways` list — one entry per `TagMeter Gateway` with `gateway_id`,
  `label`, `online`, `last_heartbeat`, hours since heartbeat, `healthy`
  (the combined rule), and the count of meters bound to it.
- `kpi.gateways_down` and `kpi.meters_behind_down_gateway`.

`www/tagmeter.html` gains:

- A **Network** view in the left nav, listing gateways with health, heartbeat
  age, GPS fix and bound-meter count.
- `link_state` as a column in the meters table and as a pill in the meter
  detail panel, so the reason for silence is visible next to the meter.
- A banner at the top of the dashboard when any gateway is unhealthy, naming
  it and the number of meters behind it. This is the one piece of fleet state
  that changes how every other number on the page should be read.

The existing recency bands (`live` / `recent` / `stale` / `cold`) stay as they
are. They describe *how long* since a reading; `link_state` describes *why*.
Both are useful and they are not redundant.

### Valve control on the dashboard

Valve toggling already exists: `valveUI()` (`tagmeter.html:881`) renders
Open/Close buttons that POST to `valve.set_valve` with the CSRF token, gated on
`can_command` and refused while `in_flight` is set. It is mounted at
`detail-valve` (fleet-grid detail) and `flow-valve` (Flow view).

The gap is that the **Valve Board is read-only**. Work required:

- Add an **Action** column to `valve-table` with per-row Open/Close buttons,
  reusing `valveUI`'s call path and its three guards (permission, in-flight,
  confirm dialog). No new endpoint — `valve.set_valve` is already whitelisted
  and does its own permission and eligibility checks server-side.
- Show `link_state` in the Valve Board's Reachable column, so an operator can
  see whether a queued command will sit behind a dead gateway or a dead meter.
- Fix the stale empty-state hint at `tagmeter.html:733`, which tells users to
  "open a Quinto meter in the desk and use the valve toggle" — it predates the
  dashboard toggle and now sends people away from the working control.

**Server-side authority is unchanged.** The dashboard must not gain any ability
the desk does not already have; `set_valve` remains the single entry point and
keeps enforcing that only Quinto Prepaid meters can be actuated.

## Data flow

```
hourly    1. sync_gateways()        -> TagMeter Gateway.online, last_heartbeat
          2. refresh_online_flags() -> Water Meter.online      (unchanged rule)
                                    -> Water Meter.link_state  (new)
every 4h  sync_fleet()              -> Water Meter.last_seen, readings (unchanged)
                                    -> calls refresh_online_flags() at the end
```

**Ordering matters.** `sync_gateways` must run before `refresh_online_flags`
within the hourly slot, or `link_state` is computed against gateway rows up to
an hour stale. Both are already `hooks.py` `scheduler_events.hourly` entries,
which Frappe executes in list order, so `sync_gateways` is listed first. It is
not a correctness bug if that order is ever violated — the next hour corrects
it — but it delays a `Gateway Down` verdict by one cycle.

`link_state` is computed, never hand-set. It is derived from `last_seen`,
`last_sync_outcome` and the bound gateway's health — all already persisted.

## Error handling

- **Gateway poll fails** — record `last_poll_outcome`, leave `online` and
  `last_heartbeat` at their previous values. Do not infer "down" from a failed
  poll; a network blip on our side is not a gateway outage.
- **Gateway never polled** (`last_heartbeat` NULL) — treated as unhealthy only
  after `gateway_stale_after_hours` from `last_polled_at`. A freshly seeded
  gateway must not mark 100 meters `Gateway Down` before its first poll.
- **Meter with no `gateway`** — falls back to meter-side rules; can reach
  `Silent` but never `Gateway Down`.
- **Both gateways unhealthy** — every bound meter that is silent reads
  `Gateway Down`. Correct, and loud, which is the intent.

## Testing

Existing suites are `tests/` (vendor layer, no bench) and
`upande_tagmeter/tests/` (Frappe layer). Follow that split.

**`tests/test_parse.py`** — extend: `parse_gateway_status` against recorded
responses for both real gateways, including the `Europe/Amsterdam` conversion
and the `0,0` no-GPS-fix case.

**`upande_tagmeter/tests/test_gateway.py`** — new: `sync_gateways` persists
fields correctly; a failed poll leaves prior state intact; a never-polled
gateway does not mark meters down.

**`upande_tagmeter/tests/test_sync.py`** — extend, the important one. A table
test over `link_state`, one case per state, plus:

- silent meter + healthy gateway -> `Silent`
- silent meter + `online=False` gateway -> `Gateway Down`
- silent meter + gateway `online=True` but stale heartbeat -> `Gateway Down`
- `server_error` meter -> `No Data on SMP`, never `Never Seen`
- meter with no gateway link -> `Silent`, never `Gateway Down`
- `online` retains its exact current values across the whole fleet fixture
  (regression guard: this change must not move the existing boolean)

## Out of scope

- Per-meter learned cadence — the fleet is uniform at cycle 1.
- Correlated-silence inference — with one active gateway serving all 100
  meters, gateway status is the direct signal and correlation adds nothing.
- The ChirpStack / SMP network-server split. This design **surfaces** meters
  that are silent on the SMP; it does not resolve which server holds the
  session. That is a larger problem, is named in the README as the single
  biggest operational risk, and needs its own spec.
- Alarm suppression — explicitly rejected above.

## Open questions

1. `…076919` is silent on the SMP since 20 Aug and, unlike its two siblings,
   is not confirmed on ChirpStack. Needs a physical check to establish whether
   `Silent` is the correct verdict for it.
2. `F04CD5FFFE01CF70` has no meters bound and no GPS fix. Is it a spare, a
   decommissioned unit, or a planned second site? If it is decommissioned it
   should be seeded with a `status` that exempts it from health alerting.
3. Is `Meter reading cycle: 1` days or hours? The observed spread — 35 meters
   within 12h, 61 within 12–24h, none between 24–30h — fits 1 day. The default
   assumes days; confirm before relying on `Late`.
