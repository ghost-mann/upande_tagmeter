"""One valve command and its delivery history."""

import frappe
from frappe.model.document import Document


class MeterCommand(Document):
	def before_insert(self):
		# A command saved from the desk's New form skips valve.set_valve: no
		# profile check, no supersede, no enqueue time, no expiry, and the meter
		# is never told it has one in flight. The hourly watchdog then finds it
		# never-sent and fires it unannounced -- MC-2026-00053/54 on KIWASCO,
		# 2026-10-22, both sent at 13:00 and stuck Queued with no deadline.
		# set_valve always stamps enqueued_at, and the form cannot (read-only),
		# so its absence is the tell. Imports carry their own enqueued_at.
		if not self.enqueued_at:
			frappe.throw(
				"Valve commands are sent from the Water Meter form's valve switch or the "
				"/tagmeter dashboard, not created here -- those check the meter has a "
				"valve, supersede anything in flight and set an expiry.",
				title="Use the valve switch",
			)

	def validate(self):
		if self.is_new():
			return
		# Status transitions are driven by valve.py and the watchdog, which have
		# the evidence. Hand-editing a status would let someone mark a command
		# Confirmed with no reading behind it.
		before = self.get_doc_before_save()
		if before and before.status != self.status and frappe.session.user != "Administrator":
			frappe.throw(
				"Meter Command status is set by the delivery and confirmation logic, "
				"not by hand. Use the watchdog, or re-issue the command."
			)
