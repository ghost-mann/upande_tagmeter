# Link State and Gateway Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace a single `online` boolean with a diagnostic `link_state` that distinguishes a faulty meter from a downed gateway, backed by real gateway health from the SMP, and surface it in the workspace, the dashboard and the Valve Board.

**Architecture:** A new `TagMeter Gateway` doctype is populated hourly by `gateway.sync_gateways()` calling the already-implemented `vendor/client.get_gateway_status`. `Water Meter` gains a `gateway` Link and a computed `link_state` Select. The existing `online` checkbox and its rule are left completely untouched so current tiles, filters and callers keep working. `link_state` is computed in `sync.refresh_link_states()` from data already in the database — no API calls.

**Tech Stack:** Frappe v16, Python 3.10+, MariaDB, pytest (vendor layer), `frappe.tests.IntegrationTestCase` (Frappe layer), vanilla JS for the `/tagmeter` dashboard.

**Spec:** `docs/superpowers/specs/2026-09-11-link-state-and-gateway-monitoring-design.md`

## Global Constraints

- **`online` must not change.** Its rule (`sync.py:235`), its values and its meaning stay exactly as they are. Task 5 includes a regression test that asserts this across a fleet fixture. Any change to `online` is a plan failure.
- **Server-side authority is unchanged.** The dashboard gains no ability the desk lacks. `valve.set_valve` stays the single valve entry point and keeps enforcing Quinto-Prepaid-only actuation.
- **Alarm flags surface regardless of `link_state`.** No alarm suppression anywhere. A meter in `Gateway Down` still shows empty-pipe, reverse-flow, leak and burst flags.
- **Datetimes** are stored naive in system timezone via `sync._to_system_naive`. `statTime` arrives in `Europe/Amsterdam` — never UTC, never meter time. `parse_gateway_status` already handles the conversion; do not re-implement it.
- **Indentation is tabs**, double-quoted strings, line length 110 (`pyproject.toml` `[tool.ruff]`).
- **Config defaults:** `gateway_stale_after_hours` = 6, `tagmeter_expected_cycle_hours` = 24. Both read from `frappe.conf` with the literal default in code.
- **Thresholds:** `Late` at 1.25× cycle (30h default), `Silent` at 3× cycle (72h default).
- **Test commands:**
  - Vendor layer: `/home/austin/frappe-v16-bench/env/bin/python -m pytest tests/<file> -v` from `apps/upande_tagmeter`
  - Frappe layer: `bench --site kaitet.local run-tests --module upande_tagmeter.tests.<module>` from `/home/austin/frappe-v16-bench`

## File Structure

| File | Responsibility |
|---|---|
| `upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/` | New doctype: gateway identity + last known health |
| `upande_tagmeter/gateway.py` | Gateway sync pass and the single definition of "unhealthy" |
| `upande_tagmeter/sync.py` | Gains `refresh_link_states()`; `refresh_online_flags` untouched |
| `upande_tagmeter/setup/import_gateways.py` | Seeds gateways and binds meters, mirroring `import_meters.py` |
| `upande_tagmeter/data/kiwasco-gateways.tsv` | 2 gateway rows |
| `upande_tagmeter/data/kiwasco-devices.tsv` | Gains a `gateway` column |
| `upande_tagmeter/workspace_block.py` | Network tile group |
| `upande_tagmeter/www/tagmeter.py` | Payload gains `link_state`, `gateways`, gateway KPIs |
| `upande_tagmeter/www/tagmeter.html` | Network view, unhealthy banner, `link_state` column, Valve Board actions |
| `upande_tagmeter/hooks.py` | `sync_gateways` + `refresh_link_states` scheduler entries |
| `tests/test_parse.py` | Extend: `parse_gateway_status` |
| `upande_tagmeter/tests/test_gateway.py` | New: gateway sync + health rule |
| `upande_tagmeter/tests/test_sync.py` | Extend: `link_state` table test + `online` regression guard |

---

### Task 1: `TagMeter Gateway` doctype

**Files:**
- Create: `upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/__init__.py`
- Create: `upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/tagmeter_gateway.json`
- Create: `upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/tagmeter_gateway.py`
- Test: `upande_tagmeter/tests/test_gateway.py`

**Interfaces:**
- Consumes: nothing
- Produces: doctype `TagMeter Gateway`, named by `gateway_id`, with fields `gateway_id`, `label`, `site`, `online` (Check), `last_heartbeat` (Datetime), `latitude`/`longitude`/`altitude` (Float), `gps_time_sync` (Check), `mqtt_protocol` (Check), `last_polled_at` (Datetime), `last_poll_outcome` (Data)

- [ ] **Step 1: Write the failing test**

Create `upande_tagmeter/tests/test_gateway.py`:

```python
"""Gateway sync against a stubbed SMP client. No network."""

from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime


class TestGatewayDoctype(IntegrationTestCase):
	def test_a_gateway_is_named_by_its_id(self):
		gid = "TESTGW0000000001"
		if frappe.db.exists("TagMeter Gateway", gid):
			frappe.delete_doc("TagMeter Gateway", gid, force=1)
		doc = frappe.get_doc({
			"doctype": "TagMeter Gateway", "gateway_id": gid, "label": "Test GW",
		}).insert()
		self.assertEqual(doc.name, gid)
		self.assertEqual(int(doc.online or 0), 0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_gateway
```

Expected: FAIL — `DoesNotExistError: DocType TagMeter Gateway not found`

- [ ] **Step 3: Create the doctype**

`upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/__init__.py` — empty file.

`upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/tagmeter_gateway.json`:

```json
{
 "actions": [],
 "allow_import": 1,
 "autoname": "field:gateway_id",
 "creation": "2026-09-11 12:00:00.000000",
 "doctype": "DocType",
 "editable_grid": 1,
 "engine": "InnoDB",
 "field_order": [
  "identity_sb", "gateway_id", "label", "cb_identity", "site", "gateway_status",
  "health_sb", "online", "last_heartbeat", "cb_health", "last_polled_at", "last_poll_outcome",
  "position_sb", "latitude", "longitude", "cb_position", "altitude", "gps_time_sync",
  "mqtt_protocol"
 ],
 "fields": [
  {"fieldname": "identity_sb", "fieldtype": "Section Break", "label": "Identity"},
  {"fieldname": "gateway_id", "fieldtype": "Data", "label": "Gateway ID", "reqd": 1, "unique": 1,
   "in_list_view": 1, "description": "The SMP gatewayID, e.g. 0C4EC0FFFE00E97F"},
  {"fieldname": "label", "fieldtype": "Data", "label": "Name", "in_list_view": 1},
  {"fieldname": "cb_identity", "fieldtype": "Column Break"},
  {"fieldname": "site", "fieldtype": "Data", "label": "Site"},
  {"fieldname": "gateway_status", "fieldtype": "Select", "label": "Status",
   "options": "Active\nSpare\nDecommissioned", "default": "Active",
   "description": "Decommissioned gateways are excluded from health alerting."},
  {"fieldname": "health_sb", "fieldtype": "Section Break", "label": "Health"},
  {"fieldname": "online", "fieldtype": "Check", "label": "Online (vendor flag)", "in_list_view": 1,
   "description": "The SMP's own flag. Not sufficient on its own -- see last_heartbeat."},
  {"fieldname": "last_heartbeat", "fieldtype": "Datetime", "label": "Last Heartbeat",
   "in_list_view": 1, "description": "statTime, converted from Europe/Amsterdam"},
  {"fieldname": "cb_health", "fieldtype": "Column Break"},
  {"fieldname": "last_polled_at", "fieldtype": "Datetime", "label": "Last Polled"},
  {"fieldname": "last_poll_outcome", "fieldtype": "Data", "label": "Last Poll Outcome"},
  {"fieldname": "position_sb", "fieldtype": "Section Break", "label": "Position"},
  {"fieldname": "latitude", "fieldtype": "Float", "label": "Latitude", "precision": "7"},
  {"fieldname": "longitude", "fieldtype": "Float", "label": "Longitude", "precision": "7"},
  {"fieldname": "cb_position", "fieldtype": "Column Break"},
  {"fieldname": "altitude", "fieldtype": "Float", "label": "Altitude (m)"},
  {"fieldname": "gps_time_sync", "fieldtype": "Check", "label": "GPS Time Sync"},
  {"fieldname": "mqtt_protocol", "fieldtype": "Check", "label": "MQTT Protocol"}
 ],
 "index_web_pages_for_search": 1,
 "links": [],
 "modified": "2026-09-11 12:00:00.000000",
 "modified_by": "Administrator",
 "module": "upande_tagmeter",
 "name": "TagMeter Gateway",
 "naming_rule": "By fieldname",
 "owner": "Administrator",
 "permissions": [
  {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1},
  {"role": "TagMeter Operator", "read": 1, "report": 1, "export": 1}
 ],
 "search_fields": "label,site",
 "show_title_field_in_link": 1,
 "sort_field": "modified",
 "sort_order": "DESC",
 "title_field": "label",
 "track_changes": 1
}
```

`upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway/tagmeter_gateway.py`:

```python
# Copyright (c) 2026, ghost-mann and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class TagMeterGateway(Document):
	pass
```

- [ ] **Step 4: Migrate and run the test**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local migrate
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_gateway
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/upande_tagmeter/doctype/tagmeter_gateway upande_tagmeter/tests/test_gateway.py
git commit -m "feat: TagMeter Gateway doctype"
```

---

### Task 2: `parse_gateway_status` test coverage

**Files:**
- Modify: `tests/test_parse.py` (append)
- Test: same file

**Interfaces:**
- Consumes: `upande_tagmeter.vendor.parse.parse_gateway_status(body, tz_name=SERVER_TZ) -> dict`
- Produces: nothing new — proves the existing parser against both real gateways

This task exists because `parse_gateway_status` has never been exercised. The two payloads below were captured live on 2026-09-11.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parse.py`:

```python
from upande_tagmeter.vendor.parse import parse_gateway_status

# Captured live 2026-09-11. F04CD5 is genuinely down; 0C4EC0 serves all 100 meters.
GW_DOWN = {
	"code": 200, "gatewayID": "F04CD5FFFE01CF70", "online": False, "mqtt_protocol": True,
	"latitude": 0, "longitude": 0, "altitude": 0, "gpsTimeSync": False,
	"statTime": "2026-09-09 12:21:13", "timezone": "Europe/Amsterdam",
}
GW_UP = {
	"code": 200, "gatewayID": "0C4EC0FFFE00E97F", "online": True, "mqtt_protocol": True,
	"latitude": -1.2970528, "longitude": 36.7771403, "altitude": 1814, "gpsTimeSync": True,
	"statTime": "2026-09-11 10:23:06", "timezone": "Europe/Amsterdam",
}


def test_gateway_status_reports_offline():
	out = parse_gateway_status(GW_DOWN)
	assert out["gateway_id"] == "F04CD5FFFE01CF70"
	assert out["online"] is False
	assert out["gps_time_sync"] is False


def test_gateway_stat_time_is_amsterdam_not_utc():
	"""statTime is SMP-generated in Dutch local time -- 2h ahead of UTC in September."""
	out = parse_gateway_status(GW_UP)
	assert out["stat_time"].hour == 8
	assert out["stat_time"].tzinfo is not None


def test_gateway_without_a_gps_fix_reports_zeroes_not_none():
	out = parse_gateway_status(GW_DOWN)
	assert out["latitude"] == 0.0
	assert out["longitude"] == 0.0
```

- [ ] **Step 2: Run to verify**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
/home/austin/frappe-v16-bench/env/bin/python -m pytest tests/test_parse.py -v -k gateway
```

Expected: PASS immediately — the parser already exists. If `test_gateway_stat_time_is_amsterdam_not_utc` fails, the timezone handling is broken and must be fixed before continuing; that is the whole reason this task comes before the sync pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_parse.py
git commit -m "test: cover parse_gateway_status against both real gateways"
```

---

### Task 3: `gateway.py` — sync pass and the health rule

**Files:**
- Create: `upande_tagmeter/gateway.py`
- Modify: `upande_tagmeter/hooks.py:24-40` (scheduler_events)
- Test: `upande_tagmeter/tests/test_gateway.py` (append)

**Interfaces:**
- Consumes: `sync.get_client()`, `sync._to_system_naive()`, `vendor.errors.Outcome`
- Produces:
  - `gateway.GATEWAY_STALE_AFTER_HOURS = 6`
  - `gateway.stale_cutoff() -> datetime`
  - `gateway.unhealthy_gateways() -> list[str]` — gateway names we are confident are down
  - `gateway.sync_gateways(client=None) -> dict` with keys `polled`, `healthy`, `unhealthy`, `errors`

- [ ] **Step 1: Write the failing tests**

Append to `upande_tagmeter/tests/test_gateway.py`:

