"""Resolution precedence for every configurable value. No network."""

from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase

from upande_tagmeter import settings


def SPEC_CAST(key):
	return settings.SPEC[key][2]


@contextmanager
def conf(**overrides):
	"""Temporarily set (or clear, with None) keys in ``frappe.conf``."""
	original = {k: frappe.conf.get(k) for k in overrides}
	try:
		for key, value in overrides.items():
			if value is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value
		yield
	finally:
		for key, value in original.items():
			if value is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value


@contextmanager
def stored(**values):
	"""Temporarily write values into the TagMeter Settings single doc."""
	doc = frappe.get_doc("TagMeter Settings")
	original = {k: doc.get(k) for k in values}
	try:
		for key, value in values.items():
			doc.db_set(key, value, update_modified=False)
		frappe.clear_document_cache("TagMeter Settings", "TagMeter Settings")
		yield
	finally:
		for key, value in original.items():
			doc.db_set(key, value, update_modified=False)
		frappe.clear_document_cache("TagMeter Settings", "TagMeter Settings")


class TestSettingsResolution(IntegrationTestCase):
	def test_an_unconfigured_value_falls_back_to_the_builtin_default(self):
		with conf(tagmeter_expected_cycle_hours=None), stored(expected_cycle_hours=None):
			self.assertEqual(settings.get("expected_cycle_hours"), 24.0)
			self.assertEqual(settings.source("expected_cycle_hours"), "default")

	def test_the_settings_page_supplies_a_value_site_config_does_not_have(self):
		with conf(tagmeter_expected_cycle_hours=None), stored(expected_cycle_hours=12):
			self.assertEqual(settings.get("expected_cycle_hours"), 12.0)
			self.assertEqual(settings.source("expected_cycle_hours"), "settings")

	def test_site_config_beats_the_settings_page(self):
		"""The whole point of the ordering: a server-side config is authoritative.

		If this ever inverts, an operator with System Manager could silently
		override something an administrator pinned in a file only root can write.
		"""
		with conf(tagmeter_expected_cycle_hours=36), stored(expected_cycle_hours=12):
			self.assertEqual(settings.get("expected_cycle_hours"), 36.0)
			self.assertEqual(settings.source("expected_cycle_hours"), "site_config")

	def test_every_spec_key_resolves_without_raising(self):
		"""Guards against a SPEC entry whose fieldname does not exist on the doctype.

		``doc.get`` on an unknown field returns None rather than raising, so a
		typo would silently pin that setting to its default forever.
		"""
		fields = {f.fieldname for f in frappe.get_meta("TagMeter Settings").fields}
		for key in settings.SPEC:
			with self.subTest(key=key):
				self.assertIn(key, fields, f"{key} is in SPEC but not on the doctype")
				settings.get(key)

	def test_a_value_that_cannot_be_cast_falls_back_instead_of_breaking(self):
		with conf(tagmeter_expected_cycle_hours="not a number"):
			self.assertEqual(settings.get("expected_cycle_hours"), 24.0)

	def test_zero_is_treated_as_unset_not_as_a_choice(self):
		"""Frappe stores an untouched Float or Int field as 0, not NULL.

		Without this, a settings doc nobody has ever edited pins the whole app
		to a 0-hour reporting cycle, 0 attempts per call and a 0-second timeout
		— every one of which is worse than the default it replaced.
		"""
		with conf(tagmeter_expected_cycle_hours=None), stored(expected_cycle_hours=0):
			self.assertEqual(settings.get("expected_cycle_hours"), 24.0)
			self.assertEqual(settings.source("expected_cycle_hours"), "default")

	def test_zero_in_site_config_is_also_treated_as_unset(self):
		"""Both layers must agree on what a zero means."""
		with conf(tagmeter_max_attempts=0), stored(max_attempts=None):
			self.assertEqual(settings.get("max_attempts"), 3)
			self.assertEqual(settings.source("max_attempts"), "default")

	def test_a_zeroed_field_does_not_mask_a_real_setting_below_it(self):
		"""site_config unset, settings zeroed -> the default, not the zero."""
		with conf(tagmeter_min_interval=None), stored(min_interval=0):
			self.assertEqual(settings.get("min_interval"), 0.25)

	def test_the_defaults_a_fresh_install_actually_gets(self):
		"""A site that has never opened the page must behave exactly as the code
		did before the page existed."""
		blanks = {k: (0 if SPEC_CAST(k) is not str else "") for k in settings.SPEC}
		with conf(**{settings.SPEC[k][0]: None for k in settings.SPEC}), stored(**blanks):
			for key, (_conf_key, default, _cast) in settings.SPEC.items():
				with self.subTest(key=key):
					self.assertEqual(settings.get(key), default)

	def test_a_blank_string_is_treated_as_unset(self):
		"""An emptied Data field stores "" , not NULL, and must not win."""
		with conf(tagmeter_meter_timezone=None), stored(meter_timezone=""):
			self.assertEqual(settings.get("meter_timezone"), "UTC")
			self.assertEqual(settings.source("meter_timezone"), "default")

	def test_the_password_is_read_through_the_decrypting_accessor(self):
		"""``doc.get("api_password")`` returns the ciphertext, not the password.

		Reading it the wrong way produces a client that authenticates with an
		encrypted blob and fails as "bad credentials", which is a long way from
		the actual cause.
		"""
		doc = frappe.get_doc("TagMeter Settings")
		original = doc.get_password("api_password", raise_exception=False)
		try:
			doc.api_password = "s3cret-under-test"
			doc.save(ignore_permissions=True)
			frappe.clear_document_cache("TagMeter Settings", "TagMeter Settings")
			with conf(tagmeter_api_password=None):
				self.assertEqual(settings.get("api_password"), "s3cret-under-test")
		finally:
			doc = frappe.get_doc("TagMeter Settings")
			doc.api_password = original
			doc.save(ignore_permissions=True)
			frappe.clear_document_cache("TagMeter Settings", "TagMeter Settings")

	def test_the_url_is_returned_without_a_trailing_slash(self):
		with conf(tagmeter_api_url="https://example.test/api/"):
			url, _user, _password = settings.credentials()
			self.assertEqual(url, "https://example.test/api")


