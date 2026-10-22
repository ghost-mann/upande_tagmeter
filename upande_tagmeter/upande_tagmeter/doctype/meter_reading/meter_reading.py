"""One decoded SMP reading. Never edited by hand.

The only writer after insert is sync._refresh_reading, which rewrites a record
from a fresh fetch of that same record from the SMP -- through db.set_value, so
this validate never sees it.
"""

import frappe
from frappe.model.document import Document


class MeterReading(Document):
	def validate(self):
		if not self.is_new():
			# Readings are evidence. Correcting one by hand would destroy the
			# audit trail that lets a top-up or valve change be proven later.
			frappe.throw("Meter Reading is immutable. Re-poll the meter instead of editing a reading.")
