"""Decode the meter's two status bytes into a valve state and ten flags.

The SMP does not expose these bytes as numbers. It renders them as the numeric
prefix of two strings::

	"ValveStatus":   "08[Valve open;Low Balance Alarm;]"   -> ST1 = 0x08
	"AlarmMessage":  "02[Empty pipe alarm;]"               -> ST2 = 0x02

Both prefixes are confirmed against the TagMeter V0.2 bit tables: ST1 bit 3 is
Low Balance (0x08) and ST2 bit 1 is Empty Pipe (0x02). Decoding the byte rather
than matching the English text keeps this independent of the
``content-language`` header, which the vendor honours.
"""

from typing import Any

# ── Valve state, ST1 bits 0-1 ────────────────────────────────────────────────
# The vendor's own platform decodes 0x01 as "Valve closed". The TagMeter README
# writes this row as "00=Open, 10=Closed, 11=Error", which is ambiguous about
# bit order -- read bit-0-first, its "10" *is* 0x01. The vendor decodes its own
# meters, so its reading wins.
#
# Do not "correct" 0b01 to Transitioning. A sibling implementation did, and it
# reports every closed valve as Transitioning -- the most consequential field in
# the system, silently wrong.
VALVE_STATE_NAMES = {
	0b00: "Open",
	0b01: "Closed",
	0b10: "Unknown",  # never observed in any captured response
	0b11: "Error",
}

# Flag name -> (byte, bit). ST1 and ST2 both carry battery signals and they can
# disagree; collapsing them would hide a meter reporting inconsistently.
FLAG_BITS: dict[str, tuple[str, int]] = {
	"battery_low": ("st1", 2),
	"alarm_low_balance": ("st1", 3),
	"alarm_battery_meter": ("st2", 0),
	"alarm_empty_pipe": ("st2", 1),
	"alarm_reverse_flow": ("st2", 2),
	"alarm_overload_flow": ("st2", 3),
	"alarm_temp": ("st2", 4),
	"alarm_ee": ("st2", 5),
	"alarm_leak": ("st2", 6),
	"alarm_pipe_burst": ("st2", 7),
}

# Words the vendor uses for each flag, for the cross-check below.
FLAG_KEYWORDS: dict[str, str] = {
	"battery_low": "battery",
	"alarm_low_balance": "low balance",
	"alarm_battery_meter": "battery",
	"alarm_empty_pipe": "empty pipe",
	"alarm_reverse_flow": "reverse",
	"alarm_overload_flow": "overload",
	"alarm_temp": "temp",
	"alarm_ee": "ee",
	"alarm_leak": "leak",
	"alarm_pipe_burst": "burst",
}


def decode_status(st1: int, st2: int) -> dict[str, Any]:
	"""Two status bytes -> a 2-bit valve enum plus ten booleans."""
	valve_bits = st1 & 0b11
	decoded: dict[str, Any] = {
		"status_raw": f"{st1:02X}{st2:02X}",
		"valve_state_raw": valve_bits,
		"valve_state": VALVE_STATE_NAMES.get(valve_bits, "Unknown"),
	}
	for flag, (which, bit) in FLAG_BITS.items():
		byte = st1 if which == "st1" else st2
		decoded[flag] = bool((byte >> bit) & 1)
	return decoded


def status_agrees_with_text(decoded: dict[str, Any], *texts: str) -> bool:
	"""Does the byte-derived decode match the words the vendor sent?

	The prefix is two characters, so a byte is rendered the same in hex and
	decimal for every value below 0x0A -- and every response captured so far
	("00", "01", "02", "08") falls in that range. We parse as hex because a
	decimal byte would need three characters above 99, but that is inference,
	not documentation.

	This check makes a wrong guess loud instead of silent: if bit 4 (0x10) is
	ever set, hex says Temperature alarm and decimal says Empty pipe, and the
	two disagree about the vendor's own wording. Callers record the mismatch on
	the reading rather than discarding either interpretation.
	"""
	blob = " ".join(texts).lower()
	valve = str(decoded.get("valve_state", "")).lower()
	if valve in ("open", "closed") and valve not in blob:
		return False
	for flag, keyword in FLAG_KEYWORDS.items():
		if decoded.get(flag) and keyword not in blob:
			return False
	return True
