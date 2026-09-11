"""Registry and denormalised current state for one physical meter."""

import frappe
from frappe.model.document import Document

from upande_tagmeter.vendor.parse import is_valid_meter_sn

# DevEUI byte 6 (of 8) encodes the model -- chars 10:12 of the 16-hex string: 0x19 for the AMR-only meters, 0x20 for the
# Quinto Prepaid ones. A disagreement means a mislabelled device, which is worth
# surfacing at import -- but not worth blocking a save over, since the SMP is
# addressed by serial and would still work.
PROFILE_BYTE = {"AMR": "19", "Quinto Prepaid": "20"}


class WaterMeter(Document):
	def validate(self):
		self.meter_sn = (self.meter_sn or "").strip()
		if not is_valid_meter_sn(self.meter_sn):
			frappe.throw(
				f"Meter Serial must be exactly 14 decimal digits, got {self.meter_sn!r}. "
				"This value is the SMP's meterID; a wrong one addresses another meter "
				"or none at all."
			)
		# A blank label must be NULL rather than "", or the title falls back to
		# an empty string in links and the list view instead of the serial.
		self.meter_label = (self.meter_label or "").strip() or None
		if self.dev_eui:
			self.dev_eui = self.dev_eui.strip().lower()
		self._warn_on_profile_mismatch()

	def _warn_on_profile_mismatch(self):
		expected = PROFILE_BYTE.get(self.meter_profile)
		if not expected or not self.dev_eui or len(self.dev_eui) != 16:
			return
		actual = self.dev_eui[10:12]
		if actual != expected:
			frappe.msgprint(
				f"DevEUI byte 6 is {actual!r} but profile {self.meter_profile!r} expects "
				f"{expected!r}. One of the two is likely wrong.",
				title="Profile / DevEUI mismatch",
				indicator="orange",
			)
