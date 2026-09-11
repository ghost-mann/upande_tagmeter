# Upande TagMeter

Monitoring and valve control for TagMeter ultrasonic water meters via the
**tagmeter.com SMP REST API**. Frappe v16.

The vendor's Smart Meter Platform is the network server and the control plane.
This app does not speak LoRaWAN, does not decode wire frames, and does not talk
to ChirpStack — it is an API integration with a state machine. The SMP is
Upande's own integration account; customers only ever see this Frappe instance.

## Scope

**V1 — registry and telemetry (done).**

- `vendor/` — Frappe-free SMP client: auth, single-flight token, error taxonomy,
  response parsing, status-byte decode.
- `Water Meter` — registry plus denormalised current state, including `online`.
- `Meter Reading` — one immutable record per SMP reading, deduped.
- `sync.py` — the polling sweep.
- `setup/import_meters.py` — seed the fleet, and audit it against the SMP.

**V2 — valve control (done).**

- `Meter Command` — one document per request, with its delivery history.
- `valve_desired` vs `valve_reported` on `Water Meter`. Two fields on purpose:
  when they disagree, either a command is in flight or the meter is not doing
  what was asked, and one field would have to lie about one of those.
- `valve.py` — `set_valve`, confirmation from readings, watchdog,
  reconciliation report.
- A valve **toggle** on the Water Meter form, shown only for Quinto Prepaid
  meters. The switch reflects `valve_reported` — what the meter actually said —
  never the request. While a command is in flight it shows the requested
  position but is disabled and labelled pending, because a switch that snapped
  to the requested position would imply the valve had moved when the downlink
  may be hours away.

**Prepaid recharge is still deliberately absent.** `recharge_meter` and
`savewebhook` need the tariff and credit-ledger model that does not exist yet,
and issuing credit with nowhere to account for it would be worse than not
issuing it. `vendor/client.py` refuses both endpoints structurally.

### How a command is confirmed

`"Operation success!"` means the SMP put a downlink in its queue. It does not
mean the valve moved. A command is `Confirmed` only when a reading **taken
after the command was sent** shows the requested state.

That cannot distinguish "the command worked" from "the valve was already in
that state" — nothing in the available data can — which is why the field is
`confirming_reading` rather than anything implying causation.

A reading showing the *opposite* state is **not** treated as failure: on Class B
the downlink may simply not have arrived yet. Failure comes from expiry, after
26 hours by default (comfortably clearing one 12-hour AMR cycle; the one
measured round trip took about 11 hours).

The watchdog re-sends only commands the SMP **never accepted**. If it already
answered `"Operation success!"`, the downlink is in *their* queue and sending
again would stack a duplicate.

### Who can actually be controlled

Only the **10 Quinto Prepaid** meters have a valve; the 90 AMR meters are
read-only devices, so the form shows no valve buttons for them.

More restrictive still: a command only reaches a meter whose LoRaWAN session is
on the **vendor's** network server, and the fleet is split across two servers
per device with nothing managing the split.

This moves. Measured on 2026-09-09, hours apart:

| Time | Quintos live on the SMP |
|---|---|
| ~13:00Z | 1 (`…0871` only; the rest 651–675 h stale) |
| ~16:00Z | **8** (only `…0868` and `…0873` still stale) |

So meters are actively re-joining the vendor's network server. Check
`online` on the Quinto meters before assuming a command can land, rather than
trusting any number written down here.

## Dashboard

`/tagmeter` is a full-page fleet dashboard, served by `www/tagmeter.py` +
`www/tagmeter.html`. It follows the Upande `ufd-modern` design system used by
the other dashboards — same ink palette, surfaces, card and KPI treatments —
with a water-blue accent in place of the scouting page's trap gold. The
severity scale is deliberately unchanged so a red badge means the same thing
everywhere.

Seven views: **Fleet**, **Register**, **Meters**, **Valves**, **Flow**,
**Graph**, **Signal**.

**Register** is where meters get the names people actually use — a house, a
tenant, a standpipe — plus a zone and a site, edited inline and saved in one
batch. Search and filter by name, serial, zone or site; a bar chart above
counts meters per zone, so an unassigned block is visible at a glance.

> **Naming sets a label; it does not rename the record.** `Water Meter` is
> auto-named from `meter_sn`, and that serial is the SMP's `meterID` and the key
> every reading and command joins on. Renaming the document would break the
> vendor mapping silently and the next sweep would create a second meter. So
> `meter_label` sits beside the serial: the dashboard shows the name, keeps the
> serial visible underneath, and falls back to the serial wherever a meter has
> no name yet. The doctype's `title_field` and `search_fields` follow the label,
> so desk search and link fields find a meter by house number too.

