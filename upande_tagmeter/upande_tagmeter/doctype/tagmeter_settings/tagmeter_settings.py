# Copyright (c) 2026, ghost-mann and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class TagMeterSettings(Document):
	def validate(self):
		if self.api_url:
			self.api_url = self.api_url.strip().rstrip("/")
		# A wrong URL fails as an auth error rather than anything obvious, so
		# catch the shape here where it can still be explained.
		if self.api_url and not self.api_url.startswith(("http://", "https://")):
			frappe.throw("API URL must start with http:// or https://")
