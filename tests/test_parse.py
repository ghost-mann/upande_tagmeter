"""Parsing real SMP bodies, including the timezone that cost us an hour to prove."""

from datetime import datetime, timezone

from tests.fixtures import vendor_responses as fx
from upande_tagmeter.vendor.parse import (
	METER_TZ,
	SERVER_TZ,
	is_valid_meter_sn,
	parse_amr,
	parse_gateway_status,
	parse_status_prefix,
	parse_vendor_datetime,
)


def test_meter_timestamps_are_utc():
	"""The meter's own clock, carried in the AMR frame, reads UTC.

	Measured: meter 68750000076929 reported "2026-09-09 13:09:35" while the
	wall clock was 13:09:11 UTC -- a live uplink 24 seconds "ahead" of UTC.
	Amsterdam would have put it at ~15:09. Treating these as Dutch local time
	shifts every reading two hours in summer and one in winter.
	"""
	assert METER_TZ == "UTC"
	assert parse_vendor_datetime("2026-08-13 09:54:06") == datetime(
		2026, 8, 13, 9, 54, 6, tzinfo=timezone.utc
	)


def test_server_generated_timestamps_are_amsterdam():
	"""get_gateway_status.statTime is stamped by the SMP, not the meter, and
	that one *is* Dutch local: 15:07:12 observed against 13:07:39 UTC."""
	assert SERVER_TZ == "Europe/Amsterdam"
	assert parse_vendor_datetime("2026-09-09 15:07:12", SERVER_TZ) == datetime(
		2026, 9, 9, 13, 7, 12, tzinfo=timezone.utc
	)


def test_server_timezone_follows_european_dst():
	"""January is CET (UTC+1), so the same wall clock is a different instant.
	A fixed offset would be wrong for half the year."""
	assert parse_vendor_datetime("2026-01-09 15:07:12", SERVER_TZ) == datetime(
		2026, 1, 9, 14, 7, 12, tzinfo=timezone.utc
	)


def test_gateway_status_is_normalised():
	body = {
		"code": 200, "gatewayID": "0c4ec0fffe00e97f", "online": True,
		"mqtt_protocol": True, "latitude": -1.2968242, "longitude": 36.7771465,
		"altitude": 1787, "gpsTimeSync": True, "statTime": "2026-09-09 15:07:12",
		"timezone": "Europe/Amsterdam",
	}
	out = parse_gateway_status(body)
	assert out["gateway_id"] == "0C4EC0FFFE00E97F"
	assert out["online"] is True
	assert out["stat_time"] == datetime(2026, 9, 9, 13, 7, 12, tzinfo=timezone.utc)


def test_unparseable_timestamp_returns_none_rather_than_raising():
	assert parse_vendor_datetime("not a date") is None
	assert parse_vendor_datetime(None) is None
	assert parse_vendor_datetime("") is None


def test_status_prefix_extraction():
	assert parse_status_prefix("08[Valve open;Low Balance Alarm;]") == 0x08
	assert parse_status_prefix("00[Valve open;]") == 0x00
	assert parse_status_prefix("02[Empty pipe alarm;]") == 0x02
	assert parse_status_prefix(None) is None
	assert parse_status_prefix("[no prefix]") is None


def test_parse_amr_on_the_frozen_meter():
	out = parse_amr(fx.AMR_0868)
	assert out["meter_sn"] == "68753500170868"
	assert out["dev_eui"] == "8cf957200020dafe"  # lowercased for consistency
	assert out["connection"] == "DN15"
	assert out["device_time"] == datetime(2026, 8, 13, 9, 54, 6, tzinfo=timezone.utc)
	assert out["remaining_balance_m3"] == 2.0
	assert out["valve_state"] == "Open"
	assert out["alarm_empty_pipe"] is True
	assert out["alarm_low_balance"] is False
	assert out["status_raw"] == "0002"
	assert out["status_text_mismatch"] == 0
	assert len(out["hourly"]) == 24


def test_parse_amr_on_the_recharged_meter():
	"""Balance 2.001 is the fingerprint of a 0.001 recharge that reached the meter."""
	out = parse_amr(fx.AMR_0871)
	assert out["remaining_balance_m3"] == 2.001
	assert out["alarm_low_balance"] is True
	assert out["valve_state"] == "Open"
	# 20 Aug 00:54:59 UTC, about 11h20m after the recharge was sent at
	# 13:36 GMT on the 19th. That latency is the Class B ping slot.
	assert out["device_time"] == datetime(2026, 8, 20, 0, 54, 59, tzinfo=timezone.utc)


def test_no_amr_record_is_not_an_error():
	"""A meter emitting only a 1-byte keepalive produces no AMR record."""
	assert parse_amr(fx.AMR_NO_RECORD) is None


def test_hourly_series_is_flattened_for_json_storage():
	rows = parse_amr(fx.AMR_0868)["hourly"]
	assert all(set(r) == {"timestamp", "total_counter", "delta"} for r in rows)
	assert rows[0]["timestamp"].endswith("+00:00")


def test_meter_sn_validation():
	assert is_valid_meter_sn("68753500170868")
	assert not is_valid_meter_sn("")
	assert not is_valid_meter_sn(None)
	assert not is_valid_meter_sn("6875350017086")     # 13 digits
	assert not is_valid_meter_sn("687535001708688")   # 15 digits
	assert not is_valid_meter_sn("6875350017086a")


# Captured live 2026-09-11. F04CD5 is genuinely down; 0C4EC0 serves all 100 meters.
GW_DOWN = {
	"code": 200, "gatewayID": "F04CD5FFFE01CF70", "online": False, "mqtt_protocol": True,
	"latitude": 0, "longitude": 0, "altitude": 0, "gpsTimeSync": False,
	"statTime": "2026-09-09 12:21:13", "timezone": "Europe/Amsterdam",
}
GW_UP = {
	"code": 200, "gatewayID": "0C4EC0FFFE00E97F", "online": True, "mqtt_protocol": True,
	"latitude": -1.2970528, "longitude": 36.7771403, "altitude": 1814, "gpsTimeSync": True,
	"statTime": "2026-09-11 10:23:06", "timezone": "Europe/Amsterdam",
}


def test_gateway_status_reports_offline():
	out = parse_gateway_status(GW_DOWN)
	assert out["gateway_id"] == "F04CD5FFFE01CF70"
	assert out["online"] is False
	assert out["gps_time_sync"] is False


def test_gateway_stat_time_is_amsterdam_not_utc():
	"""statTime is SMP-generated in Dutch local time -- 2h ahead of UTC in September."""
	out = parse_gateway_status(GW_UP)
	assert out["stat_time"].hour == 8
	assert out["stat_time"].tzinfo is not None


def test_gateway_without_a_gps_fix_reports_zeroes_not_none():
	out = parse_gateway_status(GW_DOWN)
	assert out["latitude"] == 0.0
	assert out["longitude"] == 0.0
