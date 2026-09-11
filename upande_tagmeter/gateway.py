"""Gateway health, polled from the SMP.

The vendor's ``online`` flag is not sufficient on its own. Gateway
``F04CD5FFFE01CF70`` reported ``online: false`` with a ``statTime`` two days
old, and the SMP web console's gateway list showed the same value in a column
for both a live and a dead gateway. Heartbeat age is the signal that does not
lie, so :func:`unhealthy_gateways` requires both.
"""

import json
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from upande_tagmeter.sync import _to_system_naive, get_client
from upande_tagmeter.vendor.errors import Outcome

# A gateway heartbeats far more often than a meter reports, so staleness here
# is measured in hours, not the 30 the meters need.
GATEWAY_STALE_AFTER_HOURS = 6


def stale_cutoff():
	hours = frappe.conf.get("gateway_stale_after_hours") or GATEWAY_STALE_AFTER_HOURS
	return now_datetime() - timedelta(hours=float(hours))


def gateway_health() -> dict[str, str]:
	"""Every gateway's health as one of ``healthy`` / ``unhealthy`` / ``unknown``.

	Three states, not two, because "we have no current evidence" is not the
	same claim as "this gateway is up", and the Network view exists precisely
	for the moments when that difference matters.

	``unknown`` covers a decommissioned unit (down on purpose, not a fault), a
	gateway never polled, one polled recently that has not yet produced a
	heartbeat, and -- the important one -- any gateway whose own last poll has
	itself gone stale.

	That last case is what stops our outage being reported as the network's.
	``last_polled_at`` only advances when a poll actually completes, so rotated
	credentials, an unreachable SMP or a wedged scheduler freeze it. Without
	the guard every heartbeat on the site ages past the cutoff and the whole
	fleet reads ``Gateway Down`` within ``gateway_stale_after_hours`` -- a
	fleet-wide false alarm caused by us, which is the exact trust-the-wrong-
	signal failure this feature was built to eliminate.

	The one asymmetry: a gateway that has *never* produced a heartbeat and
	whose polling has also stopped is called unhealthy rather than unknown.
	There is no prior good state to protect there, and a fleet of working
	gateways all carry heartbeats, so this branch can never raise the
	fleet-wide false alarm above.
	"""
	cutoff = stale_cutoff()
	health = {}
	for row in frappe.get_all(
		"TagMeter Gateway",
		fields=["name", "gateway_status", "online", "last_heartbeat", "last_polled_at"],
	):
		health[row.name] = _health_of(row, cutoff)
	return health


def _health_of(row, cutoff) -> str:
	if row.gateway_status == "Decommissioned":
		return "unknown"
	if not row.last_polled_at:
		# Freshly seeded. Unknown, never down -- otherwise adding a gateway
		# would condemn every meter bound to it before its first poll ran.
		return "unknown"
	if not row.last_heartbeat:
		# Polled, but the SMP has never given us a heartbeat. Allow one full
		# staleness window from the first poll attempt before condemning it, so
		# a first poll that merely errored is not read as an outage.
		return "unhealthy" if row.last_polled_at < cutoff else "unknown"
	if row.last_polled_at < cutoff:
		return "unknown"
	if not row.online or row.last_heartbeat < cutoff:
		return "unhealthy"
	return "healthy"


def unhealthy_gateways() -> list[str]:
	"""Gateways we are confident are down.

	Confidence is the point: anything :func:`gateway_health` calls ``unknown``
	is absent from this list, so it can never condemn the meters bound to it.
	"""
	return sorted(name for name, state in gateway_health().items() if state == "unhealthy")


@frappe.whitelist()
def sync_gateways(client=None, names=None) -> dict:
	"""Poll every gateway. Two API calls, so this can run hourly.

	A failed poll records the outcome and leaves the last known health alone.
	Inferring "down" from our own network blip would mark every bound meter
	``Gateway Down`` for a fault that is not the gateway's.

	``names`` narrows the sweep to specific gateway ids; ``None`` polls all of
	them. Tests need it: without a way to scope the sweep, a test's stubbed
	client is handed the real seeded gateways too, and writes poll state onto
	live rows.
	"""
	client = client or get_client()
	if names is None:
		names = frappe.get_all("TagMeter Gateway", pluck="name", order_by="name asc")
	elif isinstance(names, str):
		# Whitelisted, so a caller over HTTP hands this across as text; without
		# this it would iterate the string one character at a time.
		names = json.loads(names) if names.strip().startswith("[") else names.split(",")
		names = [n.strip() for n in names if n and n.strip()]
	else:
		names = list(names)

	errors = []
	for name in names:
		polled_at = now_datetime()
		try:
			outcome, data = client.get_gateway_status(name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"TagMeter gateway poll failed for {name}")
			errors.append(name)
			continue

		updates = {"last_polled_at": polled_at, "last_poll_outcome": outcome.value}
		if outcome is Outcome.OK and data:
			updates.update({
				"online": 1 if data.get("online") else 0,
				"last_heartbeat": _to_system_naive(data.get("stat_time")),
				"latitude": data.get("latitude"),
				"longitude": data.get("longitude"),
				"altitude": data.get("altitude"),
				"gps_time_sync": 1 if data.get("gps_time_sync") else 0,
				"mqtt_protocol": 1 if data.get("mqtt_protocol") else 0,
			})
		else:
			errors.append(name)
		frappe.db.set_value("TagMeter Gateway", name, updates, update_modified=False)

	# Three buckets, not two. "We have no current evidence" is reported as
	# ``unknown`` rather than folded into ``healthy``, which would state
	# something the data does not support.
	health = gateway_health()
	buckets = {"healthy": [], "unhealthy": [], "unknown": []}
	for name in names:
		buckets[health.get(name, "unknown")].append(name)

	return {"polled": len(names), "errors": errors, **buckets}
