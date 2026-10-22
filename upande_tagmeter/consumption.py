"""Consumption arithmetic. Frappe-free so the edge cases can be unit-tested."""


def consumption_delta(previous_m3: float | None, current_m3: float | None) -> float:
	"""Water consumed since the previous reading, in m3.

	Returns 0.0 when:

	- there is no previous reading. Treating a lifetime counter as one period's
	  consumption would be wrong, and the caller must key "is this the first
	  reading?" on the meter's ``last_reading`` link rather than on a stored
	  0.0, which is indistinguishable from a genuine zero.
	- the counter moved backwards, which means a meter replacement, a reset or
	  an out-of-order record. A negative delta is never real consumption.
	"""
	if previous_m3 is None or current_m3 is None:
		return 0.0
	delta = current_m3 - previous_m3
	return delta if delta >= 0 else 0.0


def hourly_volume(next_hour_difference: float | None) -> float:
	"""Volume through the meter in one hour of the SMP's 24-hour window, in m3.

	The vendor's ``NextHourDifference`` is NOT that hour's volume. It is this
	hour's counter minus the next hour's, so it is negative whenever water flows
	and it describes the hour that *follows* the row's timestamp. Measured on
	``68750000076912``, 2026-10-21:

	    11:00Z  TotalCounter 0.000  NextHourDifference -0.064
	    12:00Z  TotalCounter 0.064  NextHourDifference -0.036
	    13:00Z  TotalCounter 0.100

	So the volume for the hour starting at a row's timestamp is the negation.
	The last row's value even predicts the next counter: 0.102 at 09:00Z with
	-0.027, and the 09:15Z reading showed 0.129.

	A positive value would mean the counter fell, which is never real
	consumption, so it reads as zero -- the same rule as
	:func:`consumption_delta`.
	"""
	if next_hour_difference is None:
		return 0.0
	volume = round(-next_hour_difference, 6)
	return volume if volume > 0 else 0.0
