"""Seed the gateway registry.

The SMP has no list-gateways endpoint -- ``get_gateway_status`` answers for one
ID at a time -- so the inventory has to be declared here, exactly as the meter
registry is. There are two, and they change rarely.

Run with::

	bench --site kaitet.local console
	>>> from upande_tagmeter.setup import import_gateways
	>>> import_gateways.run()
"""

import csv
import pathlib
import re

import frappe

TSV = pathlib.Path(__file__).resolve().parents[1] / "data" / "kiwasco-gateways.tsv"

# 16 hex characters, the EUI-64 the SMP uses for gateway IDs.
GATEWAY_ID_RE = re.compile(r"^[0-9A-F]{16}$")


def run(path: str | None = None, dry_run: bool = False) -> dict:
	"""Create or update one TagMeter Gateway per row. Idempotent."""
	source = pathlib.Path(path) if path else TSV
	created = updated = 0
	problems: list[str] = []

	with source.open() as handle:
		for row in csv.DictReader(handle, delimiter="\t"):
			gateway_id = (row.get("gateway_id") or "").strip().upper()
			if not GATEWAY_ID_RE.match(gateway_id):
				problems.append(f"{gateway_id!r}: not 16 hex characters")
				continue

			values = {
				"label": (row.get("label") or "").strip() or gateway_id,
				"site": (row.get("site") or "").strip() or None,
				"gateway_status": (row.get("gateway_status") or "").strip() or "Active",
			}
			exists = frappe.db.exists("TagMeter Gateway", gateway_id)
			if dry_run:
				updated += 1 if exists else 0
				created += 0 if exists else 1
				continue

			if exists:
				doc = frappe.get_doc("TagMeter Gateway", gateway_id)
				doc.update(values)
				doc.save(ignore_permissions=True)
				updated += 1
			else:
				frappe.get_doc({
					"doctype": "TagMeter Gateway", "gateway_id": gateway_id, **values,
				}).insert(ignore_permissions=True)
				created += 1

	return {
		"source": str(source),
		"created": created,
		"updated": updated,
		"problems": problems,
		"total": frappe.db.count("TagMeter Gateway") if not dry_run else None,
	}
