"""Sync behaviour against a stubbed SMP client.

No network. The client is injected, so these tests cover what we *store*, while
tests/ at the app root covers how the vendor's wire behaviour is interpreted.
"""

from datetime import datetime, timedelta, timezone

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from upande_tagmeter import sync
from upande_tagmeter.vendor.errors import Outcome


class FakeClient:
	"""Returns queued (Outcome, data) pairs and records what was asked for."""

	def __init__(self, *results):
		self.results = list(results)
		self.asked = []

	def get_latest_amr(self, meter_sn):
		self.asked.append(meter_sn)
		return self.results.pop(0) if self.results else (Outcome.OK, None)


def amr(device_time, cumulative=0.0, balance=2.0, valve="Open", **kw):
	data = {
		"meter_sn": kw.get("meter_sn", ""), "dev_eui": kw.get("dev_eui"),
		"connection": "DN15", "device_time": device_time,
		"cumulative_flow_m3": cumulative, "remaining_balance_m3": balance,
		"instant_flow_m3h": 0.0, "temperature_c": 21.5, "rssi": -43, "snr": 9.0,
		"valve_status_text": f"00[Valve {valve.lower()};]",
		"alarm_message_text": "02[Empty pipe alarm;]",
		"status_raw": "0002", "valve_state": valve, "status_text_mismatch": 0,
		"alarm_empty_pipe": True, "hourly": [],
	}
	for flag in sync.FLAG_FIELDS:
		data.setdefault(flag, False)
	data["alarm_empty_pipe"] = True
	data.update(kw)
	return data


