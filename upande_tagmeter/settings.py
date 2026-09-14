"""One place that answers "what is this value set to, and where did it come from".

Every tuneable in this app used to be read straight from ``frappe.conf`` at its
point of use, each with its own inline default. That is fine until someone has
to configure a site and cannot edit ``site_config.json`` -- a real situation on
hosted Frappe, where an operator can hold System Manager and still have no way
to write a server file.

So values resolve in three steps:

1. ``site_config.json`` -- authoritative. A site configured server-side behaves
   exactly as it did before this module existed.
2. **TagMeter Settings** -- the desk page, for sites where step 1 is unreachable.
3. The built-in default, which is what the code used all along.

Keeping the order that way round matters: adding a settings page must never
change a working deployment, and an operator must never be able to quietly
override something an administrator pinned in the server config.
"""

import frappe

from upande_tagmeter.vendor.console_api import DEFAULT_BASE_URL as CONSOLE_BASE_URL

SETTINGS_DOCTYPE = "TagMeter Settings"

# fieldname -> (site_config key, default, caster)
#
# The defaults are the measured ones, not round numbers. Where a value came
# from an observation rather than a guess, the settings page says so in the
# field description, so nobody has to dig through git history to find out why
# the staleness window is 30 hours and not 24.
SPEC = {
	"api_url":                  ("tagmeter_api_url",             None,  str),
	"api_user":                 ("tagmeter_api_user",            None,  str),
	"api_password":             ("tagmeter_api_password",        None,  str),
	"console_url":              ("tagmeter_console_url", CONSOLE_BASE_URL, str),

	"min_interval":             ("tagmeter_min_interval",        0.25,  float),
	"request_timeout":          ("tagmeter_request_timeout",     30,    int),
	"max_attempts":             ("tagmeter_max_attempts",        3,     int),
	"retry_backoff":            ("tagmeter_retry_backoff",       1.0,   float),
	"meter_timezone":           ("tagmeter_meter_timezone",      "UTC", str),

	"offline_after_hours":      ("tagmeter_offline_after_hours", 30.0,  float),
	"expected_cycle_hours":     ("tagmeter_expected_cycle_hours", 24.0, float),
	"late_multiplier":          ("tagmeter_late_multiplier",     1.25,  float),
	"silent_multiplier":        ("tagmeter_silent_multiplier",   3.0,   float),

	"gateway_stale_after_hours": ("gateway_stale_after_hours",   6.0,   float),

	"valve_timeout_minutes":    ("tagmeter_valve_timeout_minutes", 10.0, float),
	"duplicate_window_seconds": ("tagmeter_duplicate_window_seconds", 30, int),
}

SECRETS = {"api_password"}


def _unset(raw, cast) -> bool:
	"""Is this value absent rather than chosen?

	Frappe stores an untouched Float or Int field as ``0``, not NULL, so "the
	operator left it alone" and "the operator asked for zero" arrive at the
	database looking identical. Zero is not a usable setting for any number
	here -- a 0-hour reporting cycle, zero attempts per call, a zero-second
	timeout and an unthrottled sweep against an undocumented rate limit are all
	either meaningless or actively harmful -- so zero is read as "not set".

	Applied to site_config too, for the same reason and so the two layers
	cannot disagree about what a zero means.
	"""
	if raw is None or raw == "":
		return True
	if cast is str:
		return False
	try:
		return float(raw) <= 0
	except (TypeError, ValueError):
		return False


def _doc():
	"""The settings document, or None if it cannot be read.

	Returning None rather than raising matters during ``bench migrate``: the
	doctype may not exist yet while other code is already asking for values.
	"""
	try:
		return frappe.get_cached_doc(SETTINGS_DOCTYPE)
	except Exception:
		return None


def source(key: str) -> str:
	"""Where ``key`` is actually coming from: site_config, settings, or default.

	Exposed because "the value is 30" is only half an answer when three layers
	can supply it, and the layer decides who can change it.
	"""
	conf_key, _default, cast = SPEC[key]
	if not _unset(frappe.conf.get(conf_key), cast):
		return "site_config"
	doc = _doc()
	if doc is not None:
		raw = doc.get_password(key, raise_exception=False) if key in SECRETS else doc.get(key)
		if not _unset(raw, cast):
			return "settings"
	return "default"


