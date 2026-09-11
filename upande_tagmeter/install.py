"""Post-install and post-migrate setup."""

import frappe

from upande_tagmeter import workspace_block

ROLE = "TagMeter Operator"


def after_install():
	if not frappe.db.exists("Role", ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert(
			ignore_permissions=True
		)
	after_migrate()


def after_migrate():
	"""Keep the workspace navigation block in step with the code.

	The tiles live in ``workspace_block.py``, so re-syncing on every migrate
	means editing them there is enough -- no hand-editing a record per site.
	"""
	workspace_block.sync()
	workspace_block.attach_to_workspace()
	workspace_block.sync_sidebar()
	frappe.db.commit()
