"""Real SMP responses, captured 2026-09-09 against tagmeter.com tenant 002.

These are ground truth. Every one was returned by the live platform, so the
tests below assert against the vendor's actual behaviour rather than against our
own idea of it.
"""

AUTH_OK = {
	"code": 200,
	"authorization": "eyJhbGciOiJIUzUxMiJ9.eyJsb2dpbl91c2VyX2tleSI6IjRlZjVkNTEyIn0.sig",
	"message": "Valid Authorization Token, Authorize Record Updated",
}

# Meter 68753500170868 -- valve open, empty pipe, data frozen since 13 Aug.
AMR_0868 = {
	"code": 200,
	"message": "Operation success!",
	"customerInfo": {
		"ThirdPartyID": None, "CustomerName": None, "CommunityName": None,
		"BuildingName": None, "HouseNumber": None, "City": None, "Telephone": None,
	},
	"dataAMRRecord": {
		"meterID": "68753500170868", "devEUI": "8CF957200020DAFE", "connection": "DN15",
		"TimeStamp": "2026-08-13 09:54:06", "TotalCounter": 0, "PrepaidBalance": 2,
		"CurrentFlow": 0, "Temperature": 23.65,
		"ValveStatus": "00[Valve open;]", "AlarmMessage": "02[Empty pipe alarm;]",
		"RSSI": -43, "SNR": 9,
	},
	"dataHourly": [
		{"TimeStamp": f"2026-08-{12 + (13 + h) // 24:02d} {(13 + h) % 24:02d}:00:00",
		 "TotalCounter": 0, "NextHourDifference": 0}
		for h in range(24)
	],
}

# Meter 68753500170871 -- the one a vendor recharge_meter of 0.001 m3 provably
# reached: balance 2 -> 2.001, and the low-balance bit is set.
AMR_0871 = {
	"code": 200,
	"message": "Operation success!",
	"customerInfo": {"CustomerName": None},
	"dataAMRRecord": {
		"meterID": "68753500170871", "devEUI": "8CF957200020DBE2", "connection": "DN15",
		"TimeStamp": "2026-08-20 00:54:59", "TotalCounter": 0, "PrepaidBalance": 2.001,
		"CurrentFlow": 0, "Temperature": 20.82,
		"ValveStatus": "08[Valve open;Low Balance Alarm;]",
		"AlarmMessage": "02[Empty pipe alarm;]",
		"RSSI": -42, "SNR": 7,
	},
	"dataHourly": [],
}

# A meter the platform knows but holds no AMR record for.
AMR_NO_RECORD = {"code": 200, "message": "Operation success!", "customerInfo": {}}

# {"meterID": ""} -- the vendor reports success on an empty serial.
CUSTOMER_INFO_EMPTY_SN = {
	"code": 200, "message": "Operation success!", "meterID": "",
	"customerInfo": {"CustomerName": None, "Telephone": None},
}

ERR_401 = {"code": 401, "message": "Invalid Authorization Token or Authorization Token non-exist"}
ERR_500_UNKNOWN_METER = {"code": 500, "message": "Unknown meterID, customer information NOT fetched"}
# Note the message: the token was fine. This is why we never branch on it.
ERR_500_MISSING_FIELD = {
	"code": 500, "message": "Failed checking Authorization Token, Latest AMR Record meterID NOT fetched"
}

GATEWAY_ONLINE = {
	"code": 200, "gatewayID": "0C4EC0FFFE00E97F", "online": True, "mqtt_protocol": True,
	"latitude": -1.2968242, "longitude": 36.7771465, "altitude": 1787,
	"gpsTimeSync": True, "statTime": "2026-09-09 14:02:11", "timezone": "Europe/Amsterdam",
}

# Measured 2026-09-11 against the live SMP, on a token a human had revoked by
# logging into tagmeter.com. The platform reports an expired token on a READ as
# code 200 with the failure buried in the message -- not as code 401, and not as
# the bare empty list get_customer_info returns. Its text also conflates two
# unrelated faults, which is why a refresh-and-retry is the only way to tell
# them apart.
AMR_TOKEN_EXPIRED = {
	"code": 200,
	"message": "Authorization Token expired or invalid or unknown meterID 68753500170871",
}

# The same code-200 auth failure, on a WRITE. Reported from the field
# 2026-09-11 as "The SMP refused it: Authorization Token expired or invalid or
# unknown meterID 68753500170872" -- a valve command killed by an expired
# token, because a write with no "success" in its message reads as a refusal.
VALVE_TOKEN_EXPIRED = {
	"code": 200,
	"message": "Authorization Token expired or invalid or unknown meterID 68753500170872",
}
