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
		"logo": "/assets/frappe/images/frappe-framework-logo.svg",
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
	"hourly": [
		# Expire overdue valve commands and re-send any the SMP never accepted.
		# Hourly rather than every few minutes: a command legitimately waits
		# hours on Class B, so checking more often would only add noise.
		"upande_tagmeter.valve.watchdog",
		# A meter that goes quiet produces no reading, so nothing would ever
		# clear its online flag. This pass is pure SQL and costs no API calls.
		"upande_tagmeter.sync.refresh_online_flags",
	],
}