class TestSettingsConsumers(IntegrationTestCase):
	"""The modules that used to read frappe.conf directly now go through here."""

	def test_the_offline_cutoff_follows_the_configured_hours(self):
		from datetime import timedelta

		from frappe.utils import now_datetime

		from upande_tagmeter import sync

		with conf(tagmeter_offline_after_hours=5):
			gap = now_datetime() - sync._offline_cutoff()
			self.assertAlmostEqual(gap.total_seconds(), timedelta(hours=5).total_seconds(), delta=5)

	def test_the_gateway_cutoff_follows_the_configured_hours(self):
		from datetime import timedelta

		from frappe.utils import now_datetime

		from upande_tagmeter import gateway

		with conf(gateway_stale_after_hours=2):
			gap = now_datetime() - gateway.stale_cutoff()
			self.assertAlmostEqual(gap.total_seconds(), timedelta(hours=2).total_seconds(), delta=5)

	def test_the_module_constants_match_the_spec_defaults(self):
		"""One source of truth: what the page offers and what the code assumes."""
		from upande_tagmeter import gateway, sync, valve

		self.assertEqual(sync.EXPECTED_CYCLE_HOURS, settings.SPEC["expected_cycle_hours"][1])
		self.assertEqual(sync.LATE_MULTIPLIER, settings.SPEC["late_multiplier"][1])
		self.assertEqual(sync.SILENT_MULTIPLIER, settings.SPEC["silent_multiplier"][1])
		self.assertEqual(sync.DEFAULT_OFFLINE_AFTER_HOURS, settings.SPEC["offline_after_hours"][1])
		self.assertEqual(gateway.GATEWAY_STALE_AFTER_HOURS, settings.SPEC["gateway_stale_after_hours"][1])
		self.assertEqual(valve.VALVE_TIMEOUT_MINUTES, settings.SPEC["valve_timeout_minutes"][1])
		self.assertEqual(valve.DUPLICATE_WINDOW_SECONDS, settings.SPEC["duplicate_window_seconds"][1])


class TestSettingsValidation(IntegrationTestCase):
	def test_a_url_without_a_scheme_is_refused(self):
		doc = frappe.get_doc("TagMeter Settings")
		doc.api_url = "tagmeter.com/restapi"
		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_an_unknown_timezone_is_refused(self):
		doc = frappe.get_doc("TagMeter Settings")
		doc.meter_timezone = "Mars/Olympus"
		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_late_must_come_before_silent(self):
		doc = frappe.get_doc("TagMeter Settings")
		doc.late_multiplier = 4
		doc.silent_multiplier = 3
		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_an_expiry_shorter_than_the_measured_round_trip_is_refused(self):
		"""~15s to actuate plus ~2m04s of ingestion lag; under 3 minutes expires
		commands that are still perfectly in flight."""
		doc = frappe.get_doc("TagMeter Settings")
		doc.valve_timeout_minutes = 1
		self.assertRaises(frappe.ValidationError, doc.validate)


