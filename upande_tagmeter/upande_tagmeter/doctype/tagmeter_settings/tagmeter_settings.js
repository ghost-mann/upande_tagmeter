// Copyright (c) 2026, ghost-mann and contributors
// For license information, please see license.txt

frappe.ui.form.on("TagMeter Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => test_connection(frm));
		render_thresholds(frm);
		render_effective(frm);
	},

	// The thresholds panel is the point of the multipliers: "Late at 1.25x" is
	// not a number anyone can picture, "Late after 30h" is. Redraw on any input
	// that feeds it so the consequence is visible before the save, not after.
	expected_cycle_hours: render_thresholds,
	late_multiplier: render_thresholds,
	silent_multiplier: render_thresholds,
	offline_after_hours: render_thresholds,
});

function hours(value) {
	if (value === null || value === undefined) return "—";
	const rounded = Math.round(value * 100) / 100;
	if (rounded < 1) return `${Math.round(rounded * 60)} min`;
	if (rounded < 48) return `${rounded}h`;
	return `${Math.round((rounded / 24) * 10) / 10} days`;
}

function render_thresholds(frm) {
	const spec = { expected_cycle_hours: 24, late_multiplier: 1.25, silent_multiplier: 3, offline_after_hours: 30 };
	// Read from the form, not the server, so the preview tracks unsaved edits.
	// An empty field means "use the default", which is exactly what the server
	// will do with it.
	const value = (f) => frm.doc[f] || spec[f];
	const cycle = value("expected_cycle_hours");
	const late = cycle * value("late_multiplier");
	const silent = cycle * value("silent_multiplier");
	const offline = value("offline_after_hours");

	// Online and Late are set independently but describe the same silence, so
	// a gap between them is worth naming rather than leaving to be discovered.
	const drift = Math.abs(offline - late);
	const note = drift < 0.01
		? `<div style="color:var(--text-muted);margin-top:8px">Online flips exactly where <b>Late</b> begins. These agree.</div>`
		: `<div style="color:var(--orange-600,#d97706);margin-top:8px">Online flips at ${hours(offline)} but <b>Late</b> begins at ${hours(late)}. A meter will sit in one state while the other disagrees for ${hours(drift)}.</div>`;

	const row = (label, text, detail, colour) => `
		<div style="display:flex;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--border-color)">
			<span style="width:8px;height:8px;border-radius:50%;background:${colour};flex:0 0 auto"></span>
			<span style="font-weight:600;min-width:130px">${label}</span>
			<span style="min-width:90px">${text}</span>
			<span style="color:var(--text-muted);font-size:var(--text-sm)">${detail}</span>
		</div>`;

	frm.get_field("thresholds_html").$wrapper.html(`
		<div style="font-size:var(--text-md)">
			${row("Reporting", `every ${hours(cycle)}`, "what a healthy meter does", "#16a34a")}
			${row("Late", `after ${hours(late)}`, "missed one report", "#d97706")}
			${row("Silent", `after ${hours(silent)}`, "missed three — send someone", "#b91c1c")}
			${row("Offline flag", `after ${hours(offline)}`, "drives the Online checkbox", "#6366f1")}
			${note}
		</div>`);
}

function render_effective(frm) {
	const wrapper = frm.get_field("effective_html").$wrapper;
	wrapper.html(`<div class="text-muted">Loading…</div>`);
	frappe.call({ method: "upande_tagmeter.settings.effective" }).then((r) => {
		const rows = (r && r.message) || [];
		if (!rows.length) return wrapper.html("");

		// Naming the layer matters more than the value: it tells the reader
		// whether editing this page would change anything at all.
		const badge = {
			site_config: ["#6366f1", "site_config.json"],
			settings: ["#16a34a", "this page"],
			default: ["#8a8f98", "built-in default"],
		};
		const body = rows.map((row) => {
			const [colour, label] = badge[row.source] || badge.default;
			const shown = row.value === null || row.value === undefined || row.value === "" ? "—" : row.value;
			return `<tr>
				<td style="padding:5px 10px 5px 0"><code>${frappe.utils.escape_html(row.key)}</code></td>
				<td style="padding:5px 10px 5px 0;font-weight:600">${frappe.utils.escape_html(String(shown))}</td>
				<td style="padding:5px 10px 5px 0"><span style="color:${colour};font-size:var(--text-sm)">● ${label}</span></td>
				<td style="padding:5px 0;color:var(--text-muted);font-size:var(--text-sm)"><code>${frappe.utils.escape_html(row.site_config_key)}</code></td>
			</tr>`;
		}).join("");

		// Credential fields read blank on a site configured through
		// site_config.json, which looks like nothing is set up at all. Say so
		// where it will be seen, rather than only in the table below.
		const from_file = rows.filter((row) => row.source === "site_config").length;
		if (from_file) {
			frm.get_field("intro_html").$wrapper.find(".tm-from-file").remove();
			frm.get_field("intro_html").$wrapper.append(
				`<div class="tm-from-file" style="margin-top:8px;color:var(--text-muted);font-size:var(--text-sm)">
					<b>${from_file}</b> of these ${rows.length} values already come from <code>site_config.json</code>
					on this site. Those fields read blank here because this page is only the fallback — editing them
					changes nothing until the server-side key is removed.</div>`
			);
		}

		wrapper.html(`
			<div style="overflow-x:auto">
			<table style="width:100%;border-collapse:collapse;font-size:var(--text-md)">
				<thead><tr style="text-align:left;color:var(--text-muted);font-size:var(--text-sm)">
					<th style="padding-bottom:6px">Setting</th><th>In effect</th><th>Coming from</th>
					<th>Override in site_config as</th>
				</tr></thead>
				<tbody>${body}</tbody>
			</table>
			<div class="text-muted" style="font-size:var(--text-sm);margin-top:10px">
				Saved values here take effect only where the matching <code>site_config.json</code> key is absent.
				Values shown reflect the last save, not unsaved edits.
			</div></div>`);
	});
}

function test_connection(frm) {
	frappe.dom.freeze(__("Asking the SMP…"));
	frappe.call({ method: "upande_tagmeter.settings.test_connection" })
		.then((r) => {
			const result = (r && r.message) || {};
			if (result.ok) {
				frappe.msgprint({
					title: __("Connected"), indicator: "green",
					message: frappe.utils.escape_html(result.message),
				});
			} else {
				frappe.msgprint({
					title: __("Could not connect"), indicator: "red",
					message: frappe.utils.escape_html(result.message),
				});
			}
		})
		.always(() => frappe.dom.unfreeze());
}
