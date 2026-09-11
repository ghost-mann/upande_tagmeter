"""Data for the TagMeter fleet dashboard at /tagmeter.

Everything here is derived from what the SMP actually returned. Where a value
has no data behind it, this returns the absence rather than a zero, so the page
can say so instead of drawing an empty chart.
"""

import json
from collections import Counter

import frappe
from frappe import _
from frappe.utils import get_system_timezone, now_datetime

no_cache = 1

# Meters report roughly every 12 hours, so recency bands are measured in hours
# rather than minutes. These drive the fleet grid's colour ramp.
RECENCY_BANDS = [
	("live", 2, "Reported in the last 2 hours"),
	("recent", 12, "2 to 12 hours ago"),
	("stale", 30, "12 to 30 hours ago"),
	("cold", None, "More than 30 hours ago"),
]

ALARM_FIELDS = [
	("alarm_empty_pipe", "Empty pipe"),
	("alarm_reverse_flow", "Reverse flow"),
	("alarm_low_balance", "Low prepaid balance"),
	("battery_low", "Battery low (ST1)"),
	("alarm_battery_meter", "Battery alarm (ST2)"),
	("alarm_leak", "Leak"),
	("alarm_pipe_burst", "Pipe burst"),
	("alarm_overload_flow", "Overload flow"),
	("alarm_temp", "Temperature"),
	("alarm_ee", "EE"),
]


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.throw(_("Log in to view the TagMeter dashboard."), frappe.PermissionError)
	context.no_cache = 1
	context.payload = json.dumps(build_payload(), default=str)
	# The dashboard issues valve commands, so it needs the CSRF token Frappe
	# expects on any state-changing call.
	context.csrf_token = frappe.sessions.get_csrf_token()
	context.can_command = int(frappe.has_permission("Meter Command", "create"))
	context.can_write = int(frappe.has_permission("Water Meter", "write"))
	context.user_fullname = frappe.utils.get_fullname(frappe.session.user)
	context.user_initials = "".join(
		part[0] for part in (context.user_fullname or "?").split()[:2]
	).upper()
	return context


def _hours_since(when, now):
	if not when:
		return None
	return round((now - when).total_seconds() / 3600.0, 2)


def _band(hours):
	if hours is None:
		return "never"
	for key, limit, _label in RECENCY_BANDS:
		if limit is None or hours < limit:
			return key
	return "cold"


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