class TestEffectiveReport(IntegrationTestCase):
	def test_the_report_covers_every_setting_and_never_leaks_the_password(self):
		frappe.set_user("Administrator")
		rows = {row["key"]: row for row in settings.effective()}
		self.assertEqual(set(rows), set(settings.SPEC))
		self.assertIn(rows["api_password"]["value"], ("set", "not set"))
		self.assertEqual(rows["api_password"]["default"], "hidden")

	def test_the_thresholds_turn_multipliers_into_hours(self):
		frappe.set_user("Administrator")
		with conf(tagmeter_expected_cycle_hours=24, tagmeter_late_multiplier=None,
		          tagmeter_silent_multiplier=None), \
		     stored(late_multiplier=None, silent_multiplier=None):
			out = settings.thresholds()
		self.assertEqual(out["late_after_hours"], 30.0)
		self.assertEqual(out["silent_after_hours"], 72.0)


class TestConnectionProbe(IntegrationTestCase):
	"""The Test Connection button, against a stub.

	Never exercised against the live SMP: the platform issues one token at a
	time and a second consumer evicts the first, so a test that really called
	out would knock whichever site currently owns the connection off its token.
	"""

	def setUp(self):
		frappe.set_user("Administrator")
		# Same ordering as the probe itself, or the assertion tests the query
		# rather than the behaviour.
		self.meter = frappe.db.get_value(
			"Water Meter", {"status": ("!=", "Decommissioned")}, "name", order_by="last_seen desc"
		)

	@contextmanager
	def _client(self, result=None, raises=None):
		from upande_tagmeter import sync

		class Fake:
			def __init__(self):
				self.asked = []

			def get_latest_amr(inner, meter_sn):
				inner.asked.append(meter_sn)
				if raises:
					raise raises
				return result

		fake = Fake()
		original = sync.get_client
		sync.get_client = lambda: fake
		try:
			yield fake
		finally:
			sync.get_client = original

	def test_a_successful_read_reports_the_url_and_user(self):
		from upande_tagmeter.vendor.errors import Outcome

		if not self.meter:
			self.skipTest("no meter on this site to probe")
		with self._client(result=(Outcome.OK, {})) as fake:
			out = settings.test_connection()
		self.assertTrue(out["ok"])
		self.assertEqual(fake.asked, [self.meter])

	def test_an_unknown_meter_still_counts_as_connected(self):
		"""The SMP had to accept the token before it could say it has no record."""
		from upande_tagmeter.vendor.errors import Outcome

		if not self.meter:
			self.skipTest("no meter on this site to probe")
		with self._client(result=(Outcome.UNKNOWN_METER, None)):
			out = settings.test_connection()
		self.assertTrue(out["ok"])
		self.assertIn("no reading", out["message"])

	def test_a_refusal_is_reported_rather_than_raised(self):
		from upande_tagmeter.vendor.errors import AuthFailed

		if not self.meter:
			self.skipTest("no meter on this site to probe")
		with self._client(raises=AuthFailed("token rejected twice")):
			out = settings.test_connection()
		self.assertFalse(out["ok"])
		self.assertIn("token rejected twice", out["message"])

	def test_the_probe_is_read_only(self):
		"""A connection test must never actuate a valve.

		Guarded by a test because the failure mode is a real valve moving on a
		production water network, not a red bar in CI.
		"""
		from upande_tagmeter.vendor.errors import Outcome

		if not self.meter:
			self.skipTest("no meter on this site to probe")

		class Tripwire:
			def get_latest_amr(self, meter_sn):
				return (Outcome.OK, {})

			def set_valve(self, *a, **kw):
				raise AssertionError("test_connection must not write to the SMP")

			def call_write(self, *a, **kw):
				raise AssertionError("test_connection must not write to the SMP")

		from upande_tagmeter import sync

		original = sync.get_client
		sync.get_client = lambda: Tripwire()
		try:
			self.assertTrue(settings.test_connection()["ok"])
		finally:
			sync.get_client = original
