"""Give commands created outside set_valve the fields it would have set.

Meter Commands saved from the desk's New form (MC-2026-00053/54 on KIWASCO,
2026-10-22) carry no enqueued_at or expires_at and were never linked to their
meter, so the form showed a plain "Closed" while an open was in flight. The
form can no longer create them; this repairs the ones already saved.

The send time is taken from last_attempt_at when there is one -- the watchdog
is what actually sent them -- otherwise from creation. Nothing is sent.
"""

import frappe
from frappe.utils import add_to_date

from upande_tagmeter.valve import timeout_minutes


def execute():
	rows = frappe.get_all(
		"Meter Command",
		filters={"status": "Queued", "enqueued_at": ["is", "not set"]},
		fields=["name", "water_meter", "requested_state", "creation", "last_attempt_at"],
		order_by="creation asc",
	)
	for row in rows:
		sent = row.last_attempt_at or row.creation
		frappe.db.set_value("Meter Command", row.name, {
			"enqueued_at": sent,
			"expires_at": add_to_date(sent, minutes=timeout_minutes(row.requested_state)),
		}, update_modified=False)
		# Oldest first, so the newest command ends up as the meter's in-flight one.
		frappe.db.set_value("Water Meter", row.water_meter, {
			"valve_desired": row.requested_state,
			"valve_in_flight": row.name,
		}, update_modified=False)
