"""The Frappe-aware half: poll the SMP and persist what comes back.

Everything that understands the vendor's wire behaviour lives in ``vendor/``.
This module only decides what to store and when.
"""

import json
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import now_datetime
from frappe.utils.data import convert_utc_to_system_timezone
from frappe.utils.synchronization import filelock

from upande_tagmeter.consumption import consumption_delta
from upande_tagmeter.vendor.client import TagMeterClient
from upande_tagmeter.vendor.errors import AuthFailed, BlockedByVendor, ConfigError, Outcome
from upande_tagmeter.vendor.parse import METER_TZ
from upande_tagmeter.vendor.status import FLAG_BITS

TOKEN_CACHE_KEY = "upande_tagmeter:token"
TOKEN_LOCK_NAME = "upande_tagmeter_token_refresh"

# Meters report roughly every 12 hours with staggered join times, so staleness
# has to be measured in tens of hours. A 6-hour threshold would mark most of the
# fleet offline every day and generate around a hundred false alarms.
DEFAULT_OFFLINE_AFTER_HOURS = 30

FLAG_FIELDS = tuple(FLAG_BITS)


class FrappeTokenStore:
	"""Shared token, one per site, in the Frappe cache.

	This has to be shared. Requesting a token from the SMP revokes the previous
	one, so if each worker kept its own they would revoke each other on every
	cycle. The lock makes refresh single-flight.

	``filelock`` is per-machine rather than distributed, which is sufficient for
	a single-server bench. A multi-server deployment would need a Redis lock
	instead -- noted here so the limitation is a decision, not a surprise.
	"""

	def get(self):
		return frappe.cache().get_value(TOKEN_CACHE_KEY)

	def set(self, token):
		# No expiry: the SMP never documented a token lifetime, and inventing
		# one would cause needless revocations. Refresh is reactive, on 401.
		frappe.cache().set_value(TOKEN_CACHE_KEY, token)

	@contextmanager
	def lock(self):
		with filelock(TOKEN_LOCK_NAME, timeout=60):
			yield


def get_client() -> TagMeterClient:
	url = frappe.conf.get("tagmeter_api_url")
	user = frappe.conf.get("tagmeter_api_user")
	password = frappe.conf.get("tagmeter_api_password")
	if not (url and user and password):
		raise ConfigError(
			"Add tagmeter_api_url, tagmeter_api_user and tagmeter_api_password to "
			"site_config.json. Credentials never live in a doctype field."
		)
	return TagMeterClient(
		url,
		user,
		password,
		token_store=FrappeTokenStore(),
		tz_name=frappe.conf.get("tagmeter_meter_timezone") or METER_TZ,
		min_interval=float(frappe.conf.get("tagmeter_min_interval") or 0.2),
		max_attempts=int(frappe.conf.get("tagmeter_max_attempts") or 3),
		retry_backoff=float(frappe.conf.get("tagmeter_retry_backoff") or 1.0),
	)


def _to_system_naive(aware_utc):
	"""Aware UTC -> naive datetime in the site's timezone, ready to store.

	Frappe renders naive datetimes as system-timezone local, so storing UTC
	would display every meter clock hours off. Conversion goes through the
	framework's own helper -- never a hand-rolled offset.
	"""
	if aware_utc is None:
		return None
	return convert_utc_to_system_timezone(aware_utc).replace(tzinfo=None)


def _offline_cutoff():
	hours = frappe.conf.get("tagmeter_offline_after_hours") or DEFAULT_OFFLINE_AFTER_HOURS
	return now_datetime() - timedelta(hours=float(hours))


# ── single meter ─────────────────────────────────────────────────────────────

