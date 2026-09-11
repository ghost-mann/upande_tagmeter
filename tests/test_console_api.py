"""Console API client, driven entirely by recorded responses. No network."""

import json
from io import BytesIO

import pytest

from upande_tagmeter.vendor.console_api import ConsoleClient
from upande_tagmeter.vendor.errors import AuthFailed, ConfigError, Outcome

BASE = "http://iotcloud.tagmeter.com:8099/prod-api"

# Captured 2026-09-11 from the live platform.
LOGIN_OK = {"code": 200, "msg": "Operation success!", "data": {"flag": True, "data": "JWT-AAA"}}
LOGIN_OK_2 = {"code": 200, "msg": "Operation success!", "data": {"flag": True, "data": "JWT-BBB"}}
LOGIN_BAD = {"msg": "Username/password cannot be empty!", "code": 500}
UNAUTHORIZED = {"msg": "Login has expired. Please log out and log in again!", "code": 401}

# Two real rows ten seconds apart -- the proof that back-to-back commands work.
HISTORY = {"code": 200, "msg": "Operation success!", "data": {"pageNum": 1, "pageSize": 50, "total": 54, "rows": [
	{"address": "68753500170871", "meterTime": "2026-09-11 11:28:46",
	 "createTime": "2026-09-11 11:30:50", "valveStatus": "08",
	 "valveStatusName": "08[Valve open;]", "rssi": -37, "snr": 11, "balance": 2.001},
	{"address": "68753500170871", "meterTime": "2026-09-11 11:28:36",
	 "createTime": "2026-09-11 11:30:40", "valveStatus": "08",
	 "valveStatusName": "08[Valve open;]", "rssi": -36, "snr": 9, "balance": 2.001},
]}}


class FakeOpener:
	"""Replays queued payloads and records every request it was handed."""

	def __init__(self, *script):
		self.script = list(script)
		self.calls = []

	def __call__(self, request, timeout=None):
		self.calls.append({
			"url": request.full_url,
			"method": request.get_method(),
			"token": request.headers.get("Token") or request.headers.get("token"),
			"body": json.loads(request.data) if request.data else None,
		})
		if not self.script:
			raise AssertionError(f"unexpected extra call to {request.full_url}")
		payload = self.script.pop(0)
		return _Response(payload)

	@property
	def paths(self):
		return [c["url"].replace(BASE, "") for c in self.calls]


class _Response(BytesIO):
	def __init__(self, payload):
		super().__init__(json.dumps(payload).encode())

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False


def make(*script):
	return ConsoleClient(BASE, "Upande", "pw", opener=FakeOpener(*script))


# ── configuration ────────────────────────────────────────────────────────────

def test_missing_credentials_fail_at_construction():
	with pytest.raises(ConfigError):
		ConsoleClient(BASE, "", "pw")
	with pytest.raises(ConfigError):
		ConsoleClient("", "Upande", "pw")


# ── auth ─────────────────────────────────────────────────────────────────────

def test_the_token_is_read_from_data_dot_data():
	"""Not data.token. A wrong guess here reads as a successful login that
	silently yields nothing, which is how it first presented when probed."""
	client = make(LOGIN_OK)
	assert client.authenticate() == "JWT-AAA"


def test_a_refused_login_raises_rather_than_returning_empty():
	client = make(LOGIN_BAD)
	with pytest.raises(AuthFailed, match="refused the credentials"):
		client.authenticate()


def test_a_login_with_no_token_in_it_raises():
	client = make({"code": 200, "msg": "Operation success!", "data": {"flag": True}})
	with pytest.raises(AuthFailed, match="no token at data.data"):
		client.authenticate()


def test_the_token_is_sent_as_a_token_header_not_authorization():
	client = make(LOGIN_OK, HISTORY)
	client.reading_history("68753500170871")
	assert client._opener.calls[-1]["token"] == "JWT-AAA"


def test_authenticates_once_then_reuses_the_token():
	client = make(LOGIN_OK, HISTORY, HISTORY)
	client.reading_history("68753500170871")
	client.reading_history("68753500170871")
	assert client._opener.paths.count("/login") == 1