### Name, Both or Serial

A three-way switch in the top bar chooses how a meter is identified, kept in
`localStorage` (a per-reader display preference, so it never becomes a doctype
field — the cost is that it does not follow you between browsers). **Both** is
the default: someone new to the fleet has to learn which name maps to which
serial before the other modes help.

The switch governs the surfaces you *scan* — tables, pickers, fleet tooltips.
Three places override it on purpose:

| Surface | Always shows | Why |
|---|---|---|
| Detail panel | Both | It is where you go to get a serial for the vendor console |
| Register | Both | They are separate columns; that is the view's job |
| **Valve confirmation** | **The serial** | **Safety — see below** |

**`meter_label` is deliberately not unique.** Two blocks can each hold a
"Standpipe 4", and forcing uniqueness would turn naming into a fight with a
validator. The cost is that a name alone can be ambiguous, which is harmless
while browsing and dangerous when actuating: closing the wrong customer's water
is the worst thing this app can do. So the valve dialog always reads
`House 12 (68750000076973)` whatever the display mode, and a test asserts that
in all three.

Any visible serial is click-to-copy, since the usual reason to reveal one is to
paste it into the vendor console or a support ticket.

A batch save applies each row independently — one bad row is reported and the
rest still land, because refusing the whole batch would throw away an
operator's typing.

**Graph** is a full-bleed interactive line chart — the sidebar drops away, the
top bar stays, and the plot takes the viewport. Real X and Y axes with nice
ticks, a measure picker (hourly rate, consumption, cumulative, balance,
temperature, signal), and a date range.

It is drawn in real pixels sized to its container rather than a scaled
`viewBox`, so tick text stays 11px at any viewport width, and it redraws
through a `ResizeObserver`. Interaction: a crosshair snaps to the nearest point
by X, drag across the plot to zoom, double-click or Escape to clear,
arrow/Home/End move the cursor from the keyboard, and every cursor move is
announced through an `aria-live` region. Point markers appear only on series of
24 points or fewer, at 8px — denser than that they merge into the line, and the
crosshair still finds any point.

**Flow** takes a meter and a date range and shows daily / weekly / monthly
volume as stat tiles, the meter's own hourly rate as an area chart, and
consumption per reading as bars. Both charts carry a crosshair and tooltip.
Period totals sum `consumption_m3` rather than differencing two lifetime
counters, so a meter replacement or counter reset cannot produce a negative.
The hourly series is the SMP's rolling 24-hour window, where `delta` is that
hour's volume — already differenced by the meter.

**Valve control is on the dashboard**, in the fleet detail panel and the Flow
view. It calls `valve.set_valve`, the same entry point the desk form uses, so
the in-flight guard, the profile check and the confirmation-by-reading rule
apply identically. The buttons disable while a command is in flight and
explain why.

Chart colours were contrast-checked rather than eyeballed: `#2b6ca3` measures
5.56:1 against the card surface and carries every stroke and bar, `#1b4a73`
(9.24:1) marks the hovered point, and `#6ea9d8` — 2.52:1, too weak for a mark —
appears only inside the gradient fill beneath a darker line.

Two deliberate choices worth keeping:

- **The hero is a 100-cell recency grid, not a consumption chart.**
  `TotalCounter` is `0.000` on every meter, so a volume trend would be a
  straight line at zero dressed up as insight. What the data actually contains
  is *which meters are alive and when each last spoke*, so that is what the
  page leads with. One cell per meter, coloured by how long ago it reported,
  outlined if it has a valve, click for full detail.
- **Empty means empty.** Where there is no data the page says so in words —
  the consumption panel explains that the fleet is installed but not in
  service — rather than rendering a chart of nothing.

All figures come from `Water Meter` / `Meter Reading` / `Meter Command` at
request time; there is no separate cache to go stale.

## Setup

`sites/<site>/site_config.json` — credentials never live in a doctype field:

```json
{
  "tagmeter_api_url":      "https://tagmeter.com/restapi/v2_0/002/api",
  "tagmeter_api_user":     "<service account>",
  "tagmeter_api_password": "<secret>"
}
```

Optional:

