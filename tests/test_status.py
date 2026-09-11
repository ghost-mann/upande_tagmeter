"""Status-byte decode, pinned to the vendor's own rendering of the same bytes."""

from upande_tagmeter.vendor.status import decode_status, status_agrees_with_text


def test_valve_closed_is_bit_zero_not_bit_one():
	"""The vendor renders a closed valve as 0x01.

	A sibling implementation maps 0b01 to "Transitioning" and 0b10 to "Closed",
	which would report every closed valve as Transitioning. This test exists to
	stop that mapping being reintroduced.
	"""
	assert decode_status(0x01, 0x00)["valve_state"] == "Closed"
	assert decode_status(0x00, 0x00)["valve_state"] == "Open"
	assert decode_status(0x03, 0x00)["valve_state"] == "Error"


def test_low_balance_is_st1_bit_three():
	decoded = decode_status(0x08, 0x00)
	assert decoded["alarm_low_balance"] is True
	assert decoded["valve_state"] == "Open"  # bits 0-1 clear


def test_empty_pipe_is_st2_bit_one():
	assert decode_status(0x00, 0x02)["alarm_empty_pipe"] is True


def test_the_two_battery_bits_stay_separate():
	"""ST1 bit 2 and ST2 bit 0 can disagree; collapsing them hides that."""
	decoded = decode_status(0x04, 0x00)
	assert decoded["battery_low"] is True
	assert decoded["alarm_battery_meter"] is False


def test_all_ten_flags_and_the_valve_enum_are_reachable():
	decoded = decode_status(0xFF, 0xFF)
	flags = [k for k, v in decoded.items() if v is True]
	assert len(flags) == 10
	assert decoded["valve_state"] == "Error"


def test_status_raw_is_preserved_for_forensics():
	assert decode_status(0x08, 0x02)["status_raw"] == "0802"


def test_agreement_with_real_vendor_strings():
	assert status_agrees_with_text(
		decode_status(0x08, 0x02), "08[Valve open;Low Balance Alarm;]", "02[Empty pipe alarm;]"
	)
	assert status_agrees_with_text(decode_status(0x01, 0x00), "01[Valve closed;]", "")


def test_disagreement_is_detected():
	"""If our byte says closed and the vendor says open, say so."""
	assert not status_agrees_with_text(decode_status(0x01, 0x00), "01[Valve open;]", "")
