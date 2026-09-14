"""Valve control through the SMP, with honest confirmation.

The SMP answers ``"Operation success!"`` the moment it accepts a request into
its downlink queue. That is not the valve moving, so this module never claims it
did: it records what was asked (``valve_desired``), what the meter last reported
(``valve_reported``), and confirms a command only when a reading *taken after
the command was sent* shows the requested state.

**Timing, corrected 2026-09-11.** An earlier version of this file argued that a
round trip took about eleven hours, from a prepaid recharge measured on
2026-08-19. That was wrong, and it made the whole module too cautious.

The vendor's own console history, captured from its internal API, shows what
actually happens: the meter acts on a downlink within ~15 seconds, and its
uplink reaches the SMP a consistent ~2m04s later -- ``meterTime 11:28:46`` ->
``createTime 11:30:50``, the same gap on every row. That history also holds two
confirmations **ten seconds apart**, so issuing commands back to back is normal
and the platform has no lock of its own.

The eleven hours was the meters' *idle* AMR cadence (9-21h) mistaken for the
actuation cycle. They are different things: an untouched meter reports twice a
day, an actuated one answers immediately. Conflating them produced a 26-hour
expiry and an in-flight lock that took a meter out of service for a day over a
command that had already succeeded.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from upande_tagmeter.sync import get_client
from upande_tagmeter.vendor.errors import AuthFailed, BlockedByVendor, Outcome

# Measured 2026-09-11 against the vendor's own console history: a meter acts on
# a downlink within ~15s and its uplink lands in the SMP a fixed ~2m04s later
# (meterTime 11:28:46 -> createTime 11:30:50, repeatedly). Two confirmations ten
# seconds apart are in that history, so back-to-back commands are normal.
#
# An earlier 26-HOUR expiry came from reading the meters' *idle* AMR cadence
# (9-21h) as the actuation cycle. It is not: idle telemetry and command response
# are different things, and conflating them locked meters out for a day.
VALVE_TIMEOUT_MINUTES = 10

# A second command for the SAME state this soon is a double-click, not intent.
# Returning the existing command keeps it idempotent without a second downlink.
DUPLICATE_WINDOW_SECONDS = 30

ACTIONS = {
	"open": {"command_type": "valve_open", "vendor": "Open", "requested_state": "Open"},
	"close": {"command_type": "valve_close", "vendor": "Close", "requested_state": "Closed"},
}


def _pending(water_meter: str):
	"""The newest still-Queued command for this meter, or None.

	Read from Meter Command rather than Water Meter.valve_in_flight: the field
	is a convenience pointer that a release or a crash can leave stale, while
	the command's own status is the fact.
	"""
	rows = frappe.get_all(
		"Meter Command", filters={"water_meter": water_meter, "status": "Queued"},
		fields=["name", "requested_state", "enqueued_at"],
		order_by="enqueued_at desc", limit_page_length=1,
	)
	return rows[0] if rows else None


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
	pending = _pending(meter.name)

	# What the valve is heading towards, which is NOT what it last reported:
	# reported lags a command by roughly two minutes, so on open -> close -> open
	# the third click would be refused against a stale "Open" without this.
	effective = pending.requested_state if pending else meter.valve_reported

	if effective == spec["requested_state"]:
		if pending:
			age = (now_datetime() - pending.enqueued_at).total_seconds()
			if age <= DUPLICATE_WINDOW_SECONDS:
				# Double-click. Hand back the command already in flight rather
				# than putting a second identical downlink on the air.
				return {"command": pending.name, "status": "Queued", "sent": False,
				        "reason": "identical command already in flight"}
			frappe.throw(
				f"{pending.name} is already asking {meter.name} to go "
				f"{spec['requested_state']}. Wait for it, or release it."
			)
		frappe.throw(
			f"{meter.name} already reports its valve {meter.valve_reported}, so this "
			f"would change nothing. Nothing was sent."
		)

	if pending:
		# Last write wins, which is what the vendor's own console does -- it
		# accepts commands back to back with no lock. Superseding keeps our
		# record honest instead of pretending the earlier request is still live.
		_close_out(frappe.get_doc("Meter Command", pending.name), "Expired", failure_reason=(
			f"Superseded at {now_datetime():%Y-%m-%d %H:%M:%S} by a request for "
			f"{spec['requested_state']} on the same meter. The SMP had accepted this "
			f"downlink, so the valve may still have acted on it before the newer one "
			f"arrived."
		))

	minutes = float(expiry_hours * 60) if expiry_hours else float(
		frappe.conf.get("tagmeter_valve_timeout_minutes") or VALVE_TIMEOUT_MINUTES
	)
	command = frappe.get_doc({
		"doctype": "Meter Command",
		"water_meter": meter.name,
		"command_type": spec["command_type"],
		"requested_state": spec["requested_state"],
		"status": "Queued",
		"requested_by": frappe.session.user,
		"enqueued_at": now_datetime(),
		"expires_at": add_to_date(now_datetime(), minutes=minutes),
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
def release(water_meter: str, reason: str | None = None) -> dict:
	"""Cancel the in-flight command on a meter and lift its lock.

	The lock exists so two downlinks cannot race, and it lasts until the command
	confirms or expires. Confirmation needs a reading, and this fleet reports
	every 9-21 hours, so a meter can sit uncontrollable for most of a day over a
	command that already did its job.

	This is the escape hatch. It does **not** recall the downlink: if the SMP
	already accepted it, the valve may still move. The next command is therefore
	genuinely ambiguous, which is why the reason is recorded against the
	cancelled command rather than the lock being silently dropped.
	"""
	meter = frappe.get_doc("Water Meter", water_meter)
	pending = _pending(water_meter)
	if not pending:
		frappe.throw(f"{meter.name} has no command in flight, so there is nothing to cancel.")

	name = pending.name
	command = frappe.get_doc("Meter Command", name)
	note = f" Reason given: {reason}" if reason else ""
	_close_out(command, "Expired", failure_reason=(
		f"Lock released by {frappe.session.user} at {now_datetime():%Y-%m-%d %H:%M} before "
		f"the meter confirmed.{note} The SMP had already accepted this downlink, so the "
		f"valve may still act on it."
	))
	# valve_desired is deliberately left alone: the operator's request stood,
	# only the lock is lifted. Clearing it would erase what was asked for.
	meter.db_set("valve_in_flight", None, update_modified=False)
	return {"released": name, "meter": meter.name}


@frappe.whitelist()
def check(water_meter: str) -> dict:
	"""Poll one meter now and report where its valve request stands.

	This is what makes live feedback possible. A command reaches the meter in
	seconds, but its uplink only appears in the SMP a fixed ~2m04s later, so a
	caller that polls this every few seconds sees Queued turn into Confirmed
	within about three minutes rather than waiting on the 4-hourly sweep.

	One meter, one API call. Cheap enough to poll; not cheap enough to poll
	forever, so callers are expected to stop once ``settled`` is true.
	"""
	from upande_tagmeter.sync import poll_meter

	outcome = poll_meter(water_meter)
	meter = frappe.get_doc("Water Meter", water_meter)
	pending = _pending(water_meter)

	status = None
	if pending:
		status = "Queued"
	elif meter.valve_in_flight:
		status = frappe.db.get_value("Meter Command", meter.valve_in_flight, "status")

	return {
		"meter": water_meter,
		"poll": outcome.get("status"),
		"valve_reported": meter.valve_reported,
		"valve_desired": meter.valve_desired,
		"command": pending.name if pending else meter.valve_in_flight,
		"status": status,
		"last_seen": meter.last_seen,
		# The caller stops polling on this rather than guessing an interval.
		"settled": not pending,
	}


EXPIRY_REASON = (
	"The meter never reported the requested state before the command expired. "
	"An actuated meter answers within seconds and its uplink reaches the SMP "
	"about two minutes later, so passing the timeout means the meter is not "
	"reachable on the SMP's network server, or has stopped transmitting."
)


def _expire_overdue() -> list[str]:
	"""Close out anything past its deadline. Shared by the poller and the
	watchdog: a ten-minute timeout applied only once an hour is not a
	ten-minute timeout."""
	expired = []
	for row in frappe.get_all(
		"Meter Command", filters={"status": "Queued"}, fields=["name", "expires_at"],
	):
		if row.expires_at and now_datetime() > row.expires_at:
			_close_out(frappe.get_doc("Meter Command", row.name), "Expired",
			           failure_reason=EXPIRY_REASON)
			expired.append(row.name)
	return expired


def _sync_meter(meter_sn: str, client=None):
	"""Indirection so the poller can be tested without the sync module's
	machinery, and so one import cycle stays broken."""
	from upande_tagmeter.sync import sync_meter

	return sync_meter(meter_sn, client=client)


@frappe.whitelist()
def poll_pending(client=None) -> dict:
	"""Poll every meter that has a command in flight, and nothing else.

	This is what makes a command confirm without a browser open. Measured
	2026-09-11: four commands issued at 16:40 sat Queued for eight minutes while
	two of them already had confirming readings waiting on the SMP -- one from
	six seconds after the command, the other twenty-five. Nothing was looking.

	The browser watcher only covers the row that was clicked and dies on a
	reload. ``sync_fleet`` runs every four hours. ``watchdog`` expires and
	re-sends but never reads. So this fills the gap, and it is deliberately
	scoped: with nothing in flight it makes no API calls at all, which is why it
	can afford to run every couple of minutes.
	"""
	names = frappe.get_all(
		"Meter Command", filters={"status": "Queued"}, pluck="water_meter", distinct=True,
	)
	if not names:
		return {"polled": 0, "confirmed": [], "failures": []}

	client = client or get_client()
	confirmed, failures = [], []
	for name in names:
		try:
			_sync_meter(name, client=client)
		except Exception:
			# One unreachable meter must not strand the others' confirmations.
			frappe.log_error(frappe.get_traceback(), f"TagMeter pending poll failed for {name}")
			failures.append(name)
			continue
		if not _pending(name):
			confirmed.append(name)
	return {"polled": len(names), "confirmed": confirmed, "failures": failures,
	        "expired": _expire_overdue()}


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
			_close_out(command, "Expired", failure_reason=EXPIRY_REASON)
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
