"""Whitelisted endpoints for the fleet dashboard.

Kept apart from sync.py: that module owns talking to the SMP, this one owns
answering the browser. Everything here is read-only except the valve call,
which delegates to valve.set_valve so the dashboard and the desk form go
through exactly the same guard rails.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime


def _require_login():
	if frappe.session.user == "Guest":
		frappe.throw(_("Log in to read meter data."), frappe.PermissionError)


def _period_total(meter_sn: str, hours: float, until=None) -> float:
	"""Consumption over a trailing window.

	Sums ``consumption_m3``, which sync.py computes per reading as the delta
	since the previous one -- so this is real usage over the window, not the
	difference between two lifetime counters, and it is unaffected by a meter
	replacement resetting its counter.
	"""
	until = until or now_datetime()
	total = frappe.db.sql(
		"""SELECT COALESCE(SUM(consumption_m3), 0) FROM `tabMeter Reading`
		   WHERE water_meter = %s AND device_time > %s AND device_time <= %s""",
		(meter_sn, add_to_date(until, hours=-hours), until),
	)
	return round(float(total[0][0] or 0), 3)


@frappe.whitelist()
def flow_series(meter_sn: str, start: str | None = None, end: str | None = None) -> dict:
	"""Readings, hourly flow rate and period totals for one meter."""
	_require_login()
	if not frappe.db.exists("Water Meter", meter_sn):
		frappe.throw(_("No meter {0}").format(meter_sn))

	end_dt = get_datetime(end) if end else now_datetime()
	start_dt = get_datetime(start) if start else add_to_date(end_dt, days=-30)

	rows = frappe.get_all(
		"Meter Reading",
		filters={"water_meter": meter_sn, "device_time": ["between", [start_dt, end_dt]]},
		fields=["device_time", "cumulative_flow_m3", "consumption_m3",
		        "remaining_balance_m3", "instant_flow_m3h", "temperature_c",
		        "rssi", "valve_state", "hourly_data"],
		order_by="device_time asc",
	)

	# The hourly series comes off the newest reading in range: the SMP ships a
	# rolling 24-hour window with each AMR record, where `delta` is that hour's
	# volume -- which is the flow rate for the hour, already differenced.
	hourly = []
	hourly_from = None
	if rows:
		newest = rows[-1]
		hourly_from = newest.device_time
		try:
			for row in json.loads(newest.hourly_data or "[]"):
				if row.get("timestamp"):
					hourly.append({"t": row["timestamp"], "v": row.get("delta") or 0})
		except (ValueError, TypeError):
			hourly = []

	readings = [{
		"t": r.device_time,
		"cum": r.cumulative_flow_m3,
		"use": r.consumption_m3,
		"bal": r.remaining_balance_m3,
		"rate": r.instant_flow_m3h,
		"temp": r.temperature_c,
		"rssi": r.rssi,
		"valve": r.valve_state,
	} for r in rows]

	used = sum(r["use"] or 0 for r in readings)
	meter = frappe.db.get_value(
		"Water Meter", meter_sn,
		["meter_sn", "meter_profile", "cumulative_flow_m3", "remaining_balance_m3",
		 "valve_reported", "valve_desired", "valve_in_flight", "online", "last_seen"],
		as_dict=True,
	)

	return {
		"meter": meter,
		"start": start_dt,
		"end": end_dt,
		"totals": {
			"day": _period_total(meter_sn, 24, end_dt),
			"week": _period_total(meter_sn, 24 * 7, end_dt),
			"month": _period_total(meter_sn, 24 * 30, end_dt),
			"range": round(used, 3),
		},
		"readings": readings,
		"hourly": hourly,
		"hourly_from": hourly_from,
		# The dashboard uses this to decide between a chart and a plain
		# statement. Drawing a flat line at zero would imply a measurement
		# that has not happened.
		"has_flow": any((r["use"] or 0) > 0 for r in readings)
		            or any(h["v"] for h in hourly),
	}


@frappe.whitelist()
def meter_options() -> list[dict]:
	"""Meters for the dashboard picker, freshest first."""
	_require_login()
	return frappe.get_all(
		"Water Meter",
		fields=["name", "meter_sn", "meter_profile", "online", "last_seen"],
		order_by="last_seen desc, meter_sn asc",
	)


MAX_LABEL = 140


@frappe.whitelist()
def rename_meter(meter_sn: str, label: str | None = None,
                 site: str | None = None, zone: str | None = None) -> dict:
	"""Give a meter a human name, and optionally place it.

	This sets a label; it does not rename the document. ``Water Meter`` is
	auto-named from ``meter_sn`` because that serial is the SMP's ``meterID``
	and the key every reading and command joins on -- renaming the record would
	break the mapping to the vendor silently, and the next sweep would create a
	second meter. So the serial stays the identity and this is what people read.

	Passing ``None`` leaves a field alone; passing an empty string clears it.
	"""
	_require_login()
	frappe.has_permission("Water Meter", "write", throw=True)
	if not frappe.db.exists("Water Meter", meter_sn):
		frappe.throw(_("No meter {0}").format(meter_sn))

	doc = frappe.get_doc("Water Meter", meter_sn)
	if label is not None:
		label = label.strip()
		if len(label) > MAX_LABEL:
			frappe.throw(_("A name can be at most {0} characters.").format(MAX_LABEL))
		doc.meter_label = label or None
	if site is not None:
		doc.site = site.strip() or None
	if zone is not None:
		doc.zone = zone.strip() or None
	doc.save(ignore_permissions=False)

	return {
		"meter_sn": doc.name,
		"label": doc.meter_label,
		"site": doc.site,
		"zone": doc.zone,
		"display": doc.meter_label or doc.name,
	}


@frappe.whitelist()
def rename_many(rows) -> dict:
	"""Apply several renames at once, so a whole block can be labelled in one go.

	Each row is applied independently: one bad row is reported and the rest
	still land, because refusing the batch would lose the operator's typing.
	"""
	_require_login()
	frappe.has_permission("Water Meter", "write", throw=True)
	if isinstance(rows, str):
		rows = json.loads(rows)

	saved, failed = [], []
	for row in rows or []:
		try:
			saved.append(rename_meter(
				row.get("meter_sn"), row.get("label"), row.get("site"), row.get("zone")
			))
		except Exception as exc:
			frappe.clear_last_message()
			failed.append({"meter_sn": row.get("meter_sn"), "error": str(exc)[:200]})
	return {"saved": saved, "failed": failed}
