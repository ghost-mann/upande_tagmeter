from upande_tagmeter.consumption import consumption_delta, hourly_volume


def test_hourly_volume_negates_the_vendors_next_hour_difference():
	# Live 68750000076912, 2026-10-21: counter 0.000 -> 0.064 -> 0.100, which
	# the SMP ships as -0.064 and -0.036.
	assert hourly_volume(-0.064) == 0.064
	assert hourly_volume(-0.036) == 0.036


def test_hourly_volume_is_zero_for_no_flow_missing_or_a_falling_counter():
	assert hourly_volume(0) == 0.0
	assert hourly_volume(None) == 0.0
	assert hourly_volume(0.05) == 0.0


def test_consumption_delta_still_ignores_a_counter_reset():
	assert consumption_delta(900.0, 0.0) == 0.0
	assert consumption_delta(None, 5.0) == 0.0
	assert consumption_delta(1.0, 1.5) == 0.5