def sync_meter(meter_sn: str, client: TagMeterClient | None = None) -> dict:
	"""Poll one meter and persist the result.

	Returns an explicit outcome rather than raising for expected conditions, so
	a fleet sweep survives one unregistered or unreachable meter.
	"""
	client = client or get_client()
	meter = frappe.get_doc("Water Meter", meter_sn)
	polled_at = now_datetime()

	outcome, data = client.get_latest_amr(meter_sn)

	if outcome is not Outcome.OK:
		_record_poll(meter, polled_at, outcome.value)
		if outcome is Outcome.UNKNOWN_METER:
			meter.db_set("vendor_registered", 0, update_modified=False)
		return {"status": outcome.value, "meter": meter_sn, "reading": None}

	meter.db_set("vendor_registered", 1, update_modified=False)

	if data is None:
		# The SMP knows this meter but holds no AMR record. Real for this fleet:
		# a meter emitting only a 1-byte keepalive produces no AMR record at all.
		_record_poll(meter, polled_at, "no_amr_record")
		return {"status": "no_amr_record", "meter": meter_sn, "reading": None}

	device_time = _to_system_naive(data["device_time"])
	if device_time is None:
		_record_poll(meter, polled_at, "no_device_time")
		return {"status": "no_device_time", "meter": meter_sn, "reading": None}

	dedupe_key = f"{meter_sn}:{device_time.isoformat()}"
	if frappe.db.exists("Meter Reading", {"dedupe_key": dedupe_key}):
		# Expected on most polls: the SMP returns its latest record every time,
		# and meters speak far less often than we ask.
		_record_poll(meter, polled_at, "duplicate")
		_apply_online(meter)
		return {"status": "duplicate", "meter": meter_sn, "reading": None}

	reading = _insert_reading(meter, data, device_time, dedupe_key, polled_at)
	if reading is None:
		_record_poll(meter, polled_at, "duplicate")
		return {"status": "duplicate", "meter": meter_sn, "reading": None}

	_apply_reading_to_meter(meter, data, device_time, reading.name, polled_at)
	return {"status": "ok", "meter": meter_sn, "reading": reading.name}


def _insert_reading(meter, data, device_time, dedupe_key, polled_at):
	is_first = not meter.last_reading
	previous = None if is_first else meter.cumulative_flow_m3
	values = {
		"doctype": "Meter Reading",
		"water_meter": meter.name,
		"device_time": device_time,
		"received_at": polled_at,
		"connection": data.get("connection"),
		"dedupe_key": dedupe_key,
		"cumulative_flow_m3": data.get("cumulative_flow_m3"),
		"consumption_m3": consumption_delta(previous, data.get("cumulative_flow_m3")),
		"remaining_balance_m3": data.get("remaining_balance_m3"),
		"instant_flow_m3h": data.get("instant_flow_m3h"),
		"temperature_c": data.get("temperature_c"),
		"rssi": data.get("rssi"),
		"snr": data.get("snr"),
		"valve_state": data.get("valve_state") or "Unknown",
		"status_raw": data.get("status_raw"),
		"status_text_mismatch": data.get("status_text_mismatch") or 0,
		"valve_status_text": data.get("valve_status_text"),
		"alarm_message_text": data.get("alarm_message_text"),
		"hourly_data": json.dumps(data.get("hourly") or [], indent=1),
	}
	for flag in FLAG_FIELDS:
		values[flag] = 1 if data.get(flag) else 0

	doc = frappe.get_doc(values)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.exceptions.DuplicateEntryError:
		# The unique index is the arbiter, not a prior existence check -- two
		# workers polling the same meter would both pass a check-then-insert.
		return None
	return doc


def _apply_reading_to_meter(meter, data, device_time, reading_name, polled_at):
	"""Denormalise the newest reading onto the meter.

	Guarded on recency: the SMP can return an older record than one we already
	hold (a re-poll after a meter's clock is resynced), and overwriting current
	state with stale values would be worse than skipping.
	"""
	if meter.last_seen and device_time <= meter.last_seen:
		_record_poll(meter, polled_at, "older_than_stored")
		return

	updates = {
		"last_reading": reading_name,
		"last_seen": device_time,
		"last_synced_at": polled_at,
		"last_sync_outcome": "ok",
		"status": "Active" if meter.status in ("Never Seen", None, "") else meter.status,
		"cumulative_flow_m3": data.get("cumulative_flow_m3"),
		"remaining_balance_m3": data.get("remaining_balance_m3"),
		"instant_flow_m3h": data.get("instant_flow_m3h"),
		"temperature_c": data.get("temperature_c"),
		"rssi": data.get("rssi"),
		"snr": data.get("snr"),
		"valve_reported": data.get("valve_state") or "Unknown",
		"status_raw": data.get("status_raw"),
		"online": 1 if device_time >= _offline_cutoff() else 0,
	}
	if data.get("dev_eui"):
		updates["dev_eui"] = data["dev_eui"]
	if data.get("connection"):
		updates["connection"] = data["connection"]
	for flag in FLAG_FIELDS:
		updates[flag] = 1 if data.get(flag) else 0

	meter.db_set(updates, update_modified=True)

	# A reading is the only proof a valve command landed. Imported here rather
	# than at module scope because valve.py needs get_client() from this module.
	from upande_tagmeter import valve

	valve.confirm_from_reading(
		meter.name, reading_name, updates["valve_reported"], device_time
	)