| Key | Default | Purpose |
|---|---|---|
| `tagmeter_offline_after_hours` | `30` | Staleness window for `online` |
| `tagmeter_min_interval` | `0.2` | Seconds between SMP calls |
| `tagmeter_meter_timezone` | `UTC` | Zone of meter `TimeStamp` fields |
| `tagmeter_max_attempts` | `3` | Attempts per call, network faults only |
| `tagmeter_retry_backoff` | `1.0` | First backoff in seconds, then doubling |

`002` in the URL is the tenant code, so a second tenant is a config change.

> **The app needs its own SMP service account.** Requesting a token revokes the
> previous one, so if a human logs into tagmeter.com as the same user, the app's
> session dies.

## Commands

```bash
bench --site tagmeter.local console
```
```python
from upande_tagmeter import sync
from upande_tagmeter.setup import import_meters

sync.test_connection()            # credentials + reachability, touches no meter
import_meters.run()               # seed 100 meters (idempotent)
import_meters.verify_against_smp()  # confirm each serial; take DevEUI from the SMP
sync.sync_fleet()                 # poll every meter
sync.sync_fleet(limit=3)          # poll a few
sync.poll_meter("68753500170871") # poll one
sync.refresh_online_flags()       # recompute online, no API calls

from upande_tagmeter import valve
valve.set_valve("68753500170871", "close")   # queue a command
valve.watchdog()                              # expire / re-send
valve.reconciliation_report()                 # desired != reported
```

Unit tests need no bench, no database and no network:

```bash
cd apps/upande_tagmeter && ../../env/bin/python -m pytest tests/ -q
```

## Things the SMP does that will surprise you

Each of these is pinned by a test in `tests/`.

- **Every response is HTTP 200.** Failures arrive as `code: 500` in the body.
  Never trust the HTTP status.
- **`{"meterID": ""}` returns `code: 200 "Operation success!"`** with null data.
  An empty serial reads as a healthy meter forever, so serials are validated
  before a request is sent.
- **Two separate things kill a token**, with different messages:
  - *Revocation* — requesting a token revokes the previous one:
    `"Invalid Authorization Token or Authorization Token non-existent"`.
    Verified. Hence one shared token and single-flight refresh.
  - *Expiry* — tokens also lapse on their own after **roughly 65 minutes**:
    `"Authorization Token expired, Request New Authorization Token"`. Measured
    (minted `14:02:33Z`, expired by `15:08Z`).

  Both are handled identically, and refresh stays **reactive** rather than
  pre-empting the TTL — a timer that minted early would revoke a token other
  workers are still using, which is the exact failure it would be avoiding.
- **`get_customer_info` returns a bare `[]` on an expired token** — HTTP 200,
  no `code` field at all — where `get_latest_amr` returns a proper
  `{"code": 401}`. A fourth response shape. The client classifies an empty list
  as unauthorized so the refresh still fires; without that, every call to that
  endpoint would fail for an hour at a time.
- **Error messages lie.** Omitting `meterID` yields `"Failed checking
  Authorization Token"` with a perfectly valid token. Never branch on message
  text; `code: 500` is disambiguated by validating our own input instead.
- **No bulk read.** One `meterID` per call; an array is rejected. 100 meters is
  100 requests, ~1–3 minutes per sweep.
- **Their edge 403s the default `python-requests` User-Agent** with an HTML
  "Request forbidden by administrative rules" page, before the API is reached.
  `urllib`, a browser string and our own product string all pass. The client
  always sends an explicit `User-Agent`; removing it breaks every call with no
  useful diagnostic. Raised as `BlockedByVendor`, because no credential change
  fixes it.
- **HTTP 500 with an empty body appears to mean "no AMR record for this
  meter".** Their code crashes rather than returning an empty result. Evidence:
  across 100 meters, every serial that answered `200` had a reading and *not
  one* answered `200` with no record — the `parse_amr` → `None` branch has
  never fired against the live platform. The failing set also shrinks as meters
  come online: it was 15 serials, then 13 once `68750000076935` and
  `68750000076902` started reporting. Not rate limiting either — the same
  serials failed identically at 0.1s, 0.6s and 1.5s request spacing.

  These surface as `Outcome.SERVER_ERROR`, are recorded on the meter, and are
  **not** retried: a second identical call cannot make data exist. Only genuine
  network faults are retried, with exponential backoff. Treat `server_error` as
  "the SMP holds nothing for this meter yet", and keep the `no_amr_record`
  branch anyway in case they ever fix it.