```python
from upande_tagmeter import gateway
from upande_tagmeter.vendor.errors import Outcome


class FakeGatewayClient:
	"""Returns queued (Outcome, parsed) pairs and records what was asked for."""

	def __init__(self, *results):
		self.results = list(results)
		self.asked = []

	def get_gateway_status(self, gateway_id):
		self.asked.append(gateway_id)
		return self.results.pop(0) if self.results else (Outcome.OK, None)


def _status(gid, online=True, stat_time=None, **kw):
	data = {
		"gateway_id": gid, "online": online, "mqtt_protocol": True,
		"latitude": -1.297, "longitude": 36.777, "altitude": 1814.0,
		"gps_time_sync": True, "stat_time": stat_time, "reported_timezone": "Europe/Amsterdam",
	}
	data.update(kw)
	return data


class TestGatewaySync(IntegrationTestCase):
	def _gw(self, gid, **values):
		if frappe.db.exists("TagMeter Gateway", gid):
			frappe.delete_doc("TagMeter Gateway", gid, force=1)
		doc = frappe.get_doc({
			"doctype": "TagMeter Gateway", "gateway_id": gid, "label": gid, "site": "Kiwasco",
		}).insert()
		if values:
			frappe.db.set_value("TagMeter Gateway", gid, values, update_modified=False)
		return doc

	def test_sync_persists_health_and_position(self):
		gid = "TESTGW0000000010"
		self._gw(gid)
		fresh = now_datetime().replace(microsecond=0)
		client = FakeGatewayClient((Outcome.OK, _status(gid, online=True, stat_time=fresh)))
		result = gateway.sync_gateways(client=client)

		self.assertEqual(client.asked, [gid])
		row = frappe.db.get_value(
			"TagMeter Gateway", gid,
			["online", "last_heartbeat", "altitude", "last_poll_outcome"], as_dict=True,
		)
		self.assertEqual(int(row.online), 1)
		self.assertEqual(row.altitude, 1814.0)
		self.assertEqual(row.last_poll_outcome, "ok")
		self.assertIn(gid, result["healthy"])

	def test_a_failed_poll_leaves_prior_state_intact(self):
		"""A blip on our side is not a gateway outage."""
		gid = "TESTGW0000000011"
		fresh = now_datetime() - timedelta(minutes=5)
		self._gw(gid, online=1, last_heartbeat=fresh, last_polled_at=fresh)
		client = FakeGatewayClient((Outcome.SERVER_ERROR, None))
		gateway.sync_gateways(client=client)

		row = frappe.db.get_value(
			"TagMeter Gateway", gid, ["online", "last_poll_outcome"], as_dict=True
		)
		self.assertEqual(int(row.online), 1, "a failed poll must not mark a gateway down")
		self.assertEqual(row.last_poll_outcome, "server_error")

	def test_a_gateway_that_says_online_with_a_stale_heartbeat_is_unhealthy(self):
		"""F04CD5FFFE01CF70 is exactly this case."""
		gid = "TESTGW0000000012"
		self._gw(gid, online=1, last_heartbeat=now_datetime() - timedelta(hours=48),
		         last_polled_at=now_datetime())
		self.assertIn(gid, gateway.unhealthy_gateways())

	def test_a_never_polled_gateway_is_not_called_down(self):
		"""A freshly seeded gateway must not condemn the meters bound to it."""
		gid = "TESTGW0000000013"
		self._gw(gid)
		self.assertNotIn(gid, gateway.unhealthy_gateways())

	def test_a_healthy_gateway_is_absent_from_the_unhealthy_list(self):
		gid = "TESTGW0000000014"
		self._gw(gid, online=1, last_heartbeat=now_datetime() - timedelta(minutes=10),
		         last_polled_at=now_datetime())
		self.assertNotIn(gid, gateway.unhealthy_gateways())
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_gateway
```

Expected: FAIL — `ModuleNotFoundError: No module named 'upande_tagmeter.gateway'`

- [ ] **Step 3: Write the implementation**

Create `upande_tagmeter/gateway.py`:

```python
"""Gateway health, polled from the SMP.

The vendor's ``online`` flag is not sufficient on its own. Gateway
``F04CD5FFFE01CF70`` reported ``online: false`` with a ``statTime`` two days
old, and the SMP web console's gateway list showed the same value in a column
for both a live and a dead gateway. Heartbeat age is the signal that does not
lie, so :func:`unhealthy_gateways` requires both.
"""

from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from upande_tagmeter.sync import _to_system_naive, get_client
from upande_tagmeter.vendor.errors import Outcome

# A gateway heartbeats far more often than a meter reports, so staleness here
# is measured in hours, not the 30 the meters need.
GATEWAY_STALE_AFTER_HOURS = 6


def stale_cutoff():
	hours = frappe.conf.get("gateway_stale_after_hours") or GATEWAY_STALE_AFTER_HOURS
	return now_datetime() - timedelta(hours=float(hours))


def unhealthy_gateways() -> list[str]:
	"""Gateways we are confident are down.

	A gateway that has never been polled is absent from this list: it is
	*unknown*, not down, and must not condemn the meters bound to it. Same for
	a decommissioned unit, which is down on purpose.
	"""
	cutoff = stale_cutoff()
	out = []
	for row in frappe.get_all(
		"TagMeter Gateway",
		filters={"gateway_status": ("!=", "Decommissioned")},
		fields=["name", "online", "last_heartbeat", "last_polled_at"],
	):
		if not row.last_polled_at:
			continue
		if not row.online or not row.last_heartbeat or row.last_heartbeat < cutoff:
			out.append(row.name)
	return out


@frappe.whitelist()
def sync_gateways(client=None) -> dict:
	"""Poll every gateway. Two API calls, so this can run hourly.

	A failed poll records the outcome and leaves the last known health alone.
	Inferring "down" from our own network blip would mark every bound meter
	``Gateway Down`` for a fault that is not the gateway's.
	"""
	client = client or get_client()
	names = frappe.get_all("TagMeter Gateway", pluck="name", order_by="name asc")

	healthy, unhealthy, errors = [], [], []
	for name in names:
		polled_at = now_datetime()
		try:
			outcome, data = client.get_gateway_status(name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"TagMeter gateway poll failed for {name}")
			errors.append(name)
			continue

		updates = {"last_polled_at": polled_at, "last_poll_outcome": outcome.value}
		if outcome is Outcome.OK and data:
			updates.update({
				"online": 1 if data.get("online") else 0,
				"last_heartbeat": _to_system_naive(data.get("stat_time")),
				"latitude": data.get("latitude"),
				"longitude": data.get("longitude"),
				"altitude": data.get("altitude"),
				"gps_time_sync": 1 if data.get("gps_time_sync") else 0,
				"mqtt_protocol": 1 if data.get("mqtt_protocol") else 0,
			})
		else:
			errors.append(name)
		frappe.db.set_value("TagMeter Gateway", name, updates, update_modified=False)

	down = set(unhealthy_gateways())
	for name in names:
		(unhealthy if name in down else healthy).append(name)

	return {"polled": len(names), "healthy": healthy, "unhealthy": unhealthy, "errors": errors}
```

- [ ] **Step 4: Register the scheduler entry**

In `upande_tagmeter/hooks.py`, inside `scheduler_events["hourly"]`, add `sync_gateways` **as the first entry** so it runs before link states are computed:

```python
	"hourly": [
		# Gateway health is the input to every "Gateway Down" verdict, so it
		# must be refreshed before link states are recomputed below.
		"upande_tagmeter.gateway.sync_gateways",
		# Expire overdue valve commands and re-send any the SMP never accepted.
		# Hourly rather than every few minutes: a command legitimately waits
		# hours on Class B, so checking more often would only add noise.
		"upande_tagmeter.valve.watchdog",
		# A meter that goes quiet produces no reading, so nothing would ever
		# clear its online flag. This pass is pure SQL and costs no API calls.
		"upande_tagmeter.sync.refresh_online_flags",
	],
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_gateway
```

