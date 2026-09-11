"""Client behaviour, driven entirely by recorded vendor responses."""

import pytest

from tests.fake_session import FakeSession
from tests.fixtures import vendor_responses as fx
from upande_tagmeter.vendor.client import MemoryTokenStore, TagMeterClient
from upande_tagmeter.vendor.errors import AuthFailed, ConfigError, InvalidMeterID, Outcome

BASE = "https://tagmeter.com/restapi/v2_0/002/api"


def make(script, **kwargs):
	kwargs.setdefault("retry_backoff", 0)  # no real sleeping in tests
	return TagMeterClient(BASE, "user", "pw", session=FakeSession(script), **kwargs)


# ── configuration ────────────────────────────────────────────────────────────

def test_missing_credentials_fail_loudly_at_construction():
	with pytest.raises(ConfigError):
		TagMeterClient(BASE, "", "pw")
	with pytest.raises(ConfigError):
		TagMeterClient("", "user", "pw")


# ── input validation, before anything is sent ────────────────────────────────

def test_blank_meter_sn_is_refused_before_any_request():
	"""The SMP answers {"meterID": ""} with code 200 "Operation success!".

	If we let that through, an empty-serial bug would look like a healthy read
	forever, so the guard has to be on our side of the wire.
	"""
	client = make([])
	with pytest.raises(InvalidMeterID):
		client.get_latest_amr("")
	assert client.session.calls == []  # nothing was sent


def test_malformed_meter_sn_is_refused():
	client = make([])
	for bad in ("6875350017086", "687535001708688", "abcdefghijklmn"):
		with pytest.raises(InvalidMeterID):
			client.get_latest_amr(bad)
	assert client.session.calls == []


# ── happy path ───────────────────────────────────────────────────────────────

def test_authenticates_once_then_reuses_the_token():
	client = make([fx.AUTH_OK, fx.AMR_0868, fx.AMR_0871])
	outcome, first = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.OK
	assert first["remaining_balance_m3"] == 2.0

	outcome, second = client.get_latest_amr("68753500170871")
	assert outcome is Outcome.OK
	assert second["remaining_balance_m3"] == 2.001

	# One auth for two reads -- minting a second token would revoke the first.
	assert client.session.endpoints == [
		"req_authorization_token", "get_latest_amr", "get_latest_amr"
	]


def test_token_is_sent_in_the_authorization_header():
	client = make([fx.AUTH_OK, fx.AMR_0868])
	client.get_latest_amr("68753500170868")
	assert client.session.calls[1]["headers"]["authorization"] == fx.AUTH_OK["authorization"]
	assert client.session.calls[1]["headers"]["content-language"] == "en"


def test_a_meter_with_no_amr_record_is_ok_with_no_data():
	client = make([fx.AUTH_OK, fx.AMR_NO_RECORD])
	outcome, data = client.get_latest_amr("68750000076929")
	assert outcome is Outcome.OK
	assert data is None


# ── the vendor's error taxonomy ──────────────────────────────────────────────

def test_http_200_with_code_500_is_not_success():
	"""Their HTTP status is always 200. Trusting it would treat every
	failure as a successful read."""
	client = make([fx.AUTH_OK, fx.ERR_500_UNKNOWN_METER])
	outcome, data = client.get_latest_amr("68753500179999")
	assert outcome is Outcome.UNKNOWN_METER
	assert data is None


def test_401_triggers_one_refresh_and_one_retry():
	client = make([fx.AUTH_OK, fx.ERR_401, fx.AUTH_OK, fx.AMR_0868])
	outcome, data = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.OK
	assert data["meter_sn"] == "68753500170868"
	assert client.session.endpoints == [
		"req_authorization_token", "get_latest_amr", "req_authorization_token", "get_latest_amr"
	]


def test_a_second_401_after_refresh_raises_rather_than_looping():
	"""Means the credentials are wrong, or a human took the session from the
	web console. Retrying would revoke tokens forever."""
	client = make([fx.AUTH_OK, fx.ERR_401, fx.AUTH_OK, fx.ERR_401])
	with pytest.raises(AuthFailed, match="service account"):
		client.get_latest_amr("68753500170868")


def test_transport_failure_is_distinct_from_a_rejection():
	client = make([fx.AUTH_OK, ConnectionError("connection reset")])
	outcome, data = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.TRANSPORT_ERROR
	assert data is None


def test_http_5xx_is_a_server_error_not_a_network_fault():
	"""Fifteen of the hundred Kiwasco serials reproducibly make the SMP return
	HTTP 500 with an empty body, at any request spacing. That is their app tier
	crashing on specific meters -- deterministic, so it must be recorded rather
	than retried."""
	client = make([fx.AUTH_OK, (None, 500)], max_attempts=3)
	outcome, data = client.get_latest_amr("68750000076884")
	assert outcome is Outcome.SERVER_ERROR
	assert data is None
	# One attempt only: no retry burned on a deterministic failure.
	assert client.session.endpoints == ["req_authorization_token", "get_latest_amr"]


def test_unparseable_body_is_bad_response_and_raw_is_kept():
	client = make([fx.AUTH_OK, (None, 200)])
	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.BAD_RESPONSE
	assert client.last_raw == "<not json>"


