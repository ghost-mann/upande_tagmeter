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

	def test_an_opposite_command_supersedes_the_one_in_flight(self):
		"""The vendor's console accepts commands back to back with no lock, and
		its history holds two confirmations ten seconds apart. Last write wins."""
		sn = "68753500171002"
		self._meter(sn)
		client = FakeClient(OK, OK)
		with patch.object(valve, "get_client", return_value=client):
			first = valve.set_valve(sn, "close")
			second = valve.set_valve(sn, "open")

		self.assertEqual(len(client.sent), 2, "both downlinks must reach the SMP")
		self.assertEqual(client.sent, [(sn, "Close"), (sn, "Open")])
		superseded = frappe.get_doc("Meter Command", first["command"])
		self.assertEqual(superseded.status, "Expired")
		self.assertIn("Superseded", superseded.failure_reason)
		self.assertEqual(
			frappe.db.get_value("Meter Command", second["command"], "status"), "Queued")
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "valve_in_flight"),
		                 second["command"])
		valve.release(sn)

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

	# ── no-op guard ──────────────────────────────────────────────────────────

	def test_asking_for_the_state_the_valve_already_reports_is_refused(self):
		"""A no-op still takes the meter out of service for as long as
		confirmation takes.

		Measured 2026-09-11: two of three locked meters had been asked to Open
		while already reporting Open. Nothing could ever change, and the lock
		would have held until the next uplink -- 20.8h away on one of them.
		"""
		sn = "68753500171020"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, "valve_reported", "Open", update_modified=False)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			with self.assertRaises(frappe.ValidationError):
				valve.set_valve(sn, "open")
		self.assertEqual(client.sent, [], "nothing may reach the SMP")
		self.assertEqual(frappe.db.count("Meter Command", {"water_meter": sn}), 0)
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "valve_in_flight"))

	def test_an_unknown_reported_state_never_blocks_a_command(self):
		"""Unknown is the default on a meter that has never reported a valve
		state. Treating it as a no-op would lock out every new meter."""
		sn = "68753500171021"
		self._meter(sn)
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "valve_reported"), "Unknown")
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "open")
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "status"), "Queued")
		valve.release(sn)  # the watchdog tests sweep every Queued row; do not leak one

	def test_the_opposite_state_is_still_allowed(self):
		sn = "68753500171022"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, "valve_reported", "Closed", update_modified=False)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "open")
		self.assertEqual(client.sent, [(sn, "Open")])
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "status"), "Queued")
		valve.release(sn)  # the watchdog tests sweep every Queued row; do not leak one

	# ── releasing a stuck lock ───────────────────────────────────────────────

	def test_releasing_cancels_the_command_and_frees_the_meter(self):
		sn = "68753500171023"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
		out = valve.release(sn, reason="meter will not report for 20h")

		self.assertEqual(out["released"], res["command"])
		command = frappe.get_doc("Meter Command", res["command"])
		self.assertEqual(command.status, "Expired")
		self.assertIn("20h", command.failure_reason)
		self.assertIn(frappe.session.user, command.failure_reason)
		self.assertIsNone(frappe.db.get_value("Water Meter", sn, "valve_in_flight"))

	def test_releasing_a_meter_with_nothing_in_flight_is_refused(self):
		sn = "68753500171024"
		self._meter(sn)
		with self.assertRaises(frappe.ValidationError):
			valve.release(sn)

	def test_releasing_leaves_the_desired_state_as_the_operators_intent(self):
		"""The request stood; only the lock is lifted. Clearing intent too would
		erase what the operator asked for."""
		sn = "68753500171025"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			valve.set_valve(sn, "close")
		valve.release(sn)
		self.assertEqual(frappe.db.get_value("Water Meter", sn, "valve_desired"), "Closed")

	# ── rapid toggling ───────────────────────────────────────────────────────

	def test_a_double_click_returns_the_same_command_not_a_second_downlink(self):
		"""Identical intent inside the duplicate window is a slipped finger, not
		a second request. Two identical downlinks would be pure air-time."""
		sn = "68753500171030"
		self._meter(sn)
		client = FakeClient(OK, OK)
		with patch.object(valve, "get_client", return_value=client):
			first = valve.set_valve(sn, "close")
			again = valve.set_valve(sn, "close")

		self.assertEqual(again["command"], first["command"])
		self.assertFalse(again["sent"])
		self.assertEqual(len(client.sent), 1, "the second click must not transmit")
		valve.release(sn)

	def test_open_close_open_is_not_blocked_by_the_stale_reported_state(self):
		"""valve_reported lags a command by ~2 minutes. Judging a new request
		against it would refuse the third click of open -> close -> open."""
		sn = "68753500171031"
		self._meter(sn)
		frappe.db.set_value("Water Meter", sn, "valve_reported", "Open", update_modified=False)
		client = FakeClient(OK, OK)
		with patch.object(valve, "get_client", return_value=client):
			valve.set_valve(sn, "close")
			# Reported is still "Open" here -- only the pending request says Closed.
			res = valve.set_valve(sn, "open")

		self.assertEqual(client.sent, [(sn, "Close"), (sn, "Open")])
		self.assertEqual(frappe.db.get_value("Meter Command", res["command"], "status"), "Queued")
		valve.release(sn)

	def test_the_expiry_is_minutes_not_a_day(self):
		sn = "68753500171032"
		self._meter(sn)
		client = FakeClient(OK)
		with patch.object(valve, "get_client", return_value=client):
			res = valve.set_valve(sn, "close")
		command = frappe.get_doc("Meter Command", res["command"])
		window = (command.expires_at - command.enqueued_at).total_seconds() / 60
		self.assertAlmostEqual(window, valve.VALVE_TIMEOUT_MINUTES, delta=1)
		self.assertLess(window, 60, "a 26-hour expiry was the original defect")
		valve.release(sn)