Expected: PASS (all 6 tests)

- [ ] **Step 6: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/gateway.py upande_tagmeter/hooks.py upande_tagmeter/tests/test_gateway.py
git commit -m "feat: poll gateway health from the SMP hourly"
```

---

### Task 4: `Water Meter.gateway` field and seeding

**Files:**
- Modify: `upande_tagmeter/upande_tagmeter/doctype/water_meter/water_meter.json`
- Create: `upande_tagmeter/data/kiwasco-gateways.tsv`
- Create: `upande_tagmeter/setup/import_gateways.py`
- Modify: `upande_tagmeter/data/kiwasco-devices.tsv` (add `gateway` column)
- Modify: `upande_tagmeter/setup/import_meters.py:60-96`
- Test: `upande_tagmeter/tests/test_gateway.py` (append)

**Interfaces:**
- Consumes: doctype `TagMeter Gateway` from Task 1
- Produces: `Water Meter.gateway` (Link to `TagMeter Gateway`, nullable); `import_gateways.run(path=None, dry_run=False) -> dict` with keys `source`, `created`, `updated`, `problems`, `total`

- [ ] **Step 1: Write the failing test**

Append to `upande_tagmeter/tests/test_gateway.py`:

```python
from upande_tagmeter.setup import import_gateways


class TestGatewaySeeding(IntegrationTestCase):
	def test_the_shipped_tsv_seeds_both_gateways(self):
		result = import_gateways.run(dry_run=True)
		self.assertEqual(result["problems"], [])
		self.assertEqual(result["created"] + result["updated"], 2)

	def test_run_is_idempotent(self):
		first = import_gateways.run()
		second = import_gateways.run()
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["updated"], first["created"] + first["updated"])

	def test_a_meter_can_be_bound_to_a_gateway(self):
		import_gateways.run()
		sn = "68753500170902"
		if not frappe.db.exists("Water Meter", sn):
			frappe.get_doc({
				"doctype": "Water Meter", "meter_sn": sn, "meter_profile": "Quinto Prepaid",
				"gateway": "0C4EC0FFFE00E97F",
			}).insert()
		self.assertEqual(
			frappe.db.get_value("Water Meter", sn, "gateway"), "0C4EC0FFFE00E97F"
		)
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_gateway
```

Expected: FAIL — `No module named 'upande_tagmeter.setup.import_gateways'`

- [ ] **Step 3: Add the `gateway` field to Water Meter**

In `water_meter.json`, add to `field_order` immediately after `"zone"`:

```
"gateway",
```

and add to `fields`:

```json
  {"fieldname": "gateway", "fieldtype": "Link", "label": "Gateway",
   "options": "TagMeter Gateway",
   "description": "The gateway this meter is bound to on the SMP. Nullable -- an unbound meter can reach Silent but never Gateway Down."}
```

- [ ] **Step 4: Create the gateway TSV**

`upande_tagmeter/data/kiwasco-gateways.tsv` (tab-separated, exactly as below):

```
gateway_id	label	site	gateway_status
0C4EC0FFFE00E97F	Gateway Upande KE	Kiwasco	Active
F04CD5FFFE01CF70	Gateway 2	Kiwasco	Active
```

- [ ] **Step 5: Write the importer**

Create `upande_tagmeter/setup/import_gateways.py`:

```python
"""Seed the gateway registry.

The SMP has no list-gateways endpoint -- ``get_gateway_status`` answers for one
ID at a time -- so the inventory has to be declared here, exactly as the meter
registry is. There are two, and they change rarely.

Run with::

	bench --site kaitet.local console
	>>> from upande_tagmeter.setup import import_gateways
	>>> import_gateways.run()
"""

import csv
import pathlib
import re

import frappe

TSV = pathlib.Path(__file__).resolve().parents[1] / "data" / "kiwasco-gateways.tsv"

# 16 hex characters, the EUI-64 the SMP uses for gateway IDs.
GATEWAY_ID_RE = re.compile(r"^[0-9A-F]{16}$")


def run(path: str | None = None, dry_run: bool = False) -> dict:
	"""Create or update one TagMeter Gateway per row. Idempotent."""
	source = pathlib.Path(path) if path else TSV
	created = updated = 0
	problems: list[str] = []

	with source.open() as handle:
		for row in csv.DictReader(handle, delimiter="\t"):
			gateway_id = (row.get("gateway_id") or "").strip().upper()
			if not GATEWAY_ID_RE.match(gateway_id):
				problems.append(f"{gateway_id!r}: not 16 hex characters")
				continue

			values = {
				"label": (row.get("label") or "").strip() or gateway_id,
				"site": (row.get("site") or "").strip() or None,
				"gateway_status": (row.get("gateway_status") or "").strip() or "Active",
			}
			exists = frappe.db.exists("TagMeter Gateway", gateway_id)
			if dry_run:
				updated += 1 if exists else 0
				created += 0 if exists else 1
				continue

			if exists:
				doc = frappe.get_doc("TagMeter Gateway", gateway_id)
				doc.update(values)
				doc.save(ignore_permissions=True)
				updated += 1
			else:
				frappe.get_doc({
					"doctype": "TagMeter Gateway", "gateway_id": gateway_id, **values,
				}).insert(ignore_permissions=True)
				created += 1

	return {
		"source": str(source),
		"created": created,
		"updated": updated,
		"problems": problems,
		"total": frappe.db.count("TagMeter Gateway") if not dry_run else None,
	}
```

- [ ] **Step 6: Bind meters in the device TSV**

Add a `gateway` column to `upande_tagmeter/data/kiwasco-devices.tsv`. The header becomes:

```
dev_eui	device_name	device_profile	gateway
```

and every one of the 100 data rows gains a trailing tab and `0C4EC0FFFE00E97F` — that is the SMP's own `Binding Information（100/100）` for that gateway. Apply it mechanically:

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
awk -F'\t' -v OFS='\t' 'NR==1 {print $0, "gateway"; next} {print $0, "0C4EC0FFFE00E97F"}' \
  upande_tagmeter/data/kiwasco-devices.tsv > /tmp/devices.tsv
mv /tmp/devices.tsv upande_tagmeter/data/kiwasco-devices.tsv
head -2 upande_tagmeter/data/kiwasco-devices.tsv
```

Then in `upande_tagmeter/setup/import_meters.py`, inside `run()`, extend the `values` dict (currently at line ~72) to carry the binding:

```python
			values = {
				"meter_profile": profile,
				"device_index": index,
				"dev_eui": dev_eui or None,
				"site": site,
				# The SMP binds each meter to exactly one gateway. Taken from the
				# TSV rather than inferred: RSSI tells you link quality, not binding.
				"gateway": (row.get("gateway") or "").strip().upper() or None,
			}
```