- **The SMP uses two different timezones in two different fields**, and neither
  string carries an offset. Getting this wrong is a silent two-hour error on
  every reading, so both were established by measurement:
  - **Meter `TimeStamp`** (`dataAMRRecord`, `dataFreezeRecord`, `dataHourly`) is
    the meter's own clock and reads **UTC**. Meter `68750000076929` reported
    `2026-09-09 13:09:35` while the wall clock was `13:09:11Z` — a live uplink
    24 seconds "ahead" of UTC. Amsterdam would have put it at ~15:09.
  - **`statTime`** from `get_gateway_status` is generated by the SMP and *is*
    Dutch local — `15:07:12` against `13:07:39Z`. It shifts with **European**
    DST, so it goes through `zoneinfo`, never a fixed offset.

  If the meters are ever re-synced with the protocol's set-date-time command
  their clocks could move to local time, hence `tagmeter_meter_timezone` is an
  override rather than a constant.
- **A meter can be known with no AMR record.** Meters emitting only a 1-byte
  keepalive produce none, so the SMP answers 200 with no record.
- **`ValveStatus`/`AlarmMessage` carry the raw status byte as a prefix.**
  `"08[Valve open;Low Balance Alarm;]"` is ST1 `0x08`. We decode the byte rather
  than match English text, which is `content-language`-dependent.
- **A closed valve is `0x01`.** The TagMeter README writes this row as
  `10=Closed`, which is ambiguous about bit order; the vendor decodes its own
  meters as `01`. A sibling implementation maps `0b01` to *Transitioning* and so
  reports every closed valve wrongly. `tests/test_status.py` guards against
  reintroducing that.

## Fleet reality, as measured 2026-09-09

The SMP is **live**, not the stale snapshot it first appeared to be. A full
sweep of 100 meters took 148s and produced 85 readings.

The fleet is **split across two network servers, per device.** Each meter is
live on exactly one and stale on the other, which is the OTAA join race: both
servers see the JoinRequest, but the meter obeys the first JoinAccept it gets.

```
68750000076921   SMP: 20 Aug (stale)     ChirpStack: today 10:21Z  -> on ChirpStack
68750000076885   SMP: 20 Aug (stale)     ChirpStack: today 10:18Z  -> on ChirpStack
68753500170871   SMP: today 13:05Z       ChirpStack: 14 Aug        -> on the SMP
```

Nothing is managing that split. It is the single biggest operational risk here:
control only works on the server that holds the session.

**The two meters recorded as "never reported" are alive.** `68750000076929`
and `68750000076963` — flagged in the sibling app as never having joined and
under investigation — are both reporting to the SMP, and were among the four
freshest readings in the fleet.

Status bytes across 86 readings, decoded from the `ValveStatus`/`AlarmMessage`
prefixes, with zero text disagreements:

| Bytes | Count | Meaning |
|---|---|---|
| `0006` | 67 | valve open, empty pipe + reverse flow |
| `0002` | 17 | valve open, empty pipe |
| `0802` | 2 | valve open, empty pipe + low balance |

`TotalCounter` is `0.0` fleet-wide and empty-pipe is universal: mounted and
powered, no water through them yet.

## Link state

`online` is a plain recency flag: a reading arrived inside 30 hours. It is
preserved exactly as it was, because tiles, filters and callers depend on it.

`link_state` says *why* a meter is quiet, which is the part that decides what
you do about it:

| State | Means | Do |
|---|---|---|
| `Reporting` | fresh inside one cycle | nothing |
| `Late` | missed one expected report | nothing yet — this is jitter |
| `Silent` | missed three, gateway healthy | send a technician |
| `Gateway Down` | missed three, bound gateway unhealthy | fix the backhaul, ignore the meters |
| `Never Seen` | no reading, ever | check commissioning |
| `No Data on SMP` | `get_latest_amr` 500s — the SMP holds no record | see the vendor question below |

A gateway is healthy only when it reports `online` **and** its heartbeat is
newer than `gateway_stale_after_hours` (default 6). Both are required:
`F04CD5FFFE01CF70` was observed reporting a `statTime` two days old, and the
web console's gateway list shows the same column value for a live and a dead
gateway. Only the API's `online` field plus heartbeat age can be trusted.

Alarm flags are **never** suppressed by link state. They are the last known
physical state of the pipe, and a backhaul outage does not make a burst pipe
less real.

Config, all in `site_config.json`:

| Key | Default |
|---|---|
| `tagmeter_expected_cycle_hours` | 24 |
| `gateway_stale_after_hours` | 6 |

## Known unknowns

- `TotalCounter`'s unit is **assumed** m³. `PrepaidBalance` is confirmed m³ (a
  recharge of `0.001` moved a balance of `2` to `2.001`), but `TotalCounter`
  reads `0` fleet-wide, so its scale cannot yet be verified. Re-check once water
  flows.