def test_a_401_triggers_one_refresh_and_one_retry():
	client = make(LOGIN_OK, UNAUTHORIZED, LOGIN_OK_2, HISTORY)
	outcome, rows = client.reading_history("68753500170871")
	assert outcome is Outcome.OK
	assert len(rows) == 2
	assert client._opener.calls[-1]["token"] == "JWT-BBB"


def test_a_second_401_after_refresh_raises_rather_than_looping():
	client = make(LOGIN_OK, UNAUTHORIZED, LOGIN_OK_2, UNAUTHORIZED)
	with pytest.raises(AuthFailed, match="service account"):
		client.reading_history("68753500170871")


# ── reads ────────────────────────────────────────────────────────────────────

def test_history_returns_rows_newest_first_with_both_timestamps():
	client = make(LOGIN_OK, HISTORY)
	outcome, rows = client.reading_history("68753500170871")
	assert outcome is Outcome.OK
	assert rows[0]["meterTime"] == "2026-09-11 11:28:46"
	assert rows[0]["createTime"] == "2026-09-11 11:30:50"
	assert rows[0]["valveStatusName"] == "08[Valve open;]"


def test_the_ingestion_lag_is_visible_in_the_history():
	"""The whole reason this client exists: the documented API exposes only
	createTime, so its "last reported" is really "last ingested"."""
	from datetime import datetime

	client = make(LOGIN_OK, HISTORY)
	_, rows = client.reading_history("68753500170871")
	fmt = "%Y-%m-%d %H:%M:%S"
	for row in rows:
		lag = datetime.strptime(row["createTime"], fmt) - datetime.strptime(row["meterTime"], fmt)
		assert 60 <= lag.total_seconds() <= 240, "measured at ~2m04s on every sampled row"


def test_two_readings_ten_seconds_apart_are_representable():
	"""Rapid toggling is real: these two rows are from one console session."""
	from datetime import datetime

	client = make(LOGIN_OK, HISTORY)
	_, rows = client.reading_history("68753500170871")
	fmt = "%Y-%m-%d %H:%M:%S"
	gap = datetime.strptime(rows[0]["meterTime"], fmt) - datetime.strptime(rows[1]["meterTime"], fmt)
	assert gap.total_seconds() == 10


def test_history_sends_the_address_and_paging_the_console_sends():
	client = make(LOGIN_OK, HISTORY)
	client.reading_history("68753500170871", page=2, page_size=25)
	body = client._opener.calls[-1]["body"]
	assert body["data"]["address"] == "68753500170871"
	assert body["pageInfo"] == {"pageNum": 2, "pageSize": 25, "total": 0}


def test_meter_detail_is_a_get_with_the_serial_in_the_path():
	detail = {"code": 200, "msg": "Operation success!", "data": {"basicInfo": {"withValve": "Y"}}}
	client = make(LOGIN_OK, detail)
	outcome, data = client.meter_detail("68753500170871")
	assert outcome is Outcome.OK
	assert data["basicInfo"]["withValve"] == "Y"
	assert client._opener.calls[-1]["method"] == "GET"
	assert client._opener.paths[-1] == "/meter/getMeterDetailInfo/68753500170871"


def test_a_non_200_code_is_not_mistaken_for_data():
	client = make(LOGIN_OK, {"code": 500, "msg": "Freeze Records meterID NOT fetched"})
	outcome, rows = client.reading_history("68753500170871")
	assert outcome is Outcome.BAD_RESPONSE
	assert rows == []


# ── the line this client must not cross ──────────────────────────────────────

def test_the_client_exposes_no_write_endpoint():
	"""updateValveStatus is deliberately absent. Mapping its numeric valveFlag
	means actuating live valves on a production water network, and the
	documented valve_control is already measured across 43 probed values."""
	surface = {name for name in dir(ConsoleClient) if not name.startswith("_")}
	assert surface == {"authenticate", "call", "reading_history", "meter_detail"}
	source = __import__("upande_tagmeter.vendor.console_api", fromlist=["x"]).__file__
	with open(source) as handle:
		body = handle.read()
	assert "updateValveStatus" not in body.split('"""', 2)[2], "no write path in the code"
