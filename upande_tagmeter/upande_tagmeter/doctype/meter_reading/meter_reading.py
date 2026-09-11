"""One decoded SMP reading. Immutable once written."""

import frappe
from frappe.model.document import Document


class MeterReading(Document):
	def validate(self):
		if not self.is_new():
			# Readings are evidence. Correcting one by hand would destroy the
			# audit trail that lets a top-up or valve change be proven later.
			frappe.throw("Meter Reading is immutable. Re-poll the meter instead of editing a reading.")
