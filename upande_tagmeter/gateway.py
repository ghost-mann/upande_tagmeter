"""Gateway health, polled from the SMP.

The vendor's ``online`` flag is not sufficient on its own. Gateway
``F04CD5FFFE01CF70`` reported ``online: false`` with a ``statTime`` two days
old, and the SMP web console's gateway list showed the same value in a column
for both a live and a dead gateway. Heartbeat age is the signal that does not
lie, so :func:`unhealthy_gateways` requires both.
"""

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


def unhealthy_gateways() -> list[str]:
	"""Gateways we are confident are down.

	A gateway that has never been polled is absent from this list: it is
	*unknown*, not down, and must not condemn the meters bound to it. Same for
	a decommissioned unit, which is down on purpose.
	"""
	cutoff = stale_cutoff()
	out = []
	for row in frappe.get_all(
		"TagMeter Gateway",
		filters={"gateway_status": ("!=", "Decommissioned")},
		fields=["name", "online", "last_heartbeat", "last_polled_at"],
	):
		if not row.last_polled_at:
			continue
		if not row.online or not row.last_heartbeat or row.last_heartbeat < cutoff:
			out.append(row.name)
	return out


@frappe.whitelist()
def sync_gateways(client=None) -> dict:
	"""Poll every gateway. Two API calls, so this can run hourly.

	A failed poll records the outcome and leaves the last known health alone.
	Inferring "down" from our own network blip would mark every bound meter
	``Gateway Down`` for a fault that is not the gateway's.
	"""
	client = client or get_client()
	names = frappe.get_all("TagMeter Gateway", pluck="name", order_by="name asc")

	healthy, unhealthy, errors = [], [], []
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

	down = set(unhealthy_gateways())
	for name in names:
		(unhealthy if name in down else healthy).append(name)

	return {"polled": len(names), "healthy": healthy, "unhealthy": unhealthy, "errors": errors}
