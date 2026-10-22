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
						.map(([k, v]) => `${v} ${TALLY_LABEL[k] || k}`)
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
		frm.add_custom_button(__("Meter Readings"), () =>
			frappe.set_route("List", "Meter Reading", { water_meter: frm.doc.name }), __("TagMeter"));
		if (frm.doc.meter_profile === "Quinto Prepaid") {
			frm.add_custom_button(__("Valve Commands"), () =>
				frappe.set_route("List", "Meter Command", { water_meter: frm.doc.name }), __("TagMeter"));
		}
		render_valve(frm);
	},
});

const TALLY_LABEL = { ok: __("new reading"), refreshed: __("refreshed, no newer reading yet") };

function poll(frm) {
	frappe.call({
		method: "upande_tagmeter.sync.poll_meter",
		args: { meter_sn: frm.doc.name },
		freeze: true,
		freeze_message: __("Asking the SMP..."),
		callback(r) {
			const res = r.message || {};
			frappe.show_alert({ message: poll_message(res), indicator: POLL_INDICATOR[res.status] || "orange" }, 10);
			frm.reload_doc();
		},
	});
}

const POLL_INDICATOR = { ok: "green", refreshed: "blue" };

// "refreshed" is the normal answer between reports, not a failure: the SMP
// only holds a meter's latest record, and meters send about once a day, so
// every poll in between re-fetches the record already stored and rewrites it.
function poll_message(res) {
	const when = res.latest ? frappe.datetime.str_to_user(res.latest) : null;
	switch (res.status) {
		case "ok":
			return __("New reading from {0}.", [when]);
		case "refreshed":
			return __("Refreshed the reading from {0} from the SMP. It has nothing newer yet; meters report about once a day.", [when]);
		case "server_error":
			return __("The SMP holds no reading for this meter yet.");
		case "unknown_meter":
			return __("The SMP does not recognise this serial.");
		case "no_amr_record":
			return __("The SMP knows this meter but has no reading for it.");
		default:
			return __("Poll result: {0}", [res.status]);
	}
}

// A meter only speaks when it chooses to, so the form cannot show where the
// valve is *now*. It shows the three things that are known, separately:
//
//   1. what the meter last reported, and when;
//   2. what was asked for, whether the SMP accepted it, and when it gives up;
//   3. when the meter should next report on its own.
//
// Collapsing those into one word was the defect: with an open in flight the
// form said "Closed" -- true of the last report, wrong about the valve.
//
// The switch shows the requested position while a command is pending, marked
// "requested", and stays usable: the server supersedes a pending command
// rather than refusing the next one, as the vendor's own console does.
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
	frappe
		.call({ method: "upande_tagmeter.valve.valve_status", args: { water_meter: frm.doc.name } })
		.then((r) => draw_valve(frm, wrapper, r.message || {}));
}

const when = (dt) => (dt ? frappe.datetime.str_to_user(dt) : null);
const cmd_link = (name) =>
	`<a href="/app/meter-command/${encodeURIComponent(name)}">${frappe.utils.escape_html(name)}</a>`;

