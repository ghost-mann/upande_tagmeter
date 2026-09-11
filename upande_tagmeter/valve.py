"""Valve control through the SMP, with honest confirmation.

The SMP answers ``"Operation success!"`` the moment it accepts a request into
its downlink queue. That is not the valve moving. On Class B the meter listens
only in scheduled ping slots, and the one round trip measured so far -- a
prepaid recharge on 2026-08-19 -- took about eleven hours.

So this module never claims a valve moved. It records what was asked
(``valve_desired``), what the meter last reported (``valve_reported``), and
confirms a command only when a reading *taken after the command was sent* shows
the requested state.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from upande_tagmeter.sync import get_client
from upande_tagmeter.vendor.errors import AuthFailed, BlockedByVendor, Outcome

# Comfortably clears one 12-hour AMR cycle. A shorter window produces false
# failures on Class B, where a command legitimately waits hours.
DEFAULT_EXPIRY_HOURS = 26

ACTIONS = {
	"open": {"command_type": "valve_open", "vendor": "Open", "requested_state": "Open"},
	"close": {"command_type": "valve_close", "vendor": "Close", "requested_state": "Closed"},
}


@frappe.whitelist()
def set_valve(water_meter: str, action: str, expiry_hours: float | None = None) -> dict:
	"""Ask a meter to open or close its valve.

	Returns immediately. The command is ``Queued`` until a reading proves
	otherwise -- see :func:`confirm_from_reading`.
	"""
	key = (action or "").strip().lower()
	if key not in ACTIONS:
		frappe.throw(f"action must be one of {', '.join(ACTIONS)}, got {action!r}")
	spec = ACTIONS[key]

	meter = frappe.get_doc("Water Meter", water_meter)
	if meter.meter_profile != "Quinto Prepaid":
		frappe.throw(
			f"{meter.name} is profile {meter.meter_profile!r} and has no valve. Only the "
			"Quinto Prepaid meters can be actuated."
		)
	if meter.valve_in_flight and frappe.db.get_value(
		"Meter Command", meter.valve_in_flight, "status"
	) == "Queued":
		frappe.throw(
			f"{meter.valve_in_flight} is still in flight for this meter. Two competing "
			"downlinks make the outcome ambiguous -- wait for it to confirm or expire."
		)

	hours = float(expiry_hours or DEFAULT_EXPIRY_HOURS)
	command = frappe.get_doc({
		"doctype": "Meter Command",
		"water_meter": meter.name,
		"command_type": spec["command_type"],
		"requested_state": spec["requested_state"],
		"status": "Queued",
		"requested_by": frappe.session.user,
		"enqueued_at": now_datetime(),
		"expires_at": add_to_date(now_datetime(), hours=hours),
		"attempts": 0,
	})
	command.insert(ignore_permissions=True)

	meter.db_set({
		"valve_desired": spec["requested_state"],
		"valve_in_flight": command.name,
	}, update_modified=False)

	result = attempt(command.name)
	return {"command": command.name, **result}


def attempt(command_name: str, client=None) -> dict:
	"""Send (or re-send) one command to the SMP. Never retried inside a call."""
	command = frappe.get_doc("Meter Command", command_name)
	if command.status != "Queued":
		return {"status": command.status, "sent": False, "reason": "not queued"}

	spec = next(s for s in ACTIONS.values() if s["command_type"] == command.command_type)
	client = client or get_client()

	try:
		outcome, body = client.set_valve(command.water_meter, spec["vendor"])
	except (AuthFailed, BlockedByVendor) as exc:
		# Not the meter's fault and not retryable per-command.
		command.db_set({
			"attempts": (command.attempts or 0) + 1,
			"last_attempt_at": now_datetime(),
			"last_outcome": type(exc).__name__,
			"vendor_message": str(exc)[:500],
		}, update_modified=False)
		raise

	message = (body or {}).get("message") if isinstance(body, dict) else None
	command.db_set({
		"attempts": (command.attempts or 0) + 1,
		"last_attempt_at": now_datetime(),
		"last_outcome": outcome.value,
		"vendor_message": message,
	}, update_modified=False)

	if outcome is Outcome.REJECTED:
		# The SMP refused the request outright. Retrying an identical rejected
		# request is pointless, so fail it now rather than let it sit Queued.
		_close_out(command, "Rejected", failure_reason=f"SMP refused the command: {message!r}")
		return {"status": "Rejected", "sent": True, "message": message}

	return {"status": command.status, "sent": outcome is Outcome.OK, "outcome": outcome.value,
	        "message": message}


def confirm_from_reading(water_meter: str, reading_name: str, valve_state: str, device_time) -> str | None:
	"""Confirm a queued command if this reading proves the requested state.

	Called from the sync path on every newly stored reading.

	The reading must be *newer than the enqueue*. A reading taken before the
	command was sent proves nothing about it. Note this cannot distinguish "the
	command worked" from "the valve was already in that state" -- nothing in the
	data can, so the field is named ``confirming_reading`` rather than anything
	implying causation.

	A reading showing the *opposite* state is not treated as failure: on Class B
	the downlink may simply not have arrived yet. Failure comes from expiry.
	"""
	queued = frappe.get_all(
		"Meter Command",
		filters={"water_meter": water_meter, "status": "Queued"},
		fields=["name", "requested_state", "enqueued_at"],
		order_by="creation asc",
	)
	for row in queued:
		if row.requested_state != valve_state:
			continue
		if row.enqueued_at and device_time and device_time <= row.enqueued_at:
			continue
		command = frappe.get_doc("Meter Command", row.name)
		command.db_set({
			"confirmed_at": now_datetime(),
			"confirming_reading": reading_name,
		}, update_modified=False)
		_close_out(command, "Confirmed")
		return command.name
	return None


@frappe.whitelist()
def watchdog() -> dict:
	"""Expire overdue commands and re-send ones the SMP never accepted.

	Re-sending is deliberately narrow: if the SMP already answered
	``"Operation success!"``, the downlink is sitting in *their* queue and
	sending again would stack a duplicate. Only an attempt that failed to reach
	them -- transport error, server error, bad response -- is worth repeating.
	"""
	expired = resent = 0
	failures = []
	for row in frappe.get_all(
		"Meter Command", filters={"status": "Queued"},
		fields=["name", "attempts", "max_attempts", "expires_at", "last_outcome"],
	):
		command = frappe.get_doc("Meter Command", row.name)
		if row.expires_at and now_datetime() > row.expires_at:
			_close_out(command, "Expired", failure_reason=(
				"The meter never reported the requested state before the command expired. "
				"On Class B this usually means the meter is not reachable on the SMP's "
				"network server, or has stopped transmitting."
			))
			expired += 1
			continue

		if row.last_outcome == Outcome.OK.value:
			continue  # accepted; waiting on the meter, not on us
		if (row.attempts or 0) >= (row.max_attempts or 3):
			_close_out(command, "Failed", failure_reason=(
				f"The SMP never accepted the command after {row.attempts} attempts "
				f"(last outcome: {row.last_outcome})."
			))
			failures.append(row.name)
			continue
		try:
			attempt(row.name)
			resent += 1
		except (AuthFailed, BlockedByVendor):
			frappe.log_error(frappe.get_traceback(), "TagMeter valve watchdog aborted")
			break
	return {"expired": expired, "resent": resent, "failed": failures}


def _close_out(command, status: str, failure_reason: str | None = None) -> None:
	updates = {"status": status}
	if failure_reason:
		updates["failure_reason"] = failure_reason
	command.db_set(updates, update_modified=True)
	# The meter is only "in flight" while something is actually queued.
	if frappe.db.get_value("Water Meter", command.water_meter, "valve_in_flight") == command.name:
		frappe.db.set_value("Water Meter", command.water_meter, "valve_in_flight", None,
		                    update_modified=False)


@frappe.whitelist()
def reconciliation_report() -> list[dict]:
	"""Meters whose reported valve state disagrees with what was requested.

	A disagreement is not necessarily a fault: it may simply be a command in
	flight. The in-flight column is what tells the two apart.
	"""
	rows = frappe.get_all(
		"Water Meter",
		filters={"meter_profile": "Quinto Prepaid", "valve_desired": ["!=", "Unknown"]},
		fields=["name", "valve_desired", "valve_reported", "valve_in_flight", "last_seen", "online"],
		order_by="meter_sn asc",
	)
	return [r for r in rows if r.valve_desired != r.valve_reported]
