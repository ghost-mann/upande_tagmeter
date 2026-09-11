"""One valve command and its delivery history."""

import frappe
from frappe.model.document import Document


class MeterCommand(Document):
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