function draw_valve(frm, wrapper, st) {
	const reported = st.reported || "Unknown";
	const pending = st.pending;
	const shown = pending ? pending.requested_state : reported;
	const is_open = shown === "Open";
	const known = shown === "Open" || shown === "Closed";
	const disabled = !frappe.model.can_write("Meter Command");
	const due = st.next_report_due
		? __("next scheduled report due about {0}", [when(st.next_report_due)])
		: __("no steady reporting schedule to estimate its next report from");

	const facts = [
		`<div><span class="tm-k">${__("Meter last reported")}</span> <b>${__(reported)}</b>${
			st.reported_at ? ` · ${when(st.reported_at)}` : ` · ${__("never")}`
		}</div>`,
	];
	let note;
	if (pending) {
		const accepted = pending.accepted
			? __("accepted by the SMP at {0}", [when(pending.sent_at)])
			: pending.sent_at
				? __("not accepted by the SMP yet (last try {0}: {1}); the watchdog retries it", [
						when(pending.sent_at), frappe.utils.escape_html(pending.last_outcome || "?")])
				: __("not sent yet");
		facts.push(`<div><span class="tm-k">${__("Requested")}</span> <b>${__(pending.requested_state)}</b> · ${cmd_link(pending.name)} · ${accepted}</div>`);
		const how = pending.requested_state === "Open"
			? __("Meters do not report an open straight away; it shows at the meter's {0}. Until then the valve may well be open already.", [due])
			: __("A close is normally reported within about two minutes.");
		note = `<div class="tm-note tm-pending">${how} ${__("Gives up at {0}.", [when(pending.deadline)])}</div>`;
	} else {
		facts.push(`<div><span class="tm-k">${__("Next report")}</span> ${due}</div>`);
		const last = st.last_command;
		if (last && last.status !== "Confirmed" && last.requested_state !== reported) {
			note = `<div class="tm-note tm-disagree">${__(
				"Last request {0} for <b>{1}</b> ended {2}; the meter still reports <b>{3}</b>.",
				[cmd_link(last.name), __(last.requested_state), __(last.status).toLowerCase(), __(reported)],
			)}</div>`;
		} else if (!known) {
			note = `<div class="tm-note tm-unknown">${__("The meter has not reported a valve state yet.")}</div>`;
		} else {
			note = `<div class="tm-note tm-ok">${__("Nothing in flight. The valve is {0} as of the last report.", [__(reported).toLowerCase()])}</div>`;
		}
	}

	wrapper.html(`
		<style>
			.tm-valve { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
			.tm-switch { position: relative; width: 92px; height: 34px; border-radius: 999px;
				background: var(--gray-300); cursor: pointer; transition: background .18s ease;
				border: 1px solid var(--border-color); flex: none; }
			.tm-switch[data-on="1"] { background: var(--green-500); }
			.tm-switch[data-known="0"] { background: var(--gray-200); }
			.tm-switch[data-pending="1"] { background-image: repeating-linear-gradient(45deg,
				transparent 0 6px, rgba(255,255,255,.35) 6px 12px); }
			.tm-switch[aria-disabled="true"] { cursor: not-allowed; opacity: .65; }
			.tm-knob { position: absolute; top: 3px; left: 3px; width: 26px; height: 26px;
				border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.28);
				transition: left .18s ease; }
			.tm-switch[data-on="1"] .tm-knob { left: 61px; }
			.tm-state { font-weight: 600; }
			.tm-facts { font-size: var(--text-sm); line-height: 1.7; }
			.tm-k { color: var(--text-muted); display: inline-block; min-width: 130px; }
			.tm-note { font-size: var(--text-sm); color: var(--text-muted); margin-top: 8px; }
			.tm-pending { color: var(--orange-600); }
			.tm-disagree { color: var(--red-600); }
		</style>
		<div class="tm-valve">
			<div class="tm-switch" data-on="${is_open ? 1 : 0}" data-known="${known ? 1 : 0}"
				data-pending="${pending ? 1 : 0}"
				aria-disabled="${disabled}" role="switch" aria-checked="${is_open}"
				title="${disabled ? __("You cannot issue commands") : __("Click to change")}">
				<div class="tm-knob"></div>
			</div>
			<div class="tm-state">${frappe.utils.escape_html(__(shown))}${
				pending ? ` <span class="text-muted">(${__("requested, not yet reported")})</span>` : ""
			}</div>
		</div>
		<div class="tm-facts" style="margin-top:8px">${facts.join("")}</div>
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
			"Ask meter {0} to {1} its valve?<br><br>The SMP accepts this immediately, but that only means it was <b>queued</b> — not that the valve moved. Confirmation arrives with the meter's next reading: within minutes for a close, but an open only shows at the meter's next scheduled report, which can be hours away.",
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