- [ ] **Step 7: Migrate and run the tests**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local migrate
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_gateway
```

Expected: PASS

- [ ] **Step 8: Seed the real data and verify**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local execute upande_tagmeter.setup.import_gateways.run
bench --site kaitet.local execute upande_tagmeter.setup.import_meters.run
bench --site kaitet.local execute upande_tagmeter.gateway.sync_gateways
```

Expected: 2 gateways created, 100 meters updated, and `sync_gateways` reporting `F04CD5FFFE01CF70` in `unhealthy`.

- [ ] **Step 9: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/upande_tagmeter/doctype/water_meter/water_meter.json \
        upande_tagmeter/data upande_tagmeter/setup/import_gateways.py \
        upande_tagmeter/setup/import_meters.py upande_tagmeter/tests/test_gateway.py
git commit -m "feat: bind meters to gateways and seed the gateway registry"
```

---

### Task 5: `link_state` field and computation

**Files:**
- Modify: `upande_tagmeter/upande_tagmeter/doctype/water_meter/water_meter.json`
- Modify: `upande_tagmeter/sync.py` (append after `refresh_online_flags`)
- Modify: `upande_tagmeter/hooks.py` (scheduler_events hourly)
- Test: `upande_tagmeter/tests/test_sync.py` (append)

**Interfaces:**
- Consumes: `gateway.unhealthy_gateways()` from Task 3, `Water Meter.gateway` from Task 4
- Produces: `sync.EXPECTED_CYCLE_HOURS = 24`, `sync.LATE_MULTIPLIER = 1.25`, `sync.SILENT_MULTIPLIER = 3`, `sync.refresh_link_states() -> dict` with keys `cycle_hours`, `unhealthy_gateways`, `counts` (a `dict[str, int]` keyed by state)

- [ ] **Step 1: Write the failing tests**

Append to `upande_tagmeter/tests/test_sync.py`:

```python
	# ── link state ───────────────────────────────────────────────────────────

	def _gateway(self, gid, healthy=True):
		if not frappe.db.exists("TagMeter Gateway", gid):
			frappe.get_doc({
				"doctype": "TagMeter Gateway", "gateway_id": gid, "label": gid,
			}).insert()
		frappe.db.set_value("TagMeter Gateway", gid, {
			"online": 1 if healthy else 0,
			"last_heartbeat": now_datetime() - timedelta(minutes=5),
			"last_polled_at": now_datetime(),
		}, update_modified=False)
		return gid

	def _staged(self, sn, hours_ago, gateway=None, outcome="ok"):
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, {
			"last_seen": None if hours_ago is None else now_datetime() - timedelta(hours=hours_ago),
			"last_sync_outcome": outcome,
			"gateway": gateway,
		}, update_modified=False)
		return sn

	def test_link_state_table(self):
		up = self._gateway("TESTGWUP00000001", healthy=True)
		down = self._gateway("TESTGWDOWN000001", healthy=False)
		cases = [
			("68753500170910", 1, up, "ok", "Reporting"),
			("68753500170911", 40, up, "ok", "Late"),
			("68753500170912", 100, up, "ok", "Silent"),
			("68753500170913", 100, down, "ok", "Gateway Down"),
			("68753500170914", None, up, "ok", "Never Seen"),
			("68753500170915", None, up, "server_error", "No Data on SMP"),
			("68753500170916", 100, None, "ok", "Silent"),
		]
		for sn, hours, gw, outcome, _expected in cases:
			self._staged(sn, hours, gateway=gw, outcome=outcome)

		sync.refresh_link_states()

		for sn, _h, _g, _o, expected in cases:
			self.assertEqual(
				frappe.db.get_value("Water Meter", sn, "link_state"), expected,
				f"{sn} should be {expected}",
			)

	def test_a_late_meter_is_late_even_behind_a_dead_gateway(self):
		"""One missed report is jitter, not evidence. Promoting it would alarm on noise."""
		down = self._gateway("TESTGWDOWN000002", healthy=False)
		sn = self._staged("68753500170917", 40, gateway=down)
		sync.refresh_link_states()
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "link_state"), "Late")

	def test_no_data_on_smp_wins_over_never_seen(self):
		"""server_error means the SMP holds no AMR record -- a different problem."""
		sn = self._staged("68753500170918", None, outcome="server_error")
		sync.refresh_link_states()
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "link_state"), "No Data on SMP")

	def test_refresh_link_states_does_not_move_the_online_flag(self):
		"""Regression guard: online keeps its exact current rule and values."""
		up = self._gateway("TESTGWUP00000002", healthy=True)
		down = self._gateway("TESTGWDOWN000003", healthy=False)
		fixture = [
			("68753500170920", 1, up), ("68753500170921", 40, down),
			("68753500170922", 100, down), ("68753500170923", None, up),
		]
		for sn, hours, gw in fixture:
			self._staged(sn, hours, gateway=gw)
		sync.refresh_online_flags()
		before = {sn: frappe.db.get_value("Water Meter", sn, "online") for sn, _h, _g in fixture}

		sync.refresh_link_states()

		after = {sn: frappe.db.get_value("Water Meter", sn, "online") for sn, _h, _g in fixture}
		self.assertEqual(before, after)
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_sync
```

Expected: FAIL — `AttributeError: module 'upande_tagmeter.sync' has no attribute 'refresh_link_states'`

- [ ] **Step 3: Add the `link_state` field**

In `water_meter.json`, add to `field_order` immediately after `"online"`:

```
"link_state",
```

and to `fields`:

```json
  {"fieldname": "link_state", "fieldtype": "Select", "label": "Link State",
   "options": "\nReporting\nLate\nSilent\nGateway Down\nNever Seen\nNo Data on SMP",
   "read_only": 1, "in_list_view": 1, "in_standard_filter": 1,
   "description": "Computed. Why a meter is quiet, not just that it is -- online stays the plain recency flag."}
```

Also add `link_state` to the doctype's `search_fields` value so it reads:

```json
 "search_fields": "meter_label,meter_profile,site,zone,status,link_state",
```

- [ ] **Step 4: Write the implementation**

Append to `upande_tagmeter/sync.py`, after `refresh_online_flags`:

```python
# The fleet reports on a uniform cycle (the SMP console shows "Meter reading
# cycle: 1" on every meter), so one configured value is enough. Late carries a
# 25% grace for jitter, which lands on the same 30h the online flag uses -- so
# Late begins exactly where online flips, and nothing gets noisier.
EXPECTED_CYCLE_HOURS = 24
LATE_MULTIPLIER = 1.25
SILENT_MULTIPLIER = 3


def _cycle_hours() -> float:
	return float(frappe.conf.get("tagmeter_expected_cycle_hours") or EXPECTED_CYCLE_HOURS)


