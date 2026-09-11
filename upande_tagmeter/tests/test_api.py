"""Dashboard API. No network: readings are created directly."""

import json

from datetime import timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from upande_tagmeter import api


class TestFlowSeries(IntegrationTestCase):
	def _meter(self, sn, profile="Quinto Prepaid"):
		if not frappe.db.exists("Water Meter", sn):
			frappe.get_doc({"doctype": "Water Meter", "meter_sn": sn,
			                "meter_profile": profile}).insert()
		return sn

	def _reading(self, sn, when, use=0.0, cum=0.0, hourly=None):
		import json
		frappe.get_doc({
			"doctype": "Meter Reading", "water_meter": sn, "device_time": when,
			"received_at": now_datetime(), "dedupe_key": f"{sn}:{when.isoformat()}",
			"consumption_m3": use, "cumulative_flow_m3": cum,
			"hourly_data": json.dumps(hourly or []),
		}).insert(ignore_permissions=True)

	def test_unknown_meter_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			api.flow_series("99999999999999")

	def test_totals_sum_consumption_not_counter_differences(self):
		"""Summing consumption_m3 survives a counter reset, where subtracting
		two lifetime readings would produce a negative."""
		sn = self._meter("68753500172001")
		now = now_datetime()
		self._reading(sn, now - timedelta(hours=2), use=1.5, cum=900.0)
		self._reading(sn, now - timedelta(hours=1), use=2.5, cum=0.0)  # counter reset
		out = api.flow_series(sn)
		self.assertEqual(out["totals"]["day"], 4.0)
		self.assertTrue(out["has_flow"])

	def test_windows_are_trailing_and_nested(self):
		sn = self._meter("68753500172002")
		now = now_datetime()
		self._reading(sn, now - timedelta(hours=3), use=1.0)
		self._reading(sn, now - timedelta(days=3), use=2.0)
		self._reading(sn, now - timedelta(days=20), use=4.0)
		out = api.flow_series(sn)
		self.assertEqual(out["totals"]["day"], 1.0)
		self.assertEqual(out["totals"]["week"], 3.0)
		self.assertEqual(out["totals"]["month"], 7.0)

	def test_hourly_comes_from_the_newest_reading_in_range(self):
		sn = self._meter("68753500172003")
		now = now_datetime()
		self._reading(sn, now - timedelta(hours=5),
		              hourly=[{"timestamp": "2026-09-01T01:00:00+00:00", "delta": 9}])
		self._reading(sn, now - timedelta(hours=1),
		              hourly=[{"timestamp": "2026-09-02T01:00:00+00:00", "delta": 3},
		                      {"timestamp": "2026-09-02T02:00:00+00:00", "delta": 4}])
		out = api.flow_series(sn)
		self.assertEqual([h["v"] for h in out["hourly"]], [3, 4])

	def test_a_dry_meter_reports_no_flow_rather_than_zeros(self):
		"""The dashboard keys its empty state on this, so that it can say so in
		words instead of drawing a flat line at zero."""
		sn = self._meter("68753500172004")
		self._reading(sn, now_datetime() - timedelta(hours=1), use=0.0, cum=0.0,
		              hourly=[{"timestamp": "2026-09-02T01:00:00+00:00", "delta": 0}])
		out = api.flow_series(sn)
		self.assertFalse(out["has_flow"])
		self.assertEqual(out["totals"]["day"], 0.0)
		self.assertEqual(len(out["hourly"]), 1)

	def test_range_filters_readings(self):
		sn = self._meter("68753500172005")
		now = now_datetime()
		self._reading(sn, now - timedelta(days=10), use=5.0)
		self._reading(sn, now - timedelta(hours=2), use=1.0)
		# The browser sends strings, and frappe.whitelist enforces the
		# annotation, so the test calls it the same way the page does.
		out = api.flow_series(
			sn,
			start=str(add_to_date(now, days=-1)),
			end=str(now),
		)
		self.assertEqual(len(out["readings"]), 1)
		self.assertEqual(out["totals"]["range"], 1.0)

	def test_malformed_hourly_json_does_not_break_the_call(self):
		sn = self._meter("68753500172006")
		self._reading(sn, now_datetime() - timedelta(hours=1))
		frappe.db.set_value("Meter Reading",
		                    {"water_meter": sn}, "hourly_data", "not json",
		                    update_modified=False)
		self.assertEqual(api.flow_series(sn)["hourly"], [])


