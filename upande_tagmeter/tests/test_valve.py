"""Valve control against a stubbed SMP. No network."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from upande_tagmeter import sync, valve
from upande_tagmeter.vendor.errors import Outcome


class FakeClient:
	"""Replays queued (Outcome, body) pairs for both reads and writes."""

	def __init__(self, *writes, reads=()):
		self.writes = list(writes)
		self.reads = list(reads)
		self.sent = []

	def set_valve(self, meter_sn, action):
		self.sent.append((meter_sn, action))
		return self.writes.pop(0) if self.writes else (
			Outcome.OK, {"code": 200, "message": "Operation success!"}
		)

	def get_latest_amr(self, meter_sn):
		return self.reads.pop(0) if self.reads else (Outcome.OK, None)


OK = (Outcome.OK, {"code": 200, "message": "Operation success!"})
REFUSED = (Outcome.REJECTED, {"code": 200, "message": "Invalid valve control command"})


def amr(device_time, valve_state="Open", cumulative=0.0):
	data = {
		"meter_sn": "", "dev_eui": None, "connection": "DN15", "device_time": device_time,
		"cumulative_flow_m3": cumulative, "remaining_balance_m3": 2.0, "instant_flow_m3h": 0.0,
		"temperature_c": 21.0, "rssi": -45, "snr": 8.0,
		"valve_status_text": f"00[Valve {valve_state.lower()};]",
		"alarm_message_text": "02[Empty pipe alarm;]",
		"status_raw": "0002", "valve_state": valve_state, "status_text_mismatch": 0, "hourly": [],
	}
	for flag in sync.FLAG_FIELDS:
		data.setdefault(flag, False)
	return data


class TestValve(IntegrationTestCase):
	def _meter(self, meter_sn, profile="Quinto Prepaid"):
		if not frappe.db.exists("Water Meter", meter_sn):
			frappe.get_doc({
				"doctype": "Water Meter", "meter_sn": meter_sn, "meter_profile": profile,
			}).insert()
		return frappe.get_doc("Water Meter", meter_sn)

    # ── issuing ─────────────────────────────────────────────────────────────

	def test_a_command_is_queued_not_confirmed(self):
		"""The SMP accepting a request is not the valve moving."""
		sn = "68753500171001"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
		command = frappe.get_doc("Meter Command", res["command"])
		self.assertEqual(command.status, "Queued")
		self.assertEqual(command.requested_state, "Closed")
		self.assertEqual(command.attempts, 1)
		self.assertEqual(command.last_outcome, "ok")
		self.assertEqual(client.sent, [(sn, "Close")])

		meter = frappe.get_doc("Water Meter", sn)
		self.assertEqual(meter.valve_desired, "Closed")
		self.assertEqual(meter.valve_in_flight, command.name)
		self.assertEqual(meter.valve_reported, "Unknown")  # untouched by a request

	def test_amr_meters_have_no_valve_to_control(self):
		sn = "68750000079001"
		self._meter(sn, profile="AMR")
		with self.assertRaises(frappe.ValidationError):
			valve.set_valve(sn, "open")

	def test_a_second_command_is_refused_while_one_is_in_flight(self):
		"""Two competing downlinks make the outcome ambiguous."""
		sn = "68753500171002"
		self._meter(sn)
		client = FakeClient(OK, OK)
		with patch.object(valve, "get_client", return_value=client):
			valve.set_valve(sn, "close")
			with self.assertRaises(frappe.ValidationError):
				valve.set_valve(sn, "open")
		self.assertEqual(len(client.sent), 1)

	def test_an_unknown_action_is_refused(self):
		sn = "68753500171003"
		self._meter(sn)
		with self.assertRaises(frappe.ValidationError):
			valve.set_valve(sn, "reset")

	def test_a_refused_command_fails_immediately_rather_than_waiting(self):
		"""Retrying an identical rejected request is pointless."""
		sn = "68753500171004"
		self._meter(sn)
		client = FakeClient(REFUSED)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "open")
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "status"), "Rejected")
		# in-flight must be cleared, or the meter is permanently blocked
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "valve_in_flight"))

    # ── confirmation ────────────────────────────────────────────────────────

	def test_a_later_reading_showing_the_requested_state_confirms(self):
		sn = "68753500171005"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")

		later = datetime.now(timezone.utc) + timedelta(minutes=5)
		sync.sync_meter(sn, client=FakeClient(reads=[(Outcome.OK, amr(later, valve_state="Closed"))]))

		command = frappe.get_doc("Meter Command", res["command"])
		self.assertEqual(command.status, "Confirmed")
		self.assertTrue(command.confirming_reading)
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "valve_in_flight"))

	def test_a_reading_older_than_the_command_proves_nothing(self):
		"""A reading taken before the command was sent says nothing about it."""
		sn = "68753500171006"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")

		earlier = datetime.now(timezone.utc) - timedelta(hours=3)
		sync.sync_meter(sn, client=FakeClient(reads=[(Outcome.OK, amr(earlier, valve_state="Closed"))]))
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "status"), "Queued")

	def test_a_reading_showing_the_opposite_state_is_not_a_failure(self):
		"""On Class B the downlink may simply not have arrived yet."""
		sn = "68753500171007"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")

		later = datetime.now(timezone.utc) + timedelta(minutes=5)
		sync.sync_meter(sn, client=FakeClient(reads=[(Outcome.OK, amr(later, valve_state="Open"))]))
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "status"), "Queued")

    # ── watchdog ────────────────────────────────────────────────────────────

	def test_the_watchdog_expires_an_overdue_command(self):
		sn = "68753500171008"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
		frappe.db.set_value("Meter Command", res["command"], "expires_at",
		                    add_to_date(now_datetime(), hours=-1), update_modified=False)

		valve.watchdog()
		command = frappe.get_doc("Meter Command", res["command"])
		self.assertEqual(command.status, "Expired")
		self.assertIn("never reported", command.failure_reason)
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "valve_in_flight"))

	def test_the_watchdog_does_not_resend_a_command_the_smp_accepted(self):
		"""It is sitting in *their* queue; sending again stacks a duplicate."""
		sn = "68753500171009"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			valve.set_valve(sn, "close")
		with patch.object(valve, "get_client", return_value=client):
			result = valve.watchdog()
		self.assertEqual(result["resent"], 0)
		self.assertEqual(len(client.sent), 1)

	def test_the_watchdog_resends_one_the_smp_never_accepted(self):
		sn = "68753500171010"
		self._meter(sn)
		client = FakeClient((Outcome.TRANSPORT_ERROR, None), OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
			self.assertEqual(
				frappe.db.get_value("Meter Command", res["command"], "last_outcome"),
				"transport_error",
			)
			valve.watchdog()
		self.assertEqual(len(client.sent), 2)
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "last_outcome"), "ok")

	def test_the_watchdog_gives_up_after_max_attempts(self):
		sn = "68753500171011"
		self._meter(sn)
		client = FakeClient(*[(Outcome.TRANSPORT_ERROR, None)] * 6)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
			for _ in range(5):
				valve.watchdog()
		command = frappe.get_doc("Meter Command", res["command"])
		self.assertEqual(command.status, "Failed")
		self.assertIn("never accepted", command.failure_reason)
		self.assertLessEqual(command.attempts, command.max_attempts)

    # ── reporting ───────────────────────────────────────────────────────────

	def test_reconciliation_lists_meters_not_doing_what_was_asked(self):
		sn = "68753500171012"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
		frappe.db.set_value("Water Meter", sn, "valve_reported", "Open", update_modified=False)

		rows = {r["name"]: r for r in valve.reconciliation_report()}
		self.assertIn(sn, rows)
		self.assertEqual(rows[sn]["valve_desired"], "Closed")
		self.assertEqual(rows[sn]["valve_reported"], "Open")
		# in-flight distinguishes "waiting" from "not obeying"
		self.assertEqual(rows[sn]["valve_in_flight"], res["command"])
