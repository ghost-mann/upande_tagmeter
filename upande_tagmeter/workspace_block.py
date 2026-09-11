"""The TagMeter workspace navigation, as a Custom HTML Block.

Frappe's default workspace gives you a flat wall of shortcuts. This renders the
same destinations as grouped tiles with a one-line explanation each, so an
operator can tell "Offline" from "No Data on SMP" without opening both.

Kept in Python rather than hand-created on each site so it ships with the app
and can be re-synced idempotently after a change.
"""

import frappe

BLOCK_NAME = "TagMeter Navigation"

# Tiles carry data-doctype so the script can hide any whose doctype this user
# cannot read -- the same trick the Webstore navigation block uses.
GROUPS = [
	("Fleet", [
		("Water Meter", "/app/water-meter", "All Meters",
		 "Every meter and its current state", "#3b82f6",
		 '<path d="M12 2.7 6.6 9a7.4 7.4 0 1 0 10.8 0z"/>'),
		("Water Meter", "/app/water-meter?online=1", "Online",
		 "Reported inside the staleness window", "#16a34a",
		 '<path d="M5 12.6a10 10 0 0 1 14 0"/><path d="M8.5 16a5.5 5.5 0 0 1 7 0"/>'
		 '<line x1="12" y1="20" x2="12.01" y2="20"/><path d="M1.5 9a15 15 0 0 1 21 0"/>'),
		("Water Meter", "/app/water-meter?online=0", "Offline",
		 "Silent longer than 30 hours", "#dc2626",
		 '<line x1="2" y1="2" x2="22" y2="22"/><path d="M8.5 16a5.5 5.5 0 0 1 7 0"/>'
		 '<line x1="12" y1="20" x2="12.01" y2="20"/><path d="M5 12.6a10 10 0 0 1 5-2.5"/>'),
		("Water Meter", "/app/water-meter?meter_profile=Quinto%20Prepaid", "Prepaid Meters",
		 "The ten meters that have a valve", "#7c3aed",
		 '<rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/>'),
	]),
	("Network", [
		("TagMeter Gateway", "/app/tagmeter-gateway", "Gateways",
		 "Every gateway and its last heartbeat", "#0ea5e9",
		 '<path d="M5 12.6a10 10 0 0 1 14 0"/><path d="M8.5 16a5.5 5.5 0 0 1 7 0"/>'
		 '<line x1="12" y1="20" x2="12.01" y2="20"/><path d="M1.5 9a15 15 0 0 1 21 0"/>'),
		("Water Meter", "/app/water-meter?link_state=Gateway%20Down", "Gateway Down",
		 "Quiet because the network is, not the meter", "#dc2626",
		 '<line x1="2" y1="2" x2="22" y2="22"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/>'
		 '<path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><line x1="12" y1="20" x2="12.01" y2="20"/>'),
		("Water Meter", "/app/water-meter?link_state=Silent", "Silent",
		 "Gateway is healthy, so it is the meter", "#b91c1c",
		 '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-10-8-10-8a18.45 18.45 0 0 1 5.06-5.94"/>'
		 '<line x1="1" y1="1" x2="23" y2="23"/>'),
		("Water Meter", "/app/water-meter?link_state=Late", "Late",
		 "Missed one expected report", "#d97706",
		 '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
	]),
	("Telemetry", [
		("Meter Reading", "/app/meter-reading", "Readings",
		 "Every reading pulled from the SMP", "#0891b2",
		 '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/>'
		 '<line x1="6" y1="20" x2="6" y2="14"/>'),
		("Water Meter", "/app/water-meter?last_sync_outcome=server_error", "No Data on SMP",
		 "The SMP holds no reading for these yet", "#d97706",
		 '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
		 '<line x1="12" y1="16" x2="12.01" y2="16"/>'),
		("Water Meter", "/app/water-meter?status=Never%20Seen", "Never Seen",
		 "Never produced a reading at all", "#ea580c",
		 '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-10-8-10-8a18.45 18.45 0 0 1 5.06-5.94"/>'
		 '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 10 8 10 8a18.5 18.5 0 0 1-2.16 3.19"/>'
		 '<line x1="1" y1="1" x2="23" y2="23"/>'),
		("Meter Reading", "/app/meter-reading?status_text_mismatch=1", "Status Disagreements",
		 "Our decode contradicts the SMP's wording", "#eab308",
		 '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>'
		 '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
	]),
	("Valve Control", [
		("Meter Command", "/app/meter-command", "Commands",
		 "Every valve request and its outcome", "#6366f1",
		 '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/>'
		 '<line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/>'
		 '<line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/>'
		 '<line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/>'
		 '<line x1="17" y1="16" x2="23" y2="16"/>'),
		("Meter Command", "/app/meter-command?status=Queued", "In Flight",
		 "Accepted by the SMP, awaiting the meter", "#d97706",
		 '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
		("Meter Command", "/app/meter-command?status=Confirmed", "Confirmed",
		 "Proved by a reading taken afterwards", "#16a34a",
		 '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>'),
		("Meter Command", "/app/meter-command?status=Expired", "Expired",
		 "The meter never reported the request", "#dc2626",
		 '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/>'
		 '<line x1="9" y1="9" x2="15" y2="15"/>'),
	]),
	("Dashboard", [
		# No data-doctype: this is a page, not a doctype, so the visibility
		# script must not try to check it against can_read.
		("", "/tagmeter", "Fleet Dashboard",
		 "Every meter, by when it last spoke", "#2b6ca3",
		 '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12"/>'),
		("", "/tagmeter#view-valves", "Valve Board",
		 "The ten meters that can be actuated", "#1b4a73",
		 '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>'),
	]),
	("Alarms", [
		("Water Meter", "/app/water-meter?alarm_empty_pipe=1", "Empty Pipe",
		 "No water in the pipe", "#0891b2",
		 '<path d="M12 2.7 6.6 9a7.4 7.4 0 1 0 10.8 0z"/><line x1="4" y1="20" x2="20" y2="4"/>'),
		("Water Meter", "/app/water-meter?alarm_reverse_flow=1", "Reverse Flow",
		 "Water moving the wrong way", "#7c3aed",
		 '<polyline points="1 4 1 10 7 10"/>'
		 '<path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>'),
		("Water Meter", "/app/water-meter?alarm_low_balance=1", "Low Balance",
		 "Prepaid credit running out", "#db2777",
		 '<line x1="12" y1="1" x2="12" y2="23"/>'
		 '<path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'),
		("Water Meter", "/app/water-meter?battery_low=1", "Battery Low",
		 "Meter battery needs attention", "#ea580c",
		 '<rect x="1" y="6" width="18" height="12" rx="2"/><line x1="23" y1="11" x2="23" y2="13"/>'
		 '<line x1="5" y1="10" x2="5" y2="14"/>'),
	]),
]

STYLE = """.wsn{padding:4px 2px;font-family:var(--font-stack,'Inter',sans-serif);}
.wsn-title{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8a8f98;font-weight:600;margin:18px 0 10px 2px;}
.wsn-title:first-child{margin-top:0;}
.wsn-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;}
.wsn-tile{display:flex;align-items:center;gap:11px;padding:13px 15px;border:1px solid var(--border-color,#e2e4e9);border-radius:10px;background:var(--card-bg,#fff);text-decoration:none;color:inherit;transition:box-shadow .18s ease,transform .18s ease;}
.wsn-tile:hover{box-shadow:0 2px 6px rgba(0,0,0,.06);transform:translateY(-1px);text-decoration:none;}
.wsn-tile:hover .wsn-lb,.wsn-tile:hover .wsn-sub{text-decoration:none;}
.wsn-ic{width:36px;height:36px;border-radius:9px;display:flex;align-items:center;justify-content:center;flex:0 0 auto;color:inherit;}
.wsn-ic svg{width:18px;height:18px;}
.wsn-tx{min-width:0;display:flex;flex-direction:column;}
.wsn-lb{display:block;font-size:13.5px;font-weight:600;line-height:1.2;}
.wsn-sub{display:block;font-size:11px;color:#8a8f98;line-height:1.3;margin-top:2px;}
.wsn-hide{display:none !important;}"""

SCRIPT = """(function() {
  // A tile pointing at a doctype this user cannot read would 404 on click.
  // frappe.boot.user.can_read is built from the site's real DocType rows for
  // this session, so a doctype that is absent can never appear in it -- this
  // costs no request and degrades to "hidden" either way.
  var canRead = (window.frappe && frappe.boot && frappe.boot.user && frappe.boot.user.can_read) || [];
  root_element.querySelectorAll('.wsn-tile[data-doctype]').forEach(function(tile) {
    var doctype = tile.getAttribute('data-doctype');
    if (doctype && canRead.indexOf(doctype) < 0) tile.classList.add('wsn-hide');
  });
  // Do not leave a group heading above an empty grid.
  root_element.querySelectorAll('.wsn-grid').forEach(function(grid) {
    if (grid.querySelectorAll('.wsn-tile:not(.wsn-hide)').length === 0) {
      grid.classList.add('wsn-hide');
      var title = grid.previousElementSibling;
      if (title && title.classList.contains('wsn-title')) title.classList.add('wsn-hide');
    }
  });
})();"""


def _rgba(hex_colour: str, alpha: str = ".13") -> str:
	h = hex_colour.lstrip("#")
	r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
	return f"rgba({r},{g},{b},{alpha})"


def build_html() -> str:
	parts = ['<div class="wsn">']
	for title, tiles in GROUPS:
		parts.append(f'\n  <div class="wsn-title">{title}</div>')
		parts.append('  <div class="wsn-grid">')
		for doctype, href, label, sub, colour, svg in tiles:
			# Only doctype tiles carry data-doctype; the visibility script checks
			# it against can_read, and a page route would never appear there.
			dt = f' data-doctype="{doctype}"' if doctype else ""
			parts.append(
				f'\n    <a class="wsn-tile"{dt} href="{href}">'
				f'\n      <span class="wsn-ic" style="background:{_rgba(colour)};color:{colour}">'
				f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
				f'stroke-linecap="round" stroke-linejoin="round">{svg}</svg></span>'
				f'\n      <span class="wsn-tx"><span class="wsn-lb">{label}</span>'
				f'<span class="wsn-sub">{sub}</span></span>'
				f'\n    </a>'
			)
		parts.append('\n  </div>')
	parts.append('\n\n</div>\n')
	return "".join(parts)


def sync() -> str:
	"""Create or update the block. Idempotent, so safe to re-run on migrate."""
	values = {"html": build_html(), "style": STYLE, "script": SCRIPT}
	if frappe.db.exists("Custom HTML Block", BLOCK_NAME):
		block = frappe.get_doc("Custom HTML Block", BLOCK_NAME)
		block.update(values)
		block.save(ignore_permissions=True)
	else:
		block = frappe.get_doc({
			"doctype": "Custom HTML Block", "name": BLOCK_NAME, **values,
		})
		block.insert(ignore_permissions=True)
		# Without a role row the block renders for nobody.
		block.append("roles", {"role": "System Manager"})
		block.append("roles", {"role": "TagMeter Operator"})
		block.save(ignore_permissions=True)
	return block.name


WORKSPACE = "Upande TagMeter"
CONTENT = [
	{"id": "tm-cb-navigation", "type": "custom_block",
	 "data": {"custom_block_name": BLOCK_NAME, "col": 12}},
]


def attach_to_workspace() -> bool:
	"""Point the workspace at the navigation block.

	Frappe deliberately does not overwrite an existing Workspace's ``content``
	from the app's JSON on migrate, because workspaces are user-editable. That
	is the right default in general, but it means a shipped layout change never
	reaches a site that already has the workspace. So this sets it explicitly.

	Kept narrow on purpose: it rewrites ``content`` only when the workspace is
	not already pointing at the block, so a later hand-edit is not clobbered on
	every migrate.
	"""
	import json

	if not frappe.db.exists("Workspace", WORKSPACE):
		return False
	workspace = frappe.get_doc("Workspace", WORKSPACE)
	already = BLOCK_NAME in (workspace.content or "")
	if already and any(r.custom_block_name == BLOCK_NAME for r in (workspace.custom_blocks or [])):
		return False

	# v16 made Workspace.type mandatory (Workspace | Link | URL). Records
	# created from a v15-era JSON have it NULL, which blocks any save.
	if not workspace.get("type"):
		workspace.type = "Workspace"
	workspace.content = json.dumps(CONTENT)
	workspace.custom_blocks = []
	workspace.append("custom_blocks", {"custom_block_name": BLOCK_NAME, "label": BLOCK_NAME})
	# The tiles cover every destination the shortcuts did; keeping both would
	# render each one twice.
	workspace.shortcuts = []
	workspace.save(ignore_permissions=True)
	return True


# v16 builds the left nav from a Workspace Sidebar record, generated once at
# install from whatever the workspace held then. Ours was generated from the
# old shortcut list, so it still advertises shortcuts that no longer exist.
# Rebuilding it here keeps the nav in step with the tiles.
SIDEBAR_ITEMS = [
	{"label": "Home", "type": "Link", "link_type": "Workspace", "link_to": WORKSPACE,
	 "icon": "home"},
	{"label": "Water Meter", "type": "Link", "link_type": "DocType", "link_to": "Water Meter",
	 "icon": "droplet"},
	{"label": "Meter Reading", "type": "Link", "link_type": "DocType", "link_to": "Meter Reading",
	 "icon": "activity"},
	{"label": "Meter Command", "type": "Link", "link_type": "DocType", "link_to": "Meter Command",
	 "icon": "sliders"},
]


def sync_sidebar() -> bool:
	"""Rebuild the Workspace Sidebar items to match the current doctypes."""
	if not frappe.db.exists("Workspace Sidebar", WORKSPACE):
		return False
	sidebar = frappe.get_doc("Workspace Sidebar", WORKSPACE)
	current = [(i.label, i.link_type, i.link_to) for i in (sidebar.items or [])]
	wanted = [(i["label"], i["link_type"], i["link_to"]) for i in SIDEBAR_ITEMS]
	if current == wanted:
		return False
	sidebar.items = []
	for item in SIDEBAR_ITEMS:
		sidebar.append("items", item)
	sidebar.save(ignore_permissions=True)
	return True