class TestRename(IntegrationTestCase):
	def _meter(self, sn, **kw):
		if not frappe.db.exists("Water Meter", sn):
			frappe.get_doc({"doctype": "Water Meter", "meter_sn": sn,
			                "meter_profile": "AMR", **kw}).insert()
		return sn

	def test_naming_does_not_rename_the_document(self):
		"""The serial is the SMP's meterID and the key every reading joins on.
		If a rename moved the document, the next sweep would create a second
		meter and the history would split in two."""
		sn = self._meter("68750000079101")
		api.rename_meter(sn, label="House 12 · Block A")
		self.assertTrue(frappe.db.exists("Water Meter", sn))
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "meter_label"), "House 12 · Block A")
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "meter_sn"), sn)

	def test_display_falls_back_to_the_serial(self):
		sn = self._meter("68750000079102")
		self.assertEqual(api.rename_meter(sn, label="")["display"], sn)

	def test_blank_label_is_stored_as_null_not_empty_string(self):
		"""An empty string would make the title field render blank in links
		and the list view instead of falling back to the serial."""
		sn = self._meter("68750000079103", meter_label="Temporary")
		api.rename_meter(sn, label="   ")
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "meter_label"))

	def test_none_leaves_a_field_alone_but_empty_clears_it(self):
		sn = self._meter("68750000079104", meter_label="Keep me", zone="Block C")
		api.rename_meter(sn, site="Kiwasco")          # label and zone untouched
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "meter_label"), "Keep me")
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "zone"), "Block C")
		api.rename_meter(sn, zone="")
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "zone"))

	def test_an_overlong_name_is_refused(self):
		sn = self._meter("68750000079105")
		with self.assertRaises(frappe.ValidationError):
			api.rename_meter(sn, label="x" * 200)

	def test_unknown_meter_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			api.rename_meter("99999999999999", label="nope")

	def test_a_batch_applies_the_good_rows_and_reports_the_bad(self):
		"""Refusing the whole batch would throw away an operator's typing."""
		a = self._meter("68750000079106")
		b = self._meter("68750000079107")
		out = api.rename_many([
			{"meter_sn": a, "label": "Standpipe 4", "zone": "Block B"},
			{"meter_sn": "99999999999999", "label": "ghost"},
			{"meter_sn": b, "label": "House 7"},
		])
		self.assertEqual(len(out["saved"]), 2)
		self.assertEqual(len(out["failed"]), 1)
		self.assertEqual(out["failed"][0]["meter_sn"], "99999999999999")
		self.assertEqual(frappe.db.get_value("Water Meter", a, "meter_label"), "Standpipe 4")
		self.assertEqual(frappe.db.get_value("Water Meter", a, "zone"), "Block B")
		self.assertEqual(frappe.db.get_value("Water Meter", b, "meter_label"), "House 7")

	def test_a_batch_accepts_a_json_string(self):
		"""frappe.whitelist hands POSTed lists through as strings."""
		sn = self._meter("68750000079108")
		out = api.rename_many(json.dumps([{"meter_sn": sn, "label": "Tap 9"}]))
		self.assertEqual(len(out["saved"]), 1)
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "meter_label"), "Tap 9")


from upande_tagmeter.www import tagmeter as dashboard


class TestDashboardPayload(IntegrationTestCase):
	def test_payload_carries_gateways_and_link_state(self):
		payload = dashboard.build_payload()
		self.assertIn("gateways", payload)
		self.assertIn("gateways_down", payload["kpi"])
		self.assertIn("meters_behind_down_gateway", payload["kpi"])
		if payload["meters"]:
			self.assertIn("link_state", payload["meters"][0])
			self.assertIn("gateway", payload["meters"][0])

	def test_an_unhealthy_gateway_is_reported_unhealthy(self):
		gid = "TESTGWDASH000001"
		if not frappe.db.exists("TagMeter Gateway", gid):
			frappe.get_doc({
				"doctype": "TagMeter Gateway", "gateway_id": gid, "label": "Dash GW",
			}).insert()
		frappe.db.set_value("TagMeter Gateway", gid, {
			"online": 1,
			"last_heartbeat": now_datetime() - timedelta(hours=48),
			"last_polled_at": now_datetime(),
		}, update_modified=False)

		payload = dashboard.build_payload()
		row = next(g for g in payload["gateways"] if g["id"] == gid)
		self.assertFalse(row["healthy"], "a 48h-old heartbeat is not healthy")
		self.assertGreaterEqual(payload["kpi"]["gateways_down"], 1)