- The status-byte prefix is parsed as **hex**. Every value observed so far
  (`00`, `01`, `02`, `08`) is identical in hex and decimal. `status_text_mismatch`
  is set when our decode disagrees with the vendor's wording, so a wrong guess
  becomes visible rather than silent.
- `accountNumber` (`savewebhook`) comes from a namespace the vendor has not
  explained, and their `customerInfo` is null for every Upande meter. V1 does
  not depend on either.
- The SMP's rate limit is undocumented.

## Write endpoints — measured

`valve_control` was probed on `68753500170871` on 2026-09-09 (the only Quinto whose session is on the vendor NS; no consumption,
empty pipe, no customer). What came back shaped how V2 is written.

**`code: 200` does NOT mean success on a write.** The only signal is the message:

```
forceValve: "Xyzzy"  ->  code=200  "Invalid valve control command"   REJECTED
forceValve: "Open"   ->  code=200  "Operation success!"              accepted
```

So for writes there is no choice but to inspect `message` — the opposite of the
rule that works for reads. The client therefore **whitelists accepted values**
and treats anything that is not an explicit success as a rejection. Writes are
also **never auto-retried**: a transport failure leaves us unable to tell
whether the request reached their queue, so retries live on the `Meter Command`
document where the attempt count is visible and bounded.

**Accepted `forceValve` values, from 43 candidates tried:**

| Value | Result |
|---|---|
| `Open` / `open` / `OPEN` | accepted — case-insensitive |
| `Close` | accepted |
| `Reset` | **accepted — undocumented, semantics unknown** |
| everything else | `"Invalid valve control command"` |

A third rejection message exists, for the 90 meters that have no valve:

```
{"code": 200, "message": "Meter ID 68750000076973 has no valve, Valve Control Aborted"}
```

Measured 2026-09-09 against three AMR-profile meters; a Quinto answered
`"Operation success!"` in the same run. So the 90/10 split is the vendor's own
view, not an inference from the ChirpStack device profiles — **reclassifying a
meter in Frappe would not make it actuable.** The AMR meters also report
`PrepaidBalance: 0` where the Quintos report `2.000`.

Rejected included `Prepaid`, `prepaid`, `PrePaid`, `Prepay`, `Auto`,
`MeterControlled`, `Meter Controlled`, `MeterControl`, `Control`, `Credit`,
`Normal`, `Default`, `44`, `44H`, `0x44`, `55`, `99`, `Force Open`, `ForceOpen`,
`On`, `Off`, `1`, `0`, and the empty string.

**Consequence: the protocol's `44H` meter-controlled/prepaid mode is not
reachable through `valve_control`.** That mode is what makes a Quinto meter
manage its own valve against its credit balance — the entire reason those ten
meters exist. Either it is set by some other endpoint, or implicitly by
`recharge_meter`, or it is not exposed at all. This is the single most
important thing to settle with the vendor before V2 is designed.

`Reset` deserves care: it is accepted, undocumented, and nothing in the vendor's
Postman collection mentions it. Do not send it to a live meter until they say
what it does.

**Confirmation latency is real.** `forceValve=Close` was enqueued at
`13:51:38Z` and the meter had not reported at all for the next 70 minutes — its
previous uplink was `13:05:35Z`. The one previously measured round trip, the
19 Aug `recharge_meter`, took about 11 hours. Any UI must show "in flight"
honestly rather than implying a valve moved.

## Open questions for the vendor

1. What values does `forceValve` accept? If the `44H` meter-controlled mode is
   unreachable, autonomous prepaid operation is impossible via the API — which
   is the entire purpose of the 10 Quinto meters. Highest-risk unknown.
2. Can the SMP **push** uplinks to a webhook? That would remove the polling
   sweep and give near-real-time telemetry.
3. Is there a rate limit?
4. Does a human logging into the web console revoke the app's token?
5. **`get_latest_amr` returns HTTP 500 with an empty body for meters that have
   no AMR record.** Please return `200` with an empty result instead. Right now
   a meter with no data is indistinguishable from a server fault, and 13% of
   the fleet is unreadable through the API. Serials that were failing at
   2026-09-09 13:00Z:

   ```
   68750000076884  68750000076886  68750000076889  68750000076899
   68750000076907  68750000076912  68750000076915  68750000076927
   68750000076934  68750000076943  68750000076954  68750000076967
   68750000076973  68753500170869
   ```

   The set shrinks as meters report, which is what points to the cause.