@frappe.whitelist()
def refresh_link_states() -> dict:
	"""Recompute ``link_state`` for the whole fleet.

	Diagnosis, not detection. ``online`` already answers "did a reading arrive
	recently"; this answers "and why not", which is the question that decides
	whether you send a technician or fix the backhaul.

	Reads the database only -- gateway health comes from the rows
	:func:`gateway.sync_gateways` wrote, never from the API, so this stays cheap
	enough to run hourly.
	"""
	from upande_tagmeter import gateway as gateway_module

	now = now_datetime()
	cycle = _cycle_hours()
	late_cutoff = now - timedelta(hours=cycle * LATE_MULTIPLIER)
	silent_cutoff = now - timedelta(hours=cycle * SILENT_MULTIPLIER)
	down = set(gateway_module.unhealthy_gateways())

	buckets: dict[str, list[str]] = {}
	for row in frappe.get_all(
		"Water Meter",
		fields=["name", "last_seen", "last_sync_outcome", "gateway", "link_state"],
	):
		# Order matters: a meter the SMP holds nothing for is not "silent", and
		# neither is one that has genuinely never spoken.
		if row.last_sync_outcome == "server_error":
			state = "No Data on SMP"
		elif not row.last_seen:
			state = "Never Seen"
		elif row.last_seen >= late_cutoff:
			state = "Reporting"
		elif row.last_seen >= silent_cutoff:
			# One missed report is jitter. Gateway health is not consulted yet --
			# promoting this to Gateway Down would raise a network alarm on noise.
			state = "Late"
		elif row.gateway and row.gateway in down:
			state = "Gateway Down"
		else:
			state = "Silent"

		if row.link_state != state:
			buckets.setdefault(state, []).append(row.name)

	for state, names in buckets.items():
		frappe.db.set_value("Water Meter", {"name": ("in", names)}, "link_state", state,
		                    update_modified=False)

	return {
		"cycle_hours": cycle,
		"unhealthy_gateways": sorted(down),
		"counts": {state: len(names) for state, names in buckets.items()},
	}
```

- [ ] **Step 5: Register the scheduler entry**

In `hooks.py`, append `refresh_link_states` to `scheduler_events["hourly"]`, **after** `refresh_online_flags`:

```python
		"upande_tagmeter.sync.refresh_online_flags",
		# Diagnosis on top of detection. Must run after sync_gateways above, or
		# a Gateway Down verdict is computed against gateway rows an hour stale.
		"upande_tagmeter.sync.refresh_link_states",
	],
```

- [ ] **Step 6: Migrate and run the tests**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local migrate
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_sync
```

Expected: PASS — including the pre-existing `online` tests, unchanged.

- [ ] **Step 7: Run against the real fleet and sanity-check**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local execute upande_tagmeter.sync.refresh_link_states
bench --site kaitet.local mariadb --execute \
  "select link_state, count(*) from \`tabWater Meter\` group by link_state;"
```

Expected: ~96 `Reporting`, 1 `No Data on SMP` (`…076954`), and 3 in `Silent` — **not** `Gateway Down`, because all 100 are bound to the healthy gateway. If those three read `Gateway Down`, the binding or the health rule is wrong.

- [ ] **Step 8: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/upande_tagmeter/doctype/water_meter/water_meter.json \
        upande_tagmeter/sync.py upande_tagmeter/hooks.py upande_tagmeter/tests/test_sync.py
git commit -m "feat: link_state distinguishes a dead meter from a dead gateway"
```

---

### Task 6: Workspace Network group

**Files:**
- Modify: `upande_tagmeter/workspace_block.py:16-101` (the `GROUPS` list)
- Test: manual — the block is presentational and `sync()` is already idempotent

**Interfaces:**
- Consumes: `link_state` from Task 5, `TagMeter Gateway` from Task 1
- Produces: a `Network` tile group in the rendered workspace block

- [ ] **Step 1: Add the group**

In `upande_tagmeter/workspace_block.py`, insert a new tuple into `GROUPS` immediately after the `"Fleet"` group and before `"Telemetry"`:

```python
	("Network", [
		("TagMeter Gateway", "/app/tagmeter-gateway", "Gateways",
		 "Every gateway and its last heartbeat", "#0ea5e9",
		 '<path d="M5 12.6a10 10 0 0 1 14 0"/><path d="M8.5 16a5.5 5.5 0 0 1 7 0"/>'
		 '<line x1="12" y1="20" x2="12.01" y2="20"/><path d="M1.5 9a15 15 0 0 1 21 0"/>'),
		("Water Meter", "/app/water-meter?link_state=Gateway%20Down", "Gateway Down",
		 "Quiet because the network is, not the meter", "#dc2626",
		 '<line x1="2" y1="2" x2="22" y2="22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/>'
		 '<path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><line x1="12" y1="20" x2="12.01" y2="20"/>'),
		("Water Meter", "/app/water-meter?link_state=Silent", "Silent",
		 "Gateway is healthy, so it is the meter", "#b91c1c",
		 '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-10-8-10-8a18.45 18.45 0 0 1 5.06-5.94"/>'
		 '<line x1="1" y1="1" x2="23" y2="23"/>'),
		("Water Meter", "/app/water-meter?link_state=Late", "Late",
		 "Missed one expected report", "#d97706",
		 '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
	]),
```

- [ ] **Step 2: Re-sync and verify**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local execute upande_tagmeter.install.after_migrate
bench --site kaitet.local clear-cache
```

Open `http://127.0.0.1:8002/app/upande-tagmeter`. Expected: a **Network** group with four tiles between Fleet and Telemetry; the existing Fleet Online/Offline tiles still present and still working.

- [ ] **Step 3: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/workspace_block.py
git commit -m "feat: Network tile group in the workspace"
```

---

### Task 7: Dashboard payload

**Files:**
- Modify: `upande_tagmeter/www/tagmeter.py:74-196` (`build_payload`)
- Test: `upande_tagmeter/tests/test_api.py` (append)

**Interfaces:**
- Consumes: `gateway.unhealthy_gateways()`, `Water Meter.link_state`, `Water Meter.gateway`
- Produces: payload keys `meters[].link_state`, `meters[].gateway`, `gateways` (list of dicts with `id`, `label`, `online`, `last_heartbeat`, `hours`, `healthy`, `bound`, `lat`, `lon`, `alt`, `gps`), `kpi.gateways_down`, `kpi.meters_behind_down_gateway`

- [ ] **Step 1: Write the failing test**

Append to `upande_tagmeter/tests/test_api.py`:

```python
from upande_tagmeter.www import tagmeter as dashboard


