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
