"""Outcomes and exceptions for the tagmeter.com SMP client.

The vendor returns HTTP 200 for everything, including failures, and puts the
real status in a JSON ``code`` field. Callers therefore branch on
:class:`Outcome`, never on an HTTP status and never on the vendor's ``message``
text -- that text is actively misleading (omitting ``meterID`` yields
"Failed checking Authorization Token" even when the token is valid).
"""

from enum import Enum


class Outcome(str, Enum):
	"""Every way a vendor call can end.

	Expected conditions are returned, not raised: a sweep across 100 meters
	must not abort because one meter is not registered on their platform.
	"""

	OK = "ok"
	UNAUTHORIZED = "unauthorized"      # code 401 -- token revoked or invalid
	UNKNOWN_METER = "unknown_meter"    # code 500 in the body, on a well-formed SN
	SERVER_ERROR = "server_error"      # HTTP 5xx -- their app tier crashed
	TRANSPORT_ERROR = "transport_error"  # timeout, DNS, connection reset
	REJECTED = "rejected"              # write refused: "Invalid valve control command"
	BAD_RESPONSE = "bad_response"      # 200 with a body we cannot parse


class TagMeterError(Exception):
	"""Something the caller cannot be expected to handle."""


class ConfigError(TagMeterError):
	"""Credentials or base URL missing from site_config.json."""


class InvalidMeterID(TagMeterError):
	"""The meter serial is not 14 decimal digits.

	Raised before any request is sent. The vendor accepts ``{"meterID": ""}``
	and answers ``code: 200, "Operation success!"`` with null data, so an empty
	or malformed SN would otherwise look like a healthy read forever.
	"""


class UnsupportedValveAction(TagMeterError):
	"""The requested valve action is not one this client will send.

	``forceValve`` was probed with 43 candidate values on 2026-09-09. Only
	``Open``, ``Close`` and ``Reset`` were accepted; everything else returned
	"Invalid valve control command". ``Reset`` is deliberately NOT offered: it
	is accepted by the SMP, appears nowhere in their documentation, and nobody
	has told us what it does to a meter.
	"""


class AuthFailed(TagMeterError):
	"""Re-authentication failed, or a refreshed token was rejected again.

	A second 401 after a successful refresh is not retryable: either the
	credentials are wrong, or a human logged into the vendor's web console and
	took the session. Requesting a token revokes the previous one, so the app
	needs its own service account.
	"""


class BlockedByVendor(TagMeterError):
	"""The SMP's front door refused the request before it reached the API.

	Their edge returns an HTML ``403 Forbidden`` -- "Request forbidden by
	administrative rules" -- for some clients. The default ``python-requests``
	User-Agent is one of them; ``urllib``, a browser string, and our own product
	string all pass. So this is a client-fingerprint block, not an auth problem,
	and no credential change will fix it.

	Raised rather than returned: every subsequent call is blocked too, so a
	fleet sweep should stop instead of failing a hundred times.
	"""