def test_unexpected_code_is_bad_response_not_silently_ok():
	client = make([fx.AUTH_OK, {"code": 418, "message": "teapot"}])
	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.BAD_RESPONSE


def test_bad_credentials_raise_at_authentication():
	client = make([{"code": 401, "message": "Invalid username or password"}])
	with pytest.raises(AuthFailed, match="refused the credentials"):
		client.get_latest_amr("68753500170868")


# ── single-flight refresh ────────────────────────────────────────────────────

def test_refresh_adopts_another_workers_token_instead_of_minting_a_second():
	"""The scenario that breaks a naive client.

	Worker A's token 401s. While A waits for the lock, worker B refreshes. A
	must adopt B's token -- if it authenticates too, it revokes B's and the two
	knock each other out on every cycle.
	"""
	store = MemoryTokenStore()
	store.set("token-A")
	client = make([fx.ERR_401, fx.AMR_0868], token_store=store)

	original_lock = store.lock

	def lock_and_simulate_other_worker():
		store.set("token-B-from-other-worker")
		return original_lock()

	store.lock = lock_and_simulate_other_worker

	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.OK
	# No re-authentication happened: B's token was reused.
	assert client.session.endpoints == ["get_latest_amr", "get_latest_amr"]
	assert store.get() == "token-B-from-other-worker"
	assert client.session.calls[1]["headers"]["authorization"] == "token-B-from-other-worker"


# ── V1 performs no writes ────────────────────────────────────────────────────

def test_the_read_path_refuses_every_write_endpoint():
	"""The read path retries on transport errors, which is wrong for a write --
	we could not tell whether the first attempt landed. So even valve_control,
	which IS supported, must not be reachable through call()."""
	client = make([fx.AUTH_OK])
	for endpoint in ("valve_control", "recharge_meter", "savewebhook"):
		with pytest.raises(ValueError, match="not a read endpoint"):
			client.call(endpoint, {"meterID": "68753500170871"})


# ── gateway ──────────────────────────────────────────────────────────────────

def test_gateway_status_normalises_the_id_to_uppercase():
	client = make([fx.AUTH_OK, fx.GATEWAY_ONLINE])
	outcome, body = client.get_gateway_status("0c4ec0fffe00e97f")
	assert outcome is Outcome.OK
	assert body["online"] is True
	assert client.session.calls[1]["body"] == {"gatewayID": "0C4EC0FFFE00E97F"}


# ── the vendor's edge blocks some clients ────────────────────────────────────

def test_an_explicit_user_agent_is_always_sent():
	"""The SMP edge 403s the default python-requests User-Agent with an HTML
	error page, before the API is reached. Letting requests use its own default
	makes every call fail with no useful diagnostic."""
	from upande_tagmeter.vendor.client import USER_AGENT

	client = make([fx.AUTH_OK, fx.AMR_0868])
	client.get_latest_amr("68753500170868")
	for call in client.session.calls:
		assert call["headers"]["User-Agent"] == USER_AGENT
	assert "python-requests" not in USER_AGENT


def test_non_json_403_raises_blocked_not_a_data_outcome():
	"""Their API always answers 200 with a JSON code. A non-JSON 4xx is the
	edge refusing us, which no credential change fixes -- so it must not be
	mistaken for an auth failure or a bad meter."""
	from upande_tagmeter.vendor.errors import BlockedByVendor

	client = make([(None, 403)])
	with pytest.raises(BlockedByVendor, match="client-fingerprint block"):
		client.get_latest_amr("68753500170868")


def test_blocked_reports_the_user_agent_that_was_refused():
	from upande_tagmeter.vendor.errors import BlockedByVendor

	client = make([(None, 403)])
	with pytest.raises(BlockedByVendor, match="upande-tagmeter"):
		client.get_latest_amr("68753500170868")


# ── retry policy ─────────────────────────────────────────────────────────────

def test_a_network_fault_is_retried_then_succeeds():
	"""The one genuinely transient class, so the only one worth retrying."""
	client = make([fx.AUTH_OK, ConnectionError("reset"), fx.AMR_0868], max_attempts=3)
	outcome, data = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.OK
	assert data["meter_sn"] == "68753500170868"
	assert client.session.endpoints.count("get_latest_amr") == 2


def test_retries_are_bounded():
	client = make([fx.AUTH_OK] + [ConnectionError("reset")] * 5, max_attempts=3)
	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.TRANSPORT_ERROR
	assert client.session.endpoints.count("get_latest_amr") == 3


def test_max_attempts_of_one_disables_retry():
	client = make([fx.AUTH_OK, ConnectionError("reset")], max_attempts=1)
	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.TRANSPORT_ERROR
	assert client.session.endpoints.count("get_latest_amr") == 1


def test_an_unknown_meter_is_not_retried():
	"""A body-level code 500 is an answer, not a failure to get one."""
	client = make([fx.AUTH_OK, fx.ERR_500_UNKNOWN_METER], max_attempts=3)
	outcome, _ = client.get_latest_amr("68753500179999")
	assert outcome is Outcome.UNKNOWN_METER
	assert client.session.endpoints.count("get_latest_amr") == 1


