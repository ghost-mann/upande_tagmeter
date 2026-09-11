"""Seed the meter registry, and audit it against the SMP.

The SMP has no list-meters endpoint, so the app can never discover its own
fleet -- every meter has to be registered here with its 14-digit serial. What
the SMP *does* offer is a verify-one call, which makes the registry
self-validating: :func:`verify_against_smp` confirms each serial and takes the
DevEUI and bore straight from the vendor's answer rather than trusting a parsed
device name.

Run with::

	bench --site kaitet console
	>>> from upande_tagmeter.setup import import_meters
	>>> import_meters.run()
	>>> import_meters.verify_against_smp()
"""

import csv
import pathlib
import re

import frappe

from upande_tagmeter.vendor.errors import Outcome

TSV = pathlib.Path(__file__).resolve().parents[1] / "data" / "kiwasco-devices.tsv"

PROFILE_MAP = {
	"Tagmeter Ultrasonic Water Meter": "AMR",
	"TagMeter Quinto Prepaid": "Quinto Prepaid",
}
# DevEUI byte 6 of 8 -- chars 10:12 of the 16-hex string.
PROFILE_BYTE = {"AMR": "19", "Quinto Prepaid": "20"}
NAME_RE = re.compile(r"^(\d{14})-(\d{1,3})$")

DEFAULT_SITE = "Kiwasco"


def run(path: str | None = None, dry_run: bool = False, site: str = DEFAULT_SITE) -> dict:
	"""Create or update one Water Meter per row. Idempotent.

	The serial is taken from the ``<sn>-<index>`` device name but validated
	rather than trusted -- a wrong serial addresses a different meter on the
	SMP, or none, and the SMP answers an unknown serial with a plain error
	rather than anything that looks like a warning.
	"""
	source = pathlib.Path(path) if path else TSV
	created = updated = 0
	problems: list[str] = []

	with source.open() as handle:
		for row in csv.DictReader(handle, delimiter="\t"):
			dev_eui = (row.get("dev_eui") or "").strip().lower()
			device_name = (row.get("device_name") or "").strip()
			profile = PROFILE_MAP.get((row.get("device_profile") or "").strip())

			match = NAME_RE.match(device_name)
			if not match:
				problems.append(f"{device_name!r}: not <14-digit-sn>-<index>")
				continue
			if not profile:
				problems.append(f"{device_name!r}: unknown profile {row.get('device_profile')!r}")
				continue

			meter_sn, index = match.group(1), int(match.group(2))
			expected_byte = PROFILE_BYTE.get(profile)
			if len(dev_eui) == 16 and dev_eui[10:12] != expected_byte:
				problems.append(
					f"{meter_sn}: DevEUI byte 6 is {dev_eui[10:12]!r}, profile {profile!r} "
					f"expects {expected_byte!r}"
				)

			values = {
				"meter_profile": profile,
				"device_index": index,
				"dev_eui": dev_eui or None,
				"site": site,
				# The SMP binds each meter to exactly one gateway. Taken from the
				# TSV rather than inferred: RSSI tells you link quality, not binding.
				"gateway": (row.get("gateway") or "").strip().upper() or None,
			}
			if dry_run:
				created += 0 if frappe.db.exists("Water Meter", meter_sn) else 1
				continue

			if frappe.db.exists("Water Meter", meter_sn):
				doc = frappe.get_doc("Water Meter", meter_sn)
				doc.update(values)
				doc.save(ignore_permissions=True)
				updated += 1
			else:
				doc = frappe.get_doc({
					"doctype": "Water Meter",
					"meter_sn": meter_sn,
					"status": "Never Seen",
					**values,
				})
				doc.insert(ignore_permissions=True)
				created += 1

	return {
		"source": str(source),
		"created": created,
		"updated": updated,
		"problems": problems,
		"total": frappe.db.count("Water Meter") if not dry_run else None,
	}


def verify_against_smp(limit: int | None = None) -> dict:
	"""Ask the SMP about every registered meter and record what it says.

	One read per meter -- there is no bulk call. Sets ``vendor_registered``, and
	overwrites DevEUI and bore with the vendor's values, which are authoritative
	in a vendor-only deployment.
	"""
	from upande_tagmeter.sync import get_client

	client = get_client()
	names = frappe.get_all(
		"Water Meter", pluck="name", order_by="meter_sn asc",
		limit_page_length=int(limit) if limit else 0,
	)

	known: list[str] = []
	unknown: list[str] = []
	no_record: list[str] = []
	errors: list[tuple[str, str]] = []
	corrections: list[str] = []

	for name in names:
		try:
			outcome, data = client.get_latest_amr(name)
		except Exception as exc:
			errors.append((name, f"{type(exc).__name__}: {exc}"))
			continue

		if outcome is Outcome.UNKNOWN_METER:
			unknown.append(name)
			frappe.db.set_value("Water Meter", name, "vendor_registered", 0, update_modified=False)
			continue
		if outcome is not Outcome.OK:
			errors.append((name, outcome.value))
			continue

		updates = {"vendor_registered": 1}
		if data is None:
			no_record.append(name)
		else:
			known.append(name)
			stored_eui = frappe.db.get_value("Water Meter", name, "dev_eui")
			if data.get("dev_eui") and data["dev_eui"] != stored_eui:
				corrections.append(f"{name}: dev_eui {stored_eui} -> {data['dev_eui']}")
				updates["dev_eui"] = data["dev_eui"]
			if data.get("connection"):
				updates["connection"] = data["connection"]
		for field, value in updates.items():
			frappe.db.set_value("Water Meter", name, field, value, update_modified=False)

	return {
		"checked": len(names),
		"known_with_record": len(known),
		"known_without_record": len(no_record),
		"unknown_to_smp": unknown,
		"dev_eui_corrections": corrections,
		"errors": errors,
	}
