app_name = "upande_tagmeter"
app_title = "Upande TagMeter"
app_publisher = "ghost-mann"
app_description = "TagMeter smart water meter monitoring and valve control via the tagmeter.com SMP REST API."
app_email = "james@upande.com"
app_license = "mit"

# Roles this app expects. Created on install.
# ------------------------------------------
# v16 scopes the desk by app: without this the workspace has no owning app on
# the /apps screen, and the sidebar group reports app=None.
add_to_apps_screen = [
	{
		"name": "upande_tagmeter",
		"logo": "/assets/upande_tagmeter/images/upande-logo.png",
		"title": "TagMeter",
		"route": "/app/upande-tagmeter",
	}
]

after_install = "upande_tagmeter.install.after_install"
after_migrate = "upande_tagmeter.install.after_migrate"

scheduler_events = {
	"cron": {
		# Meters report roughly every 12 hours, so polling every four is ample
		# headroom while keeping the daily call count modest -- there is no bulk
		# read, so each sweep is one request per meter and the SMP's rate limit
		# is undocumented.
		"0 */4 * * *": ["upande_tagmeter.sync.sync_fleet"],
	},
	# Order here is a preference, not a guarantee. Frappe enqueues each entry
	# as its own background job, so with more than one worker they can and do
	# run concurrently. That is safe: these four passes share no state, and
	# refresh_link_states never reads the online flag refresh_online_flags
	# writes. The only cost of a violated order is one cycle of freshness --
	# a Gateway Down verdict computed against hour-old gateway rows, corrected
	# on the next run -- never a wrong answer.
	"hourly": [
		# Gateway health is the input to every "Gateway Down" verdict, so this
		# is listed first to give link states the freshest rows available.
		"upande_tagmeter.gateway.sync_gateways",
		# Expire overdue valve commands and re-send any the SMP never accepted.
		# Hourly rather than every few minutes: a command legitimately waits
		# hours on Class B, so checking more often would only add noise.
		"upande_tagmeter.valve.watchdog",
		# A meter that goes quiet produces no reading, so nothing would ever
		# clear its online flag. This pass is pure SQL and costs no API calls.
		"upande_tagmeter.sync.refresh_online_flags",
		# Diagnosis on top of detection. Best run after sync_gateways above; if
		# it wins the race instead, the verdict is merely an hour stale and the
		# next cycle corrects it. Also re-run at the end of sync_fleet, so the
		# 4-hourly sweep does not leave 100 fresh last_seen values paired with
		# hour-old link states.
		"upande_tagmeter.sync.refresh_link_states",
	],
}