# ── the two ways a token dies ────────────────────────────────────────────────

def test_expired_token_is_refreshed_like_a_revoked_one():
	"""Tokens lapse after ~65 minutes with a *different* message from the one
	revocation gives. Both must refresh; the client never needs to tell them
	apart."""
	expired = {"code": 401, "message": "Authorization Token expired, Request New Authorization Token"}
	client = make([fx.AUTH_OK, expired, fx.AUTH_OK, fx.AMR_0868])
	outcome, data = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.OK
	assert data["meter_sn"] == "68753500170868"


def test_bare_empty_list_is_treated_as_unauthorized():
	"""get_customer_info answers a bare empty list -- HTTP 200, no code field --
	when the token has expired, while get_latest_amr answers a proper 401.
	Measured 2026-09-09. Classifying it as BAD_RESPONSE would skip the refresh
	and fail every call on that endpoint for an hour at a time."""
	client = make([fx.AUTH_OK, [], fx.AUTH_OK, fx.AMR_0868])
	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.OK
	assert client.session.endpoints == [
		"req_authorization_token", "get_latest_amr", "req_authorization_token", "get_latest_amr"
	]


def test_a_non_empty_list_is_still_a_bad_response():
	"""Only the empty list carries the expired-token meaning."""
	client = make([fx.AUTH_OK, [{"unexpected": True}]])
	outcome, _ = client.get_latest_amr("68753500170868")
	assert outcome is Outcome.BAD_RESPONSE


# ── writes: code is 200 either way, so the message is the only signal ────────

def test_a_write_success_is_recognised_from_the_message():
	client = make([fx.AUTH_OK, {"code": 200, "message": "Operation success!"}])
	outcome, body = client.set_valve("68753500170871", "Open")
	assert outcome is Outcome.OK
	assert client.session.calls[1]["body"] == {"meterID": "68753500170871", "forceValve": "Open"}


def test_a_write_rejection_is_not_mistaken_for_success():
	"""The SMP returns code 200 with "Invalid valve control command" when it
	refuses. Keying on code would treat a refused valve command as done."""
	client = make([fx.AUTH_OK, {"code": 200, "message": "Invalid valve control command"}])
	outcome, body = client.set_valve("68753500170871", "Close")
	assert outcome is Outcome.REJECTED
	assert body["message"] == "Invalid valve control command"


def test_an_unrecognised_write_message_is_treated_as_rejection():
	"""Never optimistically assume a write worked."""
	client = make([fx.AUTH_OK, {"code": 200, "message": "something new"}])
	outcome, _ = client.set_valve("68753500170871", "Open")
	assert outcome is Outcome.REJECTED


def test_writes_are_never_retried():
	"""A transport failure on a write leaves us unable to tell whether it
	reached their queue, so a blind retry could enqueue a second conflicting
	downlink. Retries live on the Meter Command document instead."""
	client = make([fx.AUTH_OK, ConnectionError("reset")], max_attempts=3)
	outcome, _ = client.set_valve("68753500170871", "Open")
	assert outcome is Outcome.TRANSPORT_ERROR
	assert client.session.endpoints.count("valve_control") == 1


def test_reset_is_not_offered_even_though_the_smp_accepts_it():
	"""Measured as accepted, undocumented, semantics unknown. Not exposed."""
	from upande_tagmeter.vendor.errors import UnsupportedValveAction

	client = make([])
	for action in ("Reset", "reset", "Prepaid", "open"):
		with pytest.raises(UnsupportedValveAction):
			client.set_valve("68753500170871", action)
	assert client.session.calls == []


def test_a_write_endpoint_cannot_be_called_through_the_read_path():
	client = make([fx.AUTH_OK])
	with pytest.raises(ValueError, match="not a read endpoint"):
		client.call("valve_control", {"meterID": "68753500170871", "forceValve": "Open"})


def test_a_read_endpoint_cannot_be_called_through_the_write_path():
	client = make([fx.AUTH_OK])
	with pytest.raises(ValueError, match="not a write endpoint"):
		client.call_write("get_latest_amr", {"meterID": "68753500170871"})


def test_recharge_and_savewebhook_remain_unreachable():
	"""Prepaid vending needs the credit ledger and tariff model that do not
	exist yet. Issuing credit with nowhere to account for it would be worse
	than not issuing it."""
	client = make([fx.AUTH_OK])
	for endpoint in ("recharge_meter", "savewebhook"):
		with pytest.raises(ValueError):
			client.call_write(endpoint, {"meterID": "68753500170871"})


def test_the_no_valve_refusal_is_a_rejection():
	"""The SMP has a distinct message for the 90 valveless meters:
	"Meter ID X has no valve, Valve Control Aborted". Still code 200, so it
	must not be read as success."""
	client = make([fx.AUTH_OK, {
		"code": 200,
		"message": "Meter ID 68750000076973 has no valve, Valve Control Aborted",
	}])
	outcome, body = client.set_valve("68750000076973", "Open")
	assert outcome is Outcome.REJECTED
	assert "no valve" in body["message"]
