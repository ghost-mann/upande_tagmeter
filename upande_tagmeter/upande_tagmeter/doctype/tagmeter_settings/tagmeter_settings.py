# Copyright (c) 2026, ghost-mann and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from upande_tagmeter import settings as tagmeter_settings


class TagMeterSettings(Document):
	def validate(self):
		self._tidy_urls()
		self._check_timezone()
		self._check_ordering()
		# Values are read through a cached doc, so a save that did not bust the
		# cache would leave every worker on the old numbers until something
		# else evicted it.
		frappe.clear_document_cache(self.doctype, self.name)

	def _tidy_urls(self):
		for field in ("api_url", "console_url"):
			value = (self.get(field) or "").strip().rstrip("/")
			self.set(field, value or None)
			# A wrong URL fails as an auth error rather than anything obvious,
			# so catch the shape here where it can still be explained.
			if value and not value.startswith(("http://", "https://")):
				frappe.throw(f"{self.meta.get_label(field)} must start with http:// or https://")

	def _check_timezone(self):
		zone = (self.meter_timezone or "").strip()
		self.meter_timezone = zone or None
		if not zone:
			return
		try:
			from zoneinfo import ZoneInfo

			ZoneInfo(zone)
		except Exception:
			# Silently accepting this would shift every stored reading by a
			# fixed offset, which looks like a meter fault rather than a typo.
			frappe.throw(
				f"{zone!r} is not a timezone this system knows. Use a full IANA "
				"name such as UTC or Africa/Nairobi."
			)

	def _check_ordering(self):
		"""Catch the two orderings that would make the states unreachable."""
		late = self.late_multiplier or tagmeter_settings.SPEC["late_multiplier"][1]
		silent = self.silent_multiplier or tagmeter_settings.SPEC["silent_multiplier"][1]
		if late >= silent:
			frappe.throw(
				f"Late ({late}×) must come before Silent ({silent}×), otherwise no "
				"meter ever reaches Late — it would go straight to Silent."
			)

		expiry = self.valve_timeout_minutes or tagmeter_settings.SPEC["valve_timeout_minutes"][1]
		if expiry < 3:
			# The measured round trip is ~15s of actuation plus ~2m04s of
			# ingestion lag. Expiring inside that window marks commands failed
			# that are still perfectly in flight.
			frappe.throw(
				"Command expiry must be at least 3 minutes. Confirmation cannot "
				"arrive sooner than about 2m20s, so anything below that expires "
				"commands that are still on their way."
			)