class TestDashboardPayload(IntegrationTestCase):
	def test_payload_carries_gateways_and_link_state(self):
		payload = dashboard.build_payload()
		self.assertIn("gateways", payload)
		self.assertIn("gateways_down", payload["kpi"])
		self.assertIn("meters_behind_down_gateway", payload["kpi"])
		if payload["meters"]:
			self.assertIn("link_state", payload["meters"][0])
			self.assertIn("gateway", payload["meters"][0])

	def test_an_unhealthy_gateway_is_reported_unhealthy(self):
		gid = "TESTGWDASH000001"
		if not frappe.db.exists("TagMeter Gateway", gid):
			frappe.get_doc({
				"doctype": "TagMeter Gateway", "gateway_id": gid, "label": "Dash GW",
			}).insert()
		frappe.db.set_value("TagMeter Gateway", gid, {
			"online": 1,
			"last_heartbeat": now_datetime() - timedelta(hours=48),
			"last_polled_at": now_datetime(),
		}, update_modified=False)

		payload = dashboard.build_payload()
		row = next(g for g in payload["gateways"] if g["id"] == gid)
		self.assertFalse(row["healthy"], "a 48h-old heartbeat is not healthy")
		self.assertGreaterEqual(payload["kpi"]["gateways_down"], 1)
```

Ensure `test_api.py` imports `frappe`, `IntegrationTestCase`, `now_datetime` and `timedelta` at the top; add any that are missing.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_api
```

Expected: FAIL — `KeyError: 'gateways'`

- [ ] **Step 3: Extend the payload**

In `www/tagmeter.py`, add `"link_state", "gateway"` to the `fields` list in `build_payload` (after `"last_sync_outcome"`), then add to each meter dict built in the loop:

```python
			"link_state": row.link_state,
			"gateway": row.gateway,
```

Add this helper above `build_payload`:

```python
def _gateway_rows(now):
	"""Gateways with health resolved the same way link_state resolves it."""
	from upande_tagmeter import gateway as gateway_module

	down = set(gateway_module.unhealthy_gateways())
	bound = dict(
		frappe.db.sql(
			"""SELECT gateway, COUNT(*) FROM `tabWater Meter`
			   WHERE gateway IS NOT NULL AND gateway != '' GROUP BY gateway"""
		)
	)
	rows = frappe.get_all(
		"TagMeter Gateway",
		fields=["name", "label", "site", "gateway_status", "online", "last_heartbeat",
		        "last_polled_at", "latitude", "longitude", "altitude", "gps_time_sync"],
		order_by="label asc",
	)
	return [{
		"id": r.name,
		"label": r.label or r.name,
		"site": r.site,
		"state": r.gateway_status,
		"online": int(r.online or 0),
		"last_heartbeat": r.last_heartbeat,
		"hours": _hours_since(r.last_heartbeat, now),
		"healthy": r.name not in down,
		"polled": r.last_polled_at,
		"bound": int(bound.get(r.name, 0)),
		"lat": r.latitude,
		"lon": r.longitude,
		"alt": r.altitude,
		"gps": int(r.gps_time_sync or 0),
	} for r in rows]
```

Inside `build_payload`, after the `meters` loop, add:

```python
	gateways = _gateway_rows(now)
	unhealthy_ids = {g["id"] for g in gateways if not g["healthy"]}
	behind_down = sum(1 for m in meters if m["gateway"] in unhealthy_ids)
```

Add to the returned `kpi` dict:

```python
			"gateways": len(gateways),
			"gateways_down": len(unhealthy_ids),
			"meters_behind_down_gateway": behind_down,
```

and to the top-level return dict:

```python
		"gateways": gateways,
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local run-tests --module upande_tagmeter.tests.test_api
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/www/tagmeter.py upande_tagmeter/tests/test_api.py
git commit -m "feat: gateway health and link state in the dashboard payload"
```

---

### Task 8: Dashboard UI — Network view, banner, link state, Valve Board actions

**Files:**
- Modify: `upande_tagmeter/www/tagmeter.html` — nav (line ~281), views (line ~402), tables (line ~688-710), empty-state hint (line 733)
- Test: manual in the browser, driven by the commands in Step 6

**Interfaces:**
- Consumes: payload from Task 7; the existing `table(el, cols, rows)`, `call(method, args)`, `valveUI(elId, m)`, `link(m)`, `esc()`, `ago()`, `num()` helpers and the `CAN_COMMAND` / `CSRF` constants
- Produces: no new JS API

- [ ] **Step 1: Add the Network nav link**

After the Valves nav link (line ~281), add:

```html
      <a class="side__link" data-view="view-network"><svg viewBox="0 0 24 24"><path d="M5 12.6a10 10 0 0 1 14 0"/><path d="M8.5 16a5.5 5.5 0 0 1 7 0"/><line x1="12" y1="20" x2="12.01" y2="20"/><path d="M1.5 9a15 15 0 0 1 21 0"/></svg>Network<span class="n" id="n-network">–</span></a>
```

- [ ] **Step 2: Add the Network view section**

After the `view-valves` section closes (line ~420), add:

```html
<section class="view" id="view-network" hidden>
  <div class="pagehead">
    <h2>Network</h2>
    <p class="pagehead__sub">A gateway counts as healthy only when it says online <em>and</em> its heartbeat is fresh. The vendor's flag alone has been wrong.</p>
  </div>
  <div class="card">
    <div class="card__head"><h3>Gateways</h3><div class="meta" id="gw-meta"></div></div>
    <div class="scroll"><table class="tbl" id="gw-table"></table></div>
  </div>
</section>
```

- [ ] **Step 3: Add the banner element**

Immediately after the `<p class="pagehead__sub" id="head-sub">` line (~318), add:

```html
    <div id="gw-banner" hidden></div>
```

- [ ] **Step 4: Render the banner, the gateway table and the link-state column**

Append to the script block, after the alarms rendering (~line 652):

```js
// ---- gateways ----
$("n-network").textContent = TM.gateways.length;

const gwDown = TM.gateways.filter(g => !g.healthy);
if (gwDown.length) {
  const b = $("gw-banner");
  b.hidden = false;
  b.className = "empty";
  b.style.borderColor = "var(--bad, #dc2626)";
  b.innerHTML = `<b>${gwDown.length} gateway${gwDown.length > 1 ? "s" : ""} unhealthy:</b> ` +
    gwDown.map(g => `${esc(g.label)} (${esc(g.id)}, last heartbeat ${esc(ago(g.hours))})`).join(", ") +
    `. <b>${k.meters_behind_down_gateway}</b> meter(s) are bound to ${gwDown.length > 1 ? "them" : "it"} — ` +
    `their silence is the network's, not theirs. Alarm flags below are still the last known state of the pipe and remain valid.`;
}

