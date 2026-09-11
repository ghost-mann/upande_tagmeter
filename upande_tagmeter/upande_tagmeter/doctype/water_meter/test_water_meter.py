import frappe
from frappe.tests import IntegrationTestCase


class TestWaterMeter(IntegrationTestCase):
	"""Registry validation.

	IntegrationTestCase rolls back at *class* cleanup, not per test method, so
	tests in one class share a transaction and see each other's inserts. Each
	test therefore uses its own serial rather than assuming isolation.
	"""

	# Synthetic 99-prefixed serials only. The real Kiwasco fleet occupies
	# 68750000076884-76973 and 68753500170868-170877, and import_meters puts
	# all hundred in the database -- a test using one would collide with it.
	def _make(self, meter_sn, **kw):
		return frappe.get_doc({
			"doctype": "Water Meter", "meter_sn": meter_sn,
			"meter_profile": kw.pop("meter_profile", "AMR"), **kw,
		})

	def test_serial_must_be_fourteen_digits(self):
		for bad in ("9900000000000", "990000000000010", "9900000000000a", ""):
			with self.assertRaises(frappe.ValidationError):
				self._make(bad).insert()

	def test_valid_serial_becomes_the_document_name(self):
		doc = self._make("99000000000001").insert()
		self.assertEqual(doc.name, "99000000000001")

	def test_dev_eui_is_lowercased(self):
		doc = self._make("99000000000002", dev_eui="8CF9572000191B85").insert()
		self.assertEqual(doc.dev_eui, "8cf9572000191b85")

	def test_defaults_are_conservative(self):
		doc = self._make("99000000000003").insert()
		self.assertEqual(doc.status, "Never Seen")
		self.assertEqual(doc.valve_reported, "Unknown")
		self.assertFalse(doc.online)
		self.assertFalse(doc.vendor_registered)

	def test_duplicate_serial_is_rejected(self):
		self._make("99000000000004").insert()
		with self.assertRaises(frappe.exceptions.DuplicateEntryError):
			self._make("99000000000004").insert()

	def test_profile_byte_mismatch_warns_but_saves(self):
		"""Byte 6 is 0x19 for AMR and 0x20 for Quinto -- chars 10:12.

		A mismatch means a mislabelled device, worth surfacing but not worth
		blocking: the SMP is addressed by serial and would still work.
		"""
		doc = self._make("99000000000005", dev_eui="8cf957200020dafe", meter_profile="AMR")
		doc.insert()
		self.assertEqual(doc.name, "99000000000005")

	def test_matching_profile_byte_is_accepted(self):
		doc = self._make("99000000000006", dev_eui="8cf957200020dafe",
		                 meter_profile="Quinto Prepaid").insert()
		self.assertEqual(doc.dev_eui[10:12], "20")
