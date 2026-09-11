"""Gateway sync against a stubbed SMP client. No network."""

from datetime import datetime, timedelta, timezone

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


from upande_tagmeter import gateway
from upande_tagmeter.vendor.errors import Outcome


class FakeGatewayClient:
	"""Returns queued (Outcome, parsed) pairs and records what was asked for."""

	def __init__(self, *results):
		self.results = list(results)
		self.asked = []

	def get_gateway_status(self, gateway_id):
		self.asked.append(gateway_id)
		return self.results.pop(0) if self.results else (Outcome.OK, None)


def _status(gid, online=True, stat_time=None, **kw):
	data = {
		"gateway_id": gid, "online": online, "mqtt_protocol": True,
		"latitude": -1.297, "longitude": 36.777, "altitude": 1814.0,
		"gps_time_sync": True, "stat_time": stat_time, "reported_timezone": "Europe/Amsterdam",
	}
	data.update(kw)
	return data


class TestGatewaySync(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		# IntegrationTestCase only rolls back at class teardown, not per test, so
		# a gateway left behind by an earlier method in this class is still in
		# the table here, and the doctype also holds the two real seeded
		# gateways on this site.
		#
		# Two separate guards, both needed. This delete is scoped to our own
		# TESTGW... ids -- never an unscoped wipe of a table that also holds
		# live rows. And every sync_gateways() call below passes ``names``, so
		# the sweep is confined to the gid under test: the fake client answers
		# positionally, so an unscoped sweep would hand the queued response to
		# whichever id sorts first (``0C4EC0...``, a real gateway) and would
		# write poll state onto live rows on the way past.
		frappe.db.delete("TagMeter Gateway", {"name": ("like", "TESTGW%")})

	def _gw(self, gid, **values):
		if frappe.db.exists("TagMeter Gateway", gid):
			frappe.delete_doc("TagMeter Gateway", gid, force=1)
		doc = frappe.get_doc({
			"doctype": "TagMeter Gateway", "gateway_id": gid, "label": gid, "site": "Kiwasco",
		}).insert()
		if values:
			frappe.db.set_value("TagMeter Gateway", gid, values, update_modified=False)
		return doc

	def test_sync_persists_health_and_position(self):
		gid = "TESTGW0000000010"
		self._gw(gid)
		# CORRECTED: sync._to_system_naive() expects an AWARE UTC datetime, the
		# same shape parse_gateway_status actually returns. now_datetime() is
		# naive system-local and would silently drift the stored heartbeat ~3h
		# into the future (Africa/Nairobi is UTC+3) -- see
		# test_stat_time_is_stored_converted_from_utc_to_system_local below.
		fresh = datetime.now(timezone.utc).replace(microsecond=0)
		client = FakeGatewayClient((Outcome.OK, _status(gid, online=True, stat_time=fresh)))
		result = gateway.sync_gateways(client=client, names=[gid])

		self.assertEqual(client.asked, [gid])
		row = frappe.db.get_value(
			"TagMeter Gateway", gid,
			["online", "last_heartbeat", "altitude", "last_poll_outcome"], as_dict=True,
		)
		self.assertEqual(int(row.online), 1)
		self.assertEqual(row.altitude, 1814.0)
		self.assertEqual(row.last_poll_outcome, "ok")
		self.assertIn(gid, result["healthy"])

	def test_a_failed_poll_leaves_prior_state_intact(self):
		"""A blip on our side is not a gateway outage."""
		gid = "TESTGW0000000011"
		fresh = now_datetime() - timedelta(minutes=5)
		self._gw(gid, online=1, last_heartbeat=fresh, last_polled_at=fresh)
		client = FakeGatewayClient((Outcome.SERVER_ERROR, None))
		gateway.sync_gateways(client=client, names=[gid])

		row = frappe.db.get_value(
			"TagMeter Gateway", gid, ["online", "last_poll_outcome"], as_dict=True
		)
		self.assertEqual(int(row.online), 1, "a failed poll must not mark a gateway down")
		self.assertEqual(row.last_poll_outcome, "server_error")

	def test_a_gateway_that_says_online_with_a_stale_heartbeat_is_unhealthy(self):
		"""F04CD5FFFE01CF70 is exactly this case."""
		gid = "TESTGW0000000012"
		self._gw(gid, online=1, last_heartbeat=now_datetime() - timedelta(hours=48),
		         last_polled_at=now_datetime())
		self.assertIn(gid, gateway.unhealthy_gateways())

	def test_a_never_polled_gateway_is_not_called_down(self):
		"""A freshly seeded gateway must not condemn the meters bound to it."""
		gid = "TESTGW0000000013"
		self._gw(gid)
		self.assertNotIn(gid, gateway.unhealthy_gateways())

	def test_a_scoped_sweep_never_touches_a_real_gateway_row(self):
		"""``names`` is the guard, not class-teardown rollback.

		These tests run against a live site holding two real gateways. Without
		``names`` the sweep writes ``last_polled_at`` and ``last_poll_outcome``
		onto those rows and relies on rollback to undo it; with it, they are
		never read or written at all.
		"""
		gid = "TESTGW0000000016"
		self._gw(gid)
		before = {
			r.name: (r.last_polled_at, r.last_poll_outcome)
			for r in frappe.get_all(
				"TagMeter Gateway",
				filters={"name": ("not like", "TESTGW%")},
				fields=["name", "last_polled_at", "last_poll_outcome"],
			)
		}
		self.assertTrue(before, "expected the real seeded gateways to be present")

		client = FakeGatewayClient((Outcome.OK, _status(gid, online=True, stat_time=None)))
		gateway.sync_gateways(client=client, names=[gid])

		self.assertEqual(client.asked, [gid])
		after = {
			r.name: (r.last_polled_at, r.last_poll_outcome)
			for r in frappe.get_all(
				"TagMeter Gateway",
				filters={"name": ("not like", "TESTGW%")},
				fields=["name", "last_polled_at", "last_poll_outcome"],
			)
		}
		self.assertEqual(before, after, "a scoped sweep must not write to a real gateway row")

	def test_a_gateway_whose_first_poll_just_failed_is_not_called_down(self):
		"""``last_polled_at`` is written on the failure path too.

		So the "never polled" guard alone does not protect a freshly seeded
		gateway whose very first poll errored: it has a fresh ``last_polled_at``
		and still no ``last_heartbeat``. One failed poll is not an outage.
		"""
		gid = "TESTGW0000000017"
		self._gw(gid)
		client = FakeGatewayClient((Outcome.SERVER_ERROR, None))
		result = gateway.sync_gateways(client=client, names=[gid])

		self.assertNotIn(gid, gateway.unhealthy_gateways())
		self.assertIn(gid, result["unknown"])

	def test_a_gateway_with_no_heartbeat_and_a_stale_poll_is_down(self):
		"""The grace above is one staleness window, not forever."""
		gid = "TESTGW0000000018"
		self._gw(gid, last_polled_at=now_datetime() - timedelta(hours=48),
		         last_poll_outcome="server_error")
		self.assertIn(gid, gateway.unhealthy_gateways())

	def test_our_own_polling_outage_does_not_condemn_the_whole_fleet(self):
		"""Rotated credentials must not read as every gateway going down.

		If polling stops, every heartbeat on the site ages past the cutoff. A
		rule that looked only at heartbeat age would call the entire fleet down
		for a fault that is ours, which is the exact mistake this feature
		exists to stop.
		"""
		gid = "TESTGW0000000019"
		stopped = now_datetime() - timedelta(hours=48)
		self._gw(gid, online=1, last_heartbeat=stopped, last_polled_at=stopped)
		self.assertNotIn(gid, gateway.unhealthy_gateways())
		self.assertEqual(gateway.gateway_health()[gid], "unknown")

	def test_health_is_three_states_not_two(self):
		"""Never polled and decommissioned are unknown, never healthy."""
		never = self._gw("TESTGW0000000020").name
		dead = self._gw("TESTGW0000000021", gateway_status="Decommissioned", online=1,
		                last_heartbeat=now_datetime(), last_polled_at=now_datetime()).name
		live = self._gw("TESTGW0000000022", online=1,
		                last_heartbeat=now_datetime() - timedelta(minutes=5),
		                last_polled_at=now_datetime()).name
		health = gateway.gateway_health()
		self.assertEqual(health[never], "unknown")
		self.assertEqual(health[dead], "unknown")
		self.assertEqual(health[live], "healthy")

	def test_a_healthy_gateway_is_absent_from_the_unhealthy_list(self):
		gid = "TESTGW0000000014"
		self._gw(gid, online=1, last_heartbeat=now_datetime() - timedelta(minutes=10),
		         last_polled_at=now_datetime())
		self.assertNotIn(gid, gateway.unhealthy_gateways())

	def test_stat_time_is_stored_converted_from_utc_to_system_local(self):
		"""Guards the brief's bug: stat_time is aware UTC, stored as system-local.

		Africa/Nairobi is UTC+3 with no DST, so the naive value landed in
		``last_heartbeat`` must be exactly three hours ahead of the aware UTC
		instant the fake vendor call returned -- never the raw UTC value.
		"""
		gid = "TESTGW0000000015"
		self._gw(gid)
		stat_time = datetime(2026, 9, 10, 6, 30, 0, tzinfo=timezone.utc)
		client = FakeGatewayClient((Outcome.OK, _status(gid, online=True, stat_time=stat_time)))
		gateway.sync_gateways(client=client, names=[gid])

		last_heartbeat = frappe.db.get_value("TagMeter Gateway", gid, "last_heartbeat")
		self.assertEqual(last_heartbeat, datetime(2026, 9, 10, 9, 30, 0))


from upande_tagmeter.setup import import_gateways


class TestGatewaySeeding(IntegrationTestCase):
	def test_the_shipped_tsv_seeds_both_gateways(self):
		result = import_gateways.run(dry_run=True)
		self.assertEqual(result["problems"], [])
		self.assertEqual(result["created"] + result["updated"], 2)

	def test_run_is_idempotent(self):
		first = import_gateways.run()
		second = import_gateways.run()
		self.assertEqual(second["created"], 0)
		self.assertEqual(second["updated"], first["created"] + first["updated"])

	def test_a_meter_can_be_bound_to_a_gateway(self):
		import_gateways.run()
		sn = "68753500170902"
		if not frappe.db.exists("Water Meter", sn):
			frappe.get_doc({
				"doctype": "Water Meter", "meter_sn": sn, "meter_profile": "Quinto Prepaid",
				"gateway": "0C4EC0FFFE00E97F",
			}).insert()
		self.assertEqual(
			frappe.db.get_value("Water Meter", sn, "gateway"), "0C4EC0FFFE00E97F"
		)