def build_payload() -> dict:
	now = now_datetime()
	fields = [
		"name", "meter_sn", "meter_label", "device_index", "meter_profile", "dev_eui", "connection",
		"site", "zone", "status", "online", "last_seen", "last_synced_at", "last_sync_outcome",
		"link_state", "gateway",
		"cumulative_flow_m3", "remaining_balance_m3", "instant_flow_m3h", "temperature_c",
		"rssi", "snr", "valve_reported", "valve_desired", "valve_in_flight", "status_raw",
	] + [f for f, _ in ALARM_FIELDS]

	rows = frappe.get_all("Water Meter", fields=fields, order_by="device_index asc")

	meters = []
	for row in rows:
		hours = _hours_since(row.last_seen, now)
		meters.append({
			"sn": row.meter_sn,
			"label": row.meter_label,
			# What the dashboard shows. The serial stays visible as the
			# secondary identifier so nobody loses the SMP key.
			"display": row.meter_label or row.meter_sn,
			"zone": row.zone,
			"idx": row.device_index,
			"profile": row.meter_profile,
			"valved": row.meter_profile == "Quinto Prepaid",
			"dev_eui": row.dev_eui,
			"bore": row.connection,
			"status": row.status,
			"online": int(row.online or 0),
			"last_seen": row.last_seen,
			"hours": hours,
			"band": _band(hours),
			"outcome": row.last_sync_outcome,
			"link_state": row.link_state,
			"gateway": row.gateway,
			"flow": row.cumulative_flow_m3,
			"balance": row.remaining_balance_m3,
			"rate": row.instant_flow_m3h,
			"temp": row.temperature_c,
			"rssi": row.rssi,
			"snr": row.snr,
			"valve": row.valve_reported,
			"valve_desired": row.valve_desired,
			"in_flight": row.valve_in_flight,
			"raw": row.status_raw,
			"alarms": [label for field, label in ALARM_FIELDS if row.get(field)],
		})

	reported = [m for m in meters if m["hours"] is not None]
	bands = Counter(m["band"] for m in meters)

	# Consumption is genuinely zero fleet-wide: the meters are installed but no
	# water has passed them. Report that as a fact, not as a chart of zeros.
	total_flow = sum(m["flow"] or 0 for m in meters)
	flowing = [m for m in meters if (m["flow"] or 0) > 0]

	valved = [m for m in meters if m["valved"]]
	rssi_values = [m["rssi"] for m in reported if m["rssi"]]
	temps = [m["temp"] for m in reported if m["temp"]]

	alarm_counts = []
	for field, label in ALARM_FIELDS:
		count = sum(1 for m in meters if label in m["alarms"])
		if count:
			alarm_counts.append({"label": label, "count": count})
	alarm_counts.sort(key=lambda a: -a["count"])

	readings = frappe.get_all(
		"Meter Reading",
		fields=["water_meter", "device_time", "cumulative_flow_m3", "consumption_m3",
		        "remaining_balance_m3", "valve_state", "status_raw", "rssi", "snr",
		        "temperature_c", "valve_status_text", "alarm_message_text"],
		order_by="device_time desc", limit_page_length=14,
	)

	commands = frappe.get_all(
		"Meter Command",
		fields=["name", "water_meter", "command_type", "requested_state", "status",
		        "attempts", "enqueued_at", "expires_at", "vendor_message"],
		order_by="creation desc", limit_page_length=10,
	)

	# Signal strength is one of the few fields with genuine spread, so it is
	# worth banding rather than averaging away.
	def rssi_band(v):
		if v is None:
			return None
		if v >= -60:
			return "strong"
		if v >= -85:
			return "fair"
		return "weak"

	signal = Counter(rssi_band(m["rssi"]) for m in reported if m["rssi"])

	gateways = _gateway_rows(now)
	unhealthy_ids = {g["id"] for g in gateways if not g["healthy"]}
	behind_down = sum(1 for m in meters if m["gateway"] in unhealthy_ids)

	return {
		"generated": now,
		"timezone": get_system_timezone(),
		"meters": meters,
		"gateways": gateways,
		"kpi": {
			"total": len(meters),
			"reported": len(reported),
			"online": sum(1 for m in meters if m["online"]),
			"never": bands.get("never", 0),
			"no_data": sum(1 for m in meters if m["outcome"] == "server_error"),
			"valved": len(valved),
			"valved_online": sum(1 for m in valved if m["online"]),
			"total_flow": round(total_flow, 3),
			"flowing": len(flowing),
			"credit": round(sum(m["balance"] or 0 for m in valved), 3),
			"alarms": sum(1 for m in meters if m["alarms"]),
			"avg_rssi": round(sum(rssi_values) / len(rssi_values), 1) if rssi_values else None,
			"avg_temp": round(sum(temps) / len(temps), 1) if temps else None,
			"commands_open": sum(1 for c in commands if c.status == "Queued"),
			"gateways": len(gateways),
			"gateways_down": len(unhealthy_ids),
			"meters_behind_down_gateway": behind_down,
		},
		"bands": [
			{"key": key, "label": label, "count": bands.get(key, 0)}
			for key, _limit, label in RECENCY_BANDS
		] + [{"key": "never", "label": "Never reported", "count": bands.get("never", 0)}],
		"signal": [
			{"key": "strong", "label": "Strong · ≥ −60 dBm", "count": signal.get("strong", 0)},
			{"key": "fair", "label": "Fair · −60 to −85", "count": signal.get("fair", 0)},
			{"key": "weak", "label": "Weak · < −85", "count": signal.get("weak", 0)},
		],
		"alarm_counts": alarm_counts,
		"readings": [dict(r) for r in readings],
		"commands": [dict(c) for c in commands],
	}
