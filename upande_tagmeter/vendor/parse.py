"""Turn SMP response bodies into plain dicts. No Frappe, no network.

Every quirk of the vendor's payloads is handled here so it can be pinned by a
unit test against a recorded response, without a bench and without the vendor
being reachable.
"""

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .status import decode_status, status_agrees_with_text

# The SMP uses two different timezones in two different fields. Getting this
# wrong is a silent two-hour error on every reading, so both are pinned by
# measurement rather than assumption.
#
# METER_TZ -- the "TimeStamp" inside dataAMRRecord / dataFreezeRecord / dataHourly.
#   This is the meter's own clock, carried in the AMR frame, and it reads UTC.
#   Measured 2026-09-09: meter 68750000076929 reported "2026-09-09 13:09:35"
#   while the wall clock was 13:09:11 UTC -- a live uplink landing 24 seconds
#   "ahead" of UTC. Had the clock been Amsterdam (UTC+2) it would have read
#   ~15:09. Four meters were sampled within minutes of the query and all
#   matched UTC to the second.
#
# SERVER_TZ -- "statTime" in get_gateway_status, which the SMP generates itself.
#   That one *is* Dutch local: 15:07:12 against 13:07:39 UTC. It therefore
#   shifts with European DST, which is why it goes through zoneinfo and not a
#   fixed offset.
#
# If the meters are ever re-synced with the protocol's set-date-time command,
# their clocks could move to local time -- hence the override rather than a
# hardcoded constant.
METER_TZ = "UTC"
SERVER_TZ = "Europe/Amsterdam"

# Retained name for the meter-record zone, which is what callers parse.
VENDOR_TZ = METER_TZ

METER_SN_RE = re.compile(r"^[0-9]{14}$")
_STATUS_PREFIX_RE = re.compile(r"^\s*([0-9A-Fa-f]{1,4})")


def parse_status_prefix(value: Any) -> int | None:
	"""``"08[Valve open;Low Balance Alarm;]"`` -> ``0x08``.

	See :func:`status_agrees_with_text` for why hex, and how a wrong guess is
	made visible rather than silent.
	"""
	if value is None:
		return None
	match = _STATUS_PREFIX_RE.match(str(value))
	if not match:
		return None
	return int(match.group(1), 16)


def parse_vendor_datetime(value: Any, tz_name: str = METER_TZ) -> datetime | None:
	"""``"2026-08-13 09:54:06"`` in ``tz_name`` -> aware UTC.

	Their timestamps carry no offset, so the zone has to be supplied -- and it
	differs by field. See :data:`METER_TZ` and :data:`SERVER_TZ`. Returns None
	for absent or unparseable values rather than raising: one malformed
	timestamp must not cost us the rest of the record.
	"""
	if not value:
		return None
	text = str(value).strip()
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
		try:
			naive = datetime.strptime(text, fmt)
		except ValueError:
			continue
		return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
	return None


def _f(value: Any) -> float | None:
	"""Vendor numerics arrive as JSON numbers, but tolerate strings and nulls."""
	if value is None or value == "":
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def parse_amr(body: dict, tz_name: str = METER_TZ) -> dict[str, Any] | None:
	"""Normalise one ``get_latest_amr`` body.

	Returns None when the response carries no ``dataAMRRecord`` -- a meter the
	platform knows but has never produced an AMR record for. That is a real
	state for this fleet: meters emitting a 1-byte keepalive create no AMR
	record at all, so the vendor answers 200 with no record rather than an error.

	Units: ``PrepaidBalance`` is m3 -- confirmed empirically, since a
	``recharge_meter`` of 0.001 moved a balance of 2 to 2.001. ``TotalCounter``
	is *assumed* m3 on the same scale, which cannot yet be verified because it
	reads 0 across the whole fleet. Worth re-checking once water actually flows.
	"""
	record = (body or {}).get("dataAMRRecord")
	if not isinstance(record, dict) or not record:
		return None

	valve_text = record.get("ValveStatus") or ""
	alarm_text = record.get("AlarmMessage") or ""
	st1 = parse_status_prefix(valve_text)
	st2 = parse_status_prefix(alarm_text)

	out: dict[str, Any] = {
		"meter_sn": (record.get("meterID") or "").strip(),
		"dev_eui": (record.get("devEUI") or "").strip().lower() or None,
		"connection": (record.get("connection") or "").strip() or None,
		"device_time": parse_vendor_datetime(record.get("TimeStamp"), tz_name),
		"cumulative_flow_m3": _f(record.get("TotalCounter")),
		"remaining_balance_m3": _f(record.get("PrepaidBalance")),
		"instant_flow_m3h": _f(record.get("CurrentFlow")),
		"temperature_c": _f(record.get("Temperature")),
		"rssi": _f(record.get("RSSI")),
		"snr": _f(record.get("SNR")),
		"valve_status_text": valve_text or None,
		"alarm_message_text": alarm_text or None,
	}

	if st1 is None and st2 is None:
		out["status_raw"] = None
		out["valve_state"] = "Unknown"
		out["status_text_mismatch"] = 0
	else:
		decoded = decode_status(st1 or 0, st2 or 0)
		out.update(decoded)
		# Keep the vendor's own wording alongside our decode. When they
		# disagree the reading is still stored -- both interpretations are
		# retained so a human can settle it, rather than one being discarded.
		out["status_text_mismatch"] = 0 if status_agrees_with_text(decoded, valve_text, alarm_text) else 1

	out["hourly"] = parse_hourly(body.get("dataHourly"), tz_name)
	return out


def parse_hourly(rows: Any, tz_name: str = METER_TZ) -> list[dict[str, Any]]:
	"""The rolling 24-hour series, as plain rows ready to store as JSON.

	Kept as a JSON blob on the reading rather than a child table: 100 meters at
	roughly two records a day would generate ~1.7M child rows a year, and these
	24 values are read as a block. If per-hour querying is ever needed it can be
	materialised then, from data already retained.
	"""
	if not isinstance(rows, list):
		return []
	parsed = []
	for row in rows:
		if not isinstance(row, dict):
			continue
		stamp = parse_vendor_datetime(row.get("TimeStamp"), tz_name)
		parsed.append({
			"timestamp": stamp.isoformat() if stamp else None,
			"total_counter": _f(row.get("TotalCounter")),
			"delta": _f(row.get("NextHourDifference")),
		})
	return parsed


def is_valid_meter_sn(value: Any) -> bool:
	"""14 decimal digits, exactly. See :class:`InvalidMeterID`."""
	return bool(value) and bool(METER_SN_RE.match(str(value).strip()))


def parse_gateway_status(body: dict, tz_name: str = SERVER_TZ) -> dict[str, Any]:
	"""Normalise ``get_gateway_status``.

	``statTime`` is generated by the SMP itself, so unlike meter records it is
	in Dutch local time -- see :data:`SERVER_TZ`.
	"""
	return {
		"gateway_id": (body.get("gatewayID") or "").strip().upper() or None,
		"online": bool(body.get("online")),
		"mqtt_protocol": bool(body.get("mqtt_protocol")),
		"latitude": _f(body.get("latitude")),
		"longitude": _f(body.get("longitude")),
		"altitude": _f(body.get("altitude")),
		"gps_time_sync": bool(body.get("gpsTimeSync")),
		"stat_time": parse_vendor_datetime(body.get("statTime"), tz_name),
		"reported_timezone": body.get("timezone"),
	}