class TestSync(IntegrationTestCase):
	def _meter(self, meter_sn, profile="Quinto Prepaid"):
		if not frappe.db.exists("Water Meter", meter_sn):
			frappe.get_doc({
				"doctype": "Water Meter", "meter_sn": meter_sn, "meter_profile": profile,
			}).insert()
		return frappe.get_doc("Water Meter", meter_sn)

	# ── happy path ───────────────────────────────────────────────────────────

	def test_a_reading_is_stored_and_denormalised_onto_the_meter(self):
		sn = "68753500170901"
		self._meter(sn)
		when = datetime.now(timezone.utc) - timedelta(hours=1)
		client = FakeClient((Outcome.OK, amr(when, cumulative=12.5, balance=2.001)))

		result = sync.sync_meter(sn, client=client)
		self.assertEqual(result["status"], "ok")

		reading = frappe.get_doc("Meter Reading", result["reading"])
		self.assertEqual(reading.water_meter, sn)
		self.assertEqual(reading.cumulative_flow_m3, 12.5)
		self.assertEqual(reading.remaining_balance_m3, 2.001)
		self.assertEqual(reading.valve_state, "Open")
		self.assertTrue(reading.alarm_empty_pipe)

		meter = frappe.get_doc("Water Meter", sn)
		self.assertEqual(meter.cumulative_flow_m3, 12.5)
		self.assertEqual(meter.valve_reported, "Open")
		self.assertEqual(meter.status, "Active")
		self.assertTrue(meter.online)
		self.assertTrue(meter.vendor_registered)
		self.assertEqual(meter.last_reading, result["reading"])

	def test_first_reading_reports_zero_consumption(self):
		"""A lifetime counter is not one period's consumption."""
		sn = "68753500170902"
		self._meter(sn)
		when = datetime.now(timezone.utc) - timedelta(hours=1)
		client = FakeClient((Outcome.OK, amr(when, cumulative=500.0)))
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(frappe.db.get_value("Meter Reading", result["reading"], "consumption_m3"), 0.0)

	def test_second_reading_reports_the_delta(self):
		sn = "68753500170903"
		self._meter(sn)
		first = datetime.now(timezone.utc) - timedelta(hours=13)
		second = datetime.now(timezone.utc) - timedelta(hours=1)
		client = FakeClient(
			(Outcome.OK, amr(first, cumulative=100.0)),
			(Outcome.OK, amr(second, cumulative=103.25)),
		)
		sync.sync_meter(sn, client=client)
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(
			frappe.db.get_value("Meter Reading", result["reading"], "consumption_m3"), 3.25
		)

	def test_counter_going_backwards_is_not_negative_consumption(self):
		"""Means replacement, reset or an out-of-order record -- never usage."""
		sn = "68753500170904"
		self._meter(sn)
		first = datetime.now(timezone.utc) - timedelta(hours=13)
		second = datetime.now(timezone.utc) - timedelta(hours=1)
		client = FakeClient(
			(Outcome.OK, amr(first, cumulative=100.0)),
			(Outcome.OK, amr(second, cumulative=4.0)),
		)
		sync.sync_meter(sn, client=client)
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(
			frappe.db.get_value("Meter Reading", result["reading"], "consumption_m3"), 0.0
		)

	# ── idempotency ──────────────────────────────────────────────────────────

	def test_the_same_record_polled_twice_is_stored_once(self):
		"""The expected case on most polls: the SMP returns its latest record
		every time, and meters speak far less often than we ask."""
		sn = "68753500170905"
		self._meter(sn)
		when = datetime.now(timezone.utc) - timedelta(hours=2)
		client = FakeClient((Outcome.OK, amr(when)), (Outcome.OK, amr(when)))

		first = sync.sync_meter(sn, client=client)
		second = sync.sync_meter(sn, client=client)
		self.assertEqual(first["status"], "ok")
		self.assertEqual(second["status"], "duplicate")
		self.assertIsNone(second["reading"])
		self.assertEqual(frappe.db.count("Meter Reading", {"water_meter": sn}), 1)

	def test_a_duplicate_poll_still_records_that_we_polled(self):
		sn = "68753500170906"
		self._meter(sn)
		when = datetime.now(timezone.utc) - timedelta(hours=2)
		client = FakeClient((Outcome.OK, amr(when)), (Outcome.OK, amr(when)))
		sync.sync_meter(sn, client=client)
		sync.sync_meter(sn, client=client)
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "last_sync_outcome"), "duplicate")

	def test_an_older_record_does_not_overwrite_current_state(self):
		sn = "68753500170907"
		self._meter(sn)
		newer = datetime.now(timezone.utc) - timedelta(hours=1)
		older = datetime.now(timezone.utc) - timedelta(hours=20)
		client = FakeClient(
			(Outcome.OK, amr(newer, cumulative=50.0)),
			(Outcome.OK, amr(older, cumulative=10.0)),
		)
		sync.sync_meter(sn, client=client)
		sync.sync_meter(sn, client=client)
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "cumulative_flow_m3"), 50.0)

	# ── non-ok outcomes ──────────────────────────────────────────────────────

	def test_unknown_meter_clears_vendor_registered_without_raising(self):
		sn = "68753500170908"
		self._meter(sn)
		client = FakeClient((Outcome.UNKNOWN_METER, None))
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(result["status"], "unknown_meter")
		self.assertFalse(frappe.db.get_value("Water Meter", sn, "vendor_registered"))
		self.assertEqual(frappe.db.count("Meter Reading", {"water_meter": sn}), 0)

	def test_a_known_meter_with_no_amr_record_is_recorded_as_such(self):
		"""Real for this fleet: a 1-byte keepalive produces no AMR record."""
		sn = "68753500170909"
		self._meter(sn)
		client = FakeClient((Outcome.OK, None))
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(result["status"], "no_amr_record")
		self.assertTrue(frappe.db.get_value("Water Meter", sn, "vendor_registered"))
		self.assertEqual(frappe.db.count("Meter Reading", {"water_meter": sn}), 0)

	def test_transport_error_leaves_stored_values_untouched(self):
		sn = "68753500170910"
		self._meter(sn)
		when = datetime.now(timezone.utc) - timedelta(hours=1)
		client = FakeClient((Outcome.OK, amr(when, cumulative=77.0)), (Outcome.TRANSPORT_ERROR, None))
		sync.sync_meter(sn, client=client)
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(result["status"], "transport_error")
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "cumulative_flow_m3"), 77.0)
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "last_sync_outcome"), "transport_error")

	# ── online flag ──────────────────────────────────────────────────────────

	def test_a_stale_reading_does_not_count_as_online(self):
		"""Default window is 30h, because meters report roughly every 12h."""
		sn = "68753500170911"
		self._meter(sn)
		stale = datetime.now(timezone.utc) - timedelta(hours=40)
		client = FakeClient((Outcome.OK, amr(stale)))
		sync.sync_meter(sn, client=client)
		self.assertFalse(frappe.db.get_value("Water Meter", sn, "online"))

	def test_refresh_online_flags_clears_a_meter_that_went_quiet(self):
		"""The reason this pass exists: a silent meter produces no reading, so
		nothing in sync_meter would ever clear its flag."""
		sn = "68753500170912"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, {
			"online": 1, "last_seen": now_datetime() - timedelta(hours=48),
		}, update_modified=False)
		sync.refresh_online_flags()
		self.assertFalse(frappe.db.get_value("Water Meter", sn, "online"))

	def test_refresh_online_flags_marks_a_recent_meter_online(self):
		sn = "68753500170913"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, {
			"online": 0, "last_seen": now_datetime() - timedelta(hours=2),
		}, update_modified=False)
		sync.refresh_online_flags()
		self.assertTrue(frappe.db.get_value("Water Meter", sn, "online"))

	def test_a_meter_that_never_reported_is_not_online(self):
		sn = "68753500170914"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, {"online": 1, "last_seen": None},
		                    update_modified=False)
		sync.refresh_online_flags()
		self.assertFalse(frappe.db.get_value("Water Meter", sn, "online"))

	# ── immutability ─────────────────────────────────────────────────────────

	def test_readings_cannot_be_edited(self):
		sn = "68753500170915"
		self._meter(sn)
		when = datetime.now(timezone.utc) - timedelta(hours=1)
		client = FakeClient((Outcome.OK, amr(when, cumulative=5.0)))
		result = sync.sync_meter(sn, client=client)
		reading = frappe.get_doc("Meter Reading", result["reading"])
		reading.cumulative_flow_m3 = 999.0
		with self.assertRaises(frappe.ValidationError):
			reading.save()

	def test_server_error_is_recorded_against_the_meter(self):
		"""Fifteen Kiwasco serials reproducibly crash the SMP (HTTP 500, empty
		body). The sweep must record that durably rather than treat it as a
		transient blip, so an operator can see which meters the vendor cannot
		answer for."""
		sn = "68753500170916"
		self._meter(sn)
		client = FakeClient((Outcome.SERVER_ERROR, None))
		result = sync.sync_meter(sn, client=client)
		self.assertEqual(result["status"], "server_error")
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "last_sync_outcome"), "server_error")
		self.assertEqual(frappe.db.count("Meter Reading", {"water_meter": sn}), 0)

	def test_server_error_does_not_clear_vendor_registered(self):
		"""We cannot conclude the meter is unknown -- their server never
		answered the question."""
		sn = "68753500170917"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, "vendor_registered", 1, update_modified=False)
		sync.sync_meter(sn, client=FakeClient((Outcome.SERVER_ERROR, None)))
		self.assertTrue(frappe.db.get_value("Water Meter", sn, "vendor_registered"))