def get(key: str):
	"""Resolved value for ``key``: site_config, then settings, then default."""
	conf_key, default, cast = SPEC[key]

	raw = frappe.conf.get(conf_key)
	if _unset(raw, cast):
		doc = _doc()
		raw = None
		if doc is not None:
			raw = doc.get_password(key, raise_exception=False) if key in SECRETS else doc.get(key)
	if _unset(raw, cast):
		return default

	try:
		return cast(raw.strip()) if isinstance(raw, str) and cast is not str else cast(raw)
	except (TypeError, ValueError):
		# A typo in one field must not take the whole app down; fall back and
		# say so, since a silently ignored setting is worse than a loud one.
		frappe.log_error(
			f"TagMeter setting {key!r} is {raw!r}, which is not a valid "
			f"{cast.__name__}. Using the default {default!r}.",
			"TagMeter settings",
		)
		return default


def credentials():
	"""``(url, user, password)`` -- the three that decide whether anything works."""
	url = get("api_url")
	return (url.rstrip("/") if url else None), get("api_user"), get("api_password")


@frappe.whitelist()
def effective() -> list[dict]:
	"""Every setting, its resolved value and which layer supplied it.

	Rendered on the settings page. Secrets report whether they are set, never
	what they are.
	"""
	frappe.only_for("System Manager")
	out = []
	for key in SPEC:
		value = get(key)
		out.append({
			"key": key,
			"source": source(key),
			"value": ("set" if value else "not set") if key in SECRETS else value,
			"default": "hidden" if key in SECRETS else SPEC[key][1],
			"site_config_key": SPEC[key][0],
		})
	return out


@frappe.whitelist()
def thresholds() -> dict:
	"""The staleness numbers the multipliers actually produce.

	``Late at 1.25x`` is not a number anyone can picture. This turns the
	multipliers into the hours they mean, so the settings page can show the
	consequence of a change rather than only its input.
	"""
	frappe.only_for("System Manager")
	cycle = get("expected_cycle_hours")
	return {
		"cycle_hours": cycle,
		"late_after_hours": round(cycle * get("late_multiplier"), 2),
		"silent_after_hours": round(cycle * get("silent_multiplier"), 2),
		"offline_after_hours": get("offline_after_hours"),
		"gateway_stale_after_hours": get("gateway_stale_after_hours"),
		"valve_timeout_minutes": get("valve_timeout_minutes"),
	}


@frappe.whitelist()
def test_connection() -> dict:
	"""Prove the credentials work, without disturbing anything that is running.

	Read-only by construction: it asks for one meter's latest reading. The SMP
	issues one token at a time and a fresh login revokes the previous one, so
	this deliberately goes through the shared token store rather than
	authenticating outright -- a button that logged in would knock the running
	fleet sweep off its token every time someone pressed it.
	"""
	frappe.only_for("System Manager")

	from upande_tagmeter.sync import get_client
	from upande_tagmeter.vendor.errors import AuthFailed, BlockedByVendor, ConfigError, Outcome

	url, user, _password = credentials()
	try:
		client = get_client()
	except ConfigError as exc:
		return {"ok": False, "message": str(exc)}

	probe = frappe.db.get_value(
		"Water Meter", {"status": ("!=", "Decommissioned")}, "name", order_by="last_seen desc"
	)
	if not probe:
		return {
			"ok": False,
			"message": (
				"The credentials look complete, but there is no meter to test them "
				"against yet. Run a fleet sync first, or add one Water Meter."
			),
		}

	try:
		outcome, _data = client.get_latest_amr(probe)
	except AuthFailed as exc:
		return {"ok": False, "message": f"{user} was refused by {url}. {exc}"}
	except BlockedByVendor as exc:
		return {"ok": False, "message": f"The SMP's front door refused the request. {exc}"}
	except Exception as exc:
		return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

	if outcome in (Outcome.OK, Outcome.UNKNOWN_METER):
		# UNKNOWN_METER still proves the credentials: the SMP had to accept the
		# token before it could tell us it holds no record for that serial.
		detail = (
			f"read meter {probe}" if outcome is Outcome.OK
			else f"authenticated, though the SMP holds no reading for {probe} yet"
		)
		return {"ok": True, "message": f"Connected to {url} as {user} and {detail}."}

	return {
		"ok": False,
		"message": f"Connected to {url}, but the call for {probe} ended as {outcome.value}.",
	}