$("gw-meta").textContent = `${TM.gateways.length} gateways · ${gwDown.length} unhealthy`;
table("gw-table", [
  { h: "Gateway", f: g => `<b>${esc(g.label)}</b><div style="color:var(--ink-faint)">${esc(g.id)}</div>` },
  { h: "Health", f: g => g.healthy
      ? '<span class="pill ok">healthy</span>'
      : '<span class="pill bad">unhealthy</span>' },
  { h: "Vendor flag", f: g => g.online ? "online" : '<span class="pill warn">offline</span>' },
  { h: "Last heartbeat", f: g => esc(ago(g.hours)) },
  { h: "Bound meters", num: true, f: g => g.bound },
  { h: "Position", f: g => g.gps && (g.lat || g.lon)
      ? `${num(g.lat, 4)}, ${num(g.lon, 4)} · ${num(g.alt, 0)} m`
      : '<span style="color:var(--ink-faint)">no fix</span>' },
  { h: "State", f: g => esc(g.state || "Active") },
], TM.gateways);

const STATE_PILL = {
  "Reporting": "ok", "Late": "warn", "Silent": "bad",
  "Gateway Down": "bad", "Never Seen": "", "No Data on SMP": "warn",
};
const stateCell = m => m.link_state
  ? `<span class="pill ${STATE_PILL[m.link_state] || ""}">${esc(m.link_state)}</span>`
  : '<span style="color:var(--ink-faint)">—</span>';
```

Then add a `Link state` column to the meters table (the `table("...", [...], TM.meters)` call ending at line ~697), immediately before the `Alarms` column:

```js
  { h: "Link state", f: stateCell },
```

- [ ] **Step 5: Add per-row valve actions to the Valve Board**

Replace the `Reachable` column in the `table("valve-table", ...)` call (lines ~707-710) with these two columns:

```js
  { h: "Reachable", f: m => `${m.online
      ? '<span class="pill ok">on the SMP</span>'
      : '<span class="pill bad">not reachable</span>'}<div>${stateCell(m)}</div>` },
  { h: "Action", f: m => {
      const blocked = m.in_flight || !CAN_COMMAND;
      return `<button class="btn btn--open" data-vsn="${esc(m.sn)}" data-vact="open" ${blocked ? "disabled" : ""}>Open</button>
              <button class="btn btn--close" data-vsn="${esc(m.sn)}" data-vact="close" ${blocked ? "disabled" : ""}>Close</button>`;
    } },
```

Then immediately after that `table("valve-table", ...)` call, add the delegated handler. It reuses the same guards and the same endpoint as `valveUI` — there is no second code path to the SMP:

```js
// One delegated listener rather than per-row bindings: the table is re-rendered
// on every reload, and set_valve is the only way a valve ever moves.
$("valve-table").addEventListener("click", async e => {
  const b = e.target.closest("button[data-vact]");
  if (!b || b.disabled) return;
  const m = TM.meters.find(x => x.sn === b.dataset.vsn);
  if (!m) return;
  const act = b.dataset.vact;
  const who = nameOf(m) === m.sn ? m.sn : `${nameOf(m)} (${m.sn})`;
  if (!confirm(`Ask meter ${who} to ${act} its valve?\n\nThe SMP accepts this immediately, but that only means it was queued — not that the valve moved.`)) return;
  $("valve-table").querySelectorAll("button[data-vact]").forEach(x => x.disabled = true);
  try {
    const r = await call("upande_tagmeter.valve.set_valve", { water_meter: m.sn, action: act });
    alert(r.status === "Rejected"
      ? `The SMP refused it: ${r.message}`
      : `Command ${r.command} queued. It stays queued until a reading shows the valve in that state.`);
    location.reload();
  } catch (err) {
    alert("Could not queue the command:\n\n" + err.message);
    $("valve-table").querySelectorAll("button[data-vact]").forEach(x => x.disabled = false);
  }
});
```

- [ ] **Step 6: Fix the stale empty-state hint**

At line ~733, replace the text that sends users to the desk:

```js
  : `<div class="empty">No valve command has been issued yet. Use the Open or Close button on any meter in the Valves view above.</div>`;
```

- [ ] **Step 7: Verify in the browser**

```bash
cd /home/austin/frappe-v16-bench
bench --site kaitet.local clear-cache
BROWSER=echo bench --site kaitet.local browse --user Administrator
```

Open the printed URL, then `/tagmeter`. Check each of these:

- The **Network** view lists both gateways; `F04CD5FFFE01CF70` reads **unhealthy** with a stale heartbeat and "no fix"; `0C4EC0FFFE00E97F` reads healthy with 100 bound meters and a Nairobi position.
- The unhealthy banner appears at the top and names the down gateway.
- The meters table has a **Link state** column with ~96 `Reporting`, 1 `No Data on SMP`, 3 `Silent`.
- The **Valves** view shows Open/Close buttons on all 10 rows. **Do not click them** unless you intend to actuate a real valve — `set_valve` queues a genuine downlink to live hardware.
- The browser console is free of errors.

- [ ] **Step 8: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add upande_tagmeter/www/tagmeter.html
git commit -m "feat: Network view, gateway banner, link state and Valve Board actions"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md` — add a section after "Fleet reality" (line ~352)

**Interfaces:**
- Consumes: everything above
- Produces: nothing

- [ ] **Step 1: Document the model**

Add to `README.md` after the "Fleet reality" section:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
cd /home/austin/frappe-v16-bench/apps/upande_tagmeter
git add README.md
git commit -m "docs: link state and gateway health"
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| `TagMeter Gateway` doctype | 1 |
| `parse_gateway_status` tests | 2 |
| `sync_gateways()` + hourly scheduling + ordering | 3 |
| Heartbeat-age health rule | 3 |
| `Water Meter.gateway` + seeding | 4 |
| `link_state` field + six states + precedence | 5 |
| Cadence config and thresholds | 5 |
| `online` regression guard | 5 |
| Error handling (failed poll, never polled, no gateway) | 3, 5 |
| Workspace Network group | 6 |
| Dashboard payload | 7 |
| Dashboard Network view, banner, link state column | 8 |
| Valve Board action column + stale hint | 8 |
| Alarms not suppressed | enforced by omission; stated in 8's banner copy and 9's README |
| Documentation | 9 |

No gaps.

**Placeholder scan:** no `TBD`, `TODO`, "add error handling", or "similar to Task N". Every code step carries real code.

**Type consistency:** `unhealthy_gateways() -> list[str]` is defined in Task 3 and consumed with that exact name and shape in Tasks 5 and 7. `refresh_link_states()` is defined in Task 5 and referenced in Task 5's hooks entry only. The payload keys produced in Task 7 (`gateways`, `g.healthy`, `g.bound`, `g.hours`, `g.id`, `g.label`, `g.lat`, `g.lon`, `g.alt`, `g.gps`, `g.state`, `kpi.meters_behind_down_gateway`) are exactly the keys Task 8's JS reads. `stateCell` is defined in Task 8 Step 4 and reused in Step 5. `link_state` Select options match the strings assigned in `refresh_link_states` and asserted in Task 5's table test.

**Known risk:** Task 4 Step 6 rewrites `kiwasco-devices.tsv` with `awk`. Verify with `head -2` as the step instructs, and confirm the file still has 101 lines before committing.
