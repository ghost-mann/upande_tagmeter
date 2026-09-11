"""Gateway sync against a stubbed SMP client. No network."""

from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime


class TestGatewayDoctype(IntegrationTestCase):
	def test_a_gateway_is_named_by_its_id(self):
		gid = "TESTGW0000000001"
		if frappe.db.exists("TagMeter Gateway", gid):
			frappe.delete_doc("TagMeter Gateway", gid, force=1)
		doc = frappe.get_doc({
			"doctype": "TagMeter Gateway", "gateway_id": gid, "label": "Test GW",
		}).insert()
		self.assertEqual(doc.name, gid)
		self.assertEqual(int(doc.online or 0), 0)