def _record_poll(meter, polled_at, outcome):
	meter.db_set(
		{"last_synced_at": polled_at, "last_sync_outcome": outcome}, update_modified=False
	)


def _apply_online(meter):
	online = 1 if (meter.last_seen and meter.last_seen >= _offline_cutoff()) else 0
	if int(meter.online or 0) != online:
		meter.db_set("online", online, update_modified=False)


# ── fleet ────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def poll_meter(meter_sn: str) -> dict:
	"""Poll one meter on demand -- the form's Poll Now button.

	A thin wrapper rather than whitelisting :func:`sync_meter` directly. That
	function takes an injected client so it can be tested without a network,
	and whitelisting it would both expose that parameter to the web and make
	Frappe enforce its type hint at runtime, which breaks the injection.
	"""
	return sync_meter(meter_sn)


@frappe.whitelist()
def sync_fleet(limit: int | None = None, only_profile: str | None = None) -> dict:
	"""Poll every active meter, serially, on one shared token.

	Serial by design. The SMP has no bulk read -- an array of meterIDs is
	rejected -- and its single-session token model means parallel workers would
	revoke each other. One sweep of 100 meters takes roughly one to three
	minutes.
	"""
	filters = {"status": ["!=", "Decommissioned"]}
	if only_profile:
		filters["meter_profile"] = only_profile
	names = frappe.get_all(
		"Water Meter", filters=filters, pluck="name", order_by="meter_sn asc",
		limit_page_length=int(limit) if limit else 0,
	)

	client = get_client()
	tally: dict[str, int] = {}
	failures = []
	for name in names:
		try:
			result = sync_meter(name, client=client)
			tally[result["status"]] = tally.get(result["status"], 0) + 1
		except (AuthFailed, BlockedByVendor) as exc:
			# Neither is per-meter: without a session, or blocked at their edge,
			# every remaining call fails identically. Stop rather than hammer
			# the SMP a hundred times.
			reason = type(exc).__name__
			frappe.log_error(frappe.get_traceback(), f"TagMeter sweep aborted: {reason}")
			tally[f"aborted_{reason.lower()}"] = tally.get(f"aborted_{reason.lower()}", 0) + 1
			break
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"TagMeter sync failed for {name}")
			failures.append(name)
			tally["error"] = tally.get("error", 0) + 1

	refresh_online_flags()
	return {"polled": len(names), "tally": tally, "failures": failures}


@frappe.whitelist()
def refresh_online_flags() -> dict:
	"""Recompute ``online`` for the whole fleet.

	This needs to be its own pass. A meter that goes quiet produces no reading,
	so nothing in :func:`sync_meter` would ever touch it -- ``online`` would sit
	at 1 forever for a meter that stopped reporting weeks ago.
	"""
	cutoff = _offline_cutoff()
	to_offline = frappe.db.sql(
		"""SELECT name FROM `tabWater Meter`
		   WHERE online = 1 AND (last_seen IS NULL OR last_seen < %s)""",
		(cutoff,), pluck=True,
	)
	to_online = frappe.db.sql(
		"""SELECT name FROM `tabWater Meter`
		   WHERE online = 0 AND last_seen IS NOT NULL AND last_seen >= %s""",
		(cutoff,), pluck=True,
	)
	for names, value in ((to_offline, 0), (to_online, 1)):
		if names:
			frappe.db.set_value("Water Meter", {"name": ("in", names)}, "online", value,
			                    update_modified=False)
	return {
		"cutoff": str(cutoff),
		"marked_offline": len(to_offline),
		"marked_online": len(to_online),
	}


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


@frappe.whitelist()
def test_connection() -> dict:
	"""Prove credentials and reachability without touching a meter."""
	client = get_client()
	token = client.authenticate()
	return {"ok": True, "token_prefix": token[:16], "base_url": client.base_url}
