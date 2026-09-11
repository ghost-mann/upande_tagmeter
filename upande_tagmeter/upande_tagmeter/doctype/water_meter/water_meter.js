// ── List view: link health at a glance ──────────────────────────────────────
frappe.listview_settings["Water Meter"] = {
	add_fields: ["online", "status", "last_seen", "meter_profile"],

	onload(listview) {
		// A full sweep is serial -- the SMP has no bulk read -- so it runs in a
		// background worker rather than holding the request open for minutes.
		listview.page.add_menu_item(__("Pull readings for every meter"), () => {
			frappe.call({
				method: "upande_tagmeter.sync.enqueue_fleet_sync",
				freeze: true,
				freeze_message: __("Handing the sweep to a worker..."),
				callback(r) {
					const res = r.message || {};
					frappe.msgprint({
						title: __("Sweep started"),
						indicator: "blue",
						message: __(
							"Polling {0} meters in the background, one at a time. It takes a couple of minutes; readings and link states update as it goes. You can leave this page.",
							[res.meters || "all"],
						),
					});
				},
			});
		});

		// Selected rows only. Each meter is a separate call to the SMP taken
		// serially, so this is capped server-side at 25 -- for the whole fleet
		// use the menu item above, which runs in a worker.
		listview.page.add_actions_menu_item(__("Pull readings now"), () => {
			const names = listview.get_checked_items(true);
			if (!names.length) {
				frappe.msgprint(__("Select the meters you want to poll."));
				return;
			}
			frappe.call({
				method: "upande_tagmeter.sync.poll_many",
				args: { meter_sns: names },
				freeze: true,
				freeze_message: __("Asking the SMP about {0} meters...", [names.length]),
				callback(r) {
					const res = r.message || {};
					const tally = Object.entries(res.tally || {})
						.map(([k, v]) => `${v} ${k}`)
						.join(", ");
					frappe.show_alert({
						message: __("Polled {0}: {1}", [res.polled, tally || __("no results")]),
						indicator: res.failures && res.failures.length ? "orange" : "green",
					});
					listview.refresh();
				},
			});
		}, false);
	},

	get_indicator(doc) {
		if (doc.status === "Decommissioned") {
			return [__("Decommissioned"), "gray", "status,=,Decommissioned"];
		}
		if (doc.status === "Never Seen") {
			return [__("Never Seen"), "orange", "status,=,Never Seen"];
		}
		if (cint(doc.online)) return [__("Online"), "green", "online,=,1"];
		// Offline means "reported before, but not recently" -- the window is
		// tagmeter_offline_after_hours (default 30), because meters report
		// roughly every 12 hours.
		return [__("Offline"), "red", "online,=,0"];
	},
};

// ── Form ────────────────────────────────────────────────────────────────────
frappe.ui.form.on("Water Meter", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Poll Now"), () => poll(frm), __("TagMeter"));
		render_valve(frm);
	},
});

function poll(frm) {
	frappe.call({
		method: "upande_tagmeter.sync.poll_meter",
		args: { meter_sn: frm.doc.name },
		freeze: true,
		freeze_message: __("Asking the SMP..."),
		callback(r) {
			frappe.show_alert({
				message: __("Poll result: {0}", [(r.message || {}).status]),
				indicator: "blue",
			});
			frm.reload_doc();
		},
	});
}

// The switch is a view of `valve_reported` -- what the meter actually said --
// never of the request. A toggle that snapped to the requested position would
// imply the valve had moved when only the request had been accepted, so while a
// command is in flight it shows the requested position and says so.
//
// It stays USABLE though: the server supersedes a pending command rather than
// refusing the next one, as the vendor's own console does, so disabling the
// switch here would enforce a lock the server no longer has.
function render_valve(frm) {
	const wrapper = frm.get_field("valve_toggle_html").$wrapper;

	// 90 of the 100 meters have no valve at all -- the SMP refuses them with
	// "Meter ID X has no valve, Valve Control Aborted". Say so plainly rather
	// than rendering nothing: an absent control reads as a broken page.
	if (frm.doc.meter_profile !== "Quinto Prepaid") {
		wrapper.html(`
			<div class="text-muted" style="font-size: var(--text-sm)">
				${__("This meter has no valve. Its profile is <b>{0}</b>, and the SMP refuses valve commands for it. Only the Quinto Prepaid meters can be actuated.", [frappe.utils.escape_html(frm.doc.meter_profile || "unknown")])}
			</div>
		`);
		return;
	}
	const reported = frm.doc.valve_reported || "Unknown";
	const desired = frm.doc.valve_desired || "Unknown";
	const in_flight = frm.doc.valve_in_flight;

	const shown = in_flight && desired !== "Unknown" ? desired : reported;
	const is_open = shown === "Open";
	const known = shown === "Open" || shown === "Closed";
	const disabled = !frappe.model.can_write("Meter Command");

	let note = "";
	if (in_flight) {
		note = `<div class="tm-note tm-pending">${__(
			"Command {0} is in flight. An actuated meter answers in seconds and its reading reaches the SMP about two minutes later, so this usually confirms within three minutes.",
			[`<a href="/app/meter-command/${encodeURIComponent(in_flight)}">${frappe.utils.escape_html(in_flight)}</a>`],
		)}</div>`;
	} else if (desired !== "Unknown" && desired !== reported) {
		note = `<div class="tm-note tm-disagree">${__(
			"Requested <b>{0}</b>, meter last reported <b>{1}</b>. Nothing is in flight, so the meter is not doing what was asked.",
			[desired, reported],
		)}</div>`;
	} else if (!known) {
		note = `<div class="tm-note tm-unknown">${__(
			"The meter has not reported a valve state yet.",
		)}</div>`;
	} else {
		note = `<div class="tm-note tm-ok">${__("Meter reports the valve is {0}.", [
			__(reported).toLowerCase(),
		])}</div>`;
	}

	wrapper.html(`
		<style>
			.tm-valve { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
			.tm-switch { position: relative; width: 92px; height: 34px; border-radius: 999px;
				background: var(--gray-300); cursor: pointer; transition: background .18s ease;
				border: 1px solid var(--border-color); flex: none; }
			.tm-switch[data-on="1"] { background: var(--green-500); }
			.tm-switch[data-known="0"] { background: var(--gray-200); }
			.tm-switch[aria-disabled="true"] { cursor: not-allowed; opacity: .65; }
			.tm-knob { position: absolute; top: 3px; left: 3px; width: 26px; height: 26px;
				border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.28);
				transition: left .18s ease; }
			.tm-switch[data-on="1"] .tm-knob { left: 61px; }
			.tm-state { font-weight: 600; }
			.tm-note { font-size: var(--text-sm); color: var(--text-muted); margin-top: 8px; }
			.tm-pending { color: var(--orange-600); }
			.tm-disagree { color: var(--red-600); }
		</style>
		<div class="tm-valve">
			<div class="tm-switch" data-on="${is_open ? 1 : 0}" data-known="${known ? 1 : 0}"
				aria-disabled="${disabled}" role="switch" aria-checked="${is_open}"
				title="${disabled ? __("You cannot issue commands") : __("Click to change")}">
				<div class="tm-knob"></div>
			</div>
			<div>
				<div class="tm-state">${frappe.utils.escape_html(__(shown))}${
					in_flight ? ` <span class="text-muted">(${__("requested")})</span>` : ""
				}</div>
				<div class="text-muted" style="font-size: var(--text-sm)">${
					frm.doc.last_seen
						? __("as of {0}", [frappe.datetime.str_to_user(frm.doc.last_seen)])
						: __("never reported")
				}</div>
			</div>
		</div>
		${note}
	`);

	if (disabled) return;
	wrapper.find(".tm-switch").on("click", () => {
		// Flipping asks for the opposite of what is currently shown.
		set_valve(frm, is_open ? "close" : "open");
	});
}

function set_valve(frm, action) {
	const verb = action === "open" ? __("open") : __("close");
	frappe.confirm(
		__(
			"Ask meter {0} to {1} its valve?<br><br>The SMP accepts this immediately, but that only means it was <b>queued</b> — not that the valve moved. Confirmation arrives with the meter's next reading, usually within about three minutes.",
			[frm.doc.name, verb],
		),
		() => {
			frappe.call({
				method: "upande_tagmeter.valve.set_valve",
				args: { water_meter: frm.doc.name, action: action },
				freeze: true,
				freeze_message: __("Queueing with the SMP..."),
				callback(r) {
					const res = r.message || {};
					frappe.msgprint({
						title: __("Command {0}", [res.command || ""]),
						indicator: res.status === "Rejected" ? "red" : "orange",
						message:
							res.status === "Rejected"
								? __("The SMP refused it: {0}", [res.message || __("no reason given")])
								: __("Queued. The meter must report back before this is confirmed."),
					});
					frm.reload_doc();
				},
			});
		},
	);
}
