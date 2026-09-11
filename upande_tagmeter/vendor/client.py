"""HTTP client for the tagmeter.com SMP REST API.

Frappe-free by design. The token store and HTTP session are injected, so every
behaviour below -- including token revocation and the vendor's overloaded error
codes -- is unit-testable against recorded responses with no bench and no
network.

Reads and valve control are implemented. Prepaid recharge (``recharge_meter``,
``savewebhook``) is still absent -- it needs the credit ledger and tariff model
that do not exist yet, and issuing credit with nowhere to account for it would
be worse than not issuing it.

Reads and writes are classified by different rules, because the SMP answers
``code: 200`` for both success and failure on a write. See :meth:`_classify`
and :meth:`_classify_write`.
"""

import threading
import time
from contextlib import contextmanager
from typing import Any

from .errors import (
	AuthFailed,
	BlockedByVendor,
	ConfigError,
	InvalidMeterID,
	Outcome,
	UnsupportedValveAction,
)
from .parse import METER_TZ, is_valid_meter_sn, parse_amr, parse_gateway_status

DEFAULT_TIMEOUT = 30  # observed latencies 0.5-4.8s; slow should be slow, not failed

# The SMP's edge 403s the default python-requests User-Agent with an HTML
# "Request forbidden by administrative rules" page, before the API is reached.
# urllib, a browser string, and this product string all pass. Do not remove it,
# and do not let requests fall back to its own default.
USER_AGENT = "upande-tagmeter/0.1 (+https://upande.com)"

# The SMP reports an expired token on a read as ``code: 200`` with this text in
# the message, and the same string also covers an unknown meterID -- two
# unrelated faults sharing one sentence. Matched case-insensitively on the
# stable prefix only, so the trailing serial does not have to be parsed out.
AUTH_FAILURE_TEXT = "authorization token expired or invalid"


def reports_auth_failure(parsed: Any) -> bool:
	"""True when a ``code: 200`` body is actually reporting an auth failure.

	Deliberately narrow: only a 200 qualifies. A genuine ``code: 401`` is
	already classified by the normal path, and a body carrying real data is
	never second-guessed on the strength of its message text.
	"""
	if not isinstance(parsed, dict) or parsed.get("code") != 200:
		return False
	return AUTH_FAILURE_TEXT in str(parsed.get("message") or "").lower()

READ_ENDPOINTS = frozenset({
	"req_authorization_token",
	"get_latest_amr",
	"get_freeze_record",
	"get_customer_info",
	"get_gateway_status",
})

# Writes are enumerated separately so a read path can never reach one by
# accident, and so adding an endpoint here is a deliberate act.
WRITE_ENDPOINTS = frozenset({"valve_control"})

# Accepted values, measured. "Reset" is accepted by the SMP too but is
# deliberately not offered -- see UnsupportedValveAction.
VALVE_ACTIONS = ("Open", "Close")


class MemoryTokenStore:
	"""Default store: one token, process-local, with a real lock.

	Fine for tests and single-process scripts. Production uses the Frappe
	cache-backed store in ``sync.py`` so that every worker shares one token --
	see the class docstring there for why that is not optional.
	"""

	def __init__(self) -> None:
		self._token: str | None = None
		self._lock = threading.Lock()

	def get(self) -> str | None:
		return self._token

	def set(self, token: str | None) -> None:
		self._token = token

	@contextmanager
	def lock(self):
		with self._lock:
			yield


class TagMeterClient:
	"""One SMP session.

	Two separate things invalidate a token, and they report differently:

	* **Revocation.** Requesting a token revokes the previous one -- verified:
	  token A returns "Invalid Authorization Token or Authorization Token
	  non-existent" the moment token B is issued. The vendor treats this as a
	  single-session login, not a stateless API key.
	* **Expiry.** Tokens also lapse on their own after roughly 65 minutes,
	  reporting "Authorization Token expired, Request New Authorization Token".
	  Measured: minted 14:02:33Z, expired by 15:08Z.

	Both surface as UNAUTHORIZED and are handled identically, so the client
	never needs to tell them apart -- but the revocation half is why refresh
	must be single-flight. Without that, two workers revoke each other every
	cycle and produce 401s that look random in the logs.

	Refresh stays reactive rather than pre-empting the ~65 minute TTL: a timer
	that minted early would revoke a token other workers are still using,
	which is the exact failure it would be trying to avoid.
	"""

	def __init__(
		self,
		base_url: str,
		username: str,
		password: str,
		*,
		token_store: Any = None,
		session: Any = None,
		timeout: int = DEFAULT_TIMEOUT,
		tz_name: str = METER_TZ,
		min_interval: float = 0.0,
		max_attempts: int = 3,
		retry_backoff: float = 1.0,
	) -> None:
		if not base_url or not username or not password:
			raise ConfigError(
				"upande_tagmeter needs tagmeter_api_url, tagmeter_api_user and "
				"tagmeter_api_password in site_config.json."
			)
		self.base_url = base_url.rstrip("/")
		self.username = username
		self.password = password
		self.tz_name = tz_name
		self.timeout = timeout
		self.min_interval = min_interval
		self.max_attempts = max(1, int(max_attempts))
		self.retry_backoff = retry_backoff
		self.store = token_store if token_store is not None else MemoryTokenStore()
		self._session = session
		self._last_call_at = 0.0
		self.last_raw: str | None = None

	# ── transport ────────────────────────────────────────────────────────────

	@property
	def session(self):
		if self._session is None:
			import requests

			self._session = requests.Session()
		return self._session

	def _throttle(self) -> None:
		"""Their rate limit is undocumented, so pace calls conservatively."""
		if self.min_interval <= 0:
			return
		gap = time.monotonic() - self._last_call_at
		if gap < self.min_interval:
			time.sleep(self.min_interval - gap)

	def _raw_post(self, endpoint: str, body: dict, token: str | None) -> tuple[str, Any]:
		"""POST and parse JSON.

		Returns ``(kind, payload)`` where kind is one of ``"ok"``,
		``"server_error"`` or ``"transport_error"``. The distinction matters:
		a network fault is worth retrying, whereas their HTTP 500 is
		reproducible per meter and retrying it only burns calls.
		"""
		if endpoint not in READ_ENDPOINTS and endpoint not in WRITE_ENDPOINTS:
			raise ValueError(f"{endpoint} is not an endpoint this client will call")
		headers = {
			"content-language": "en",
			"content-type": "application/json",
			"User-Agent": USER_AGENT,
		}
		if token:
			headers["authorization"] = token
		self._throttle()
		try:
			response = self.session.post(
				f"{self.base_url}/{endpoint}", json=body, headers=headers, timeout=self.timeout
			)
		except Exception as exc:  # requests raises a family; all mean "unreachable"
			return "transport_error", f"{type(exc).__name__}: {exc}"
		finally:
			self._last_call_at = time.monotonic()

		self.last_raw = (response.text or "")[:4000]
		# Their HTTP status is not the answer -- failures arrive as 200 with
		# code 500 in the body. A 5xx at the HTTP layer is a genuine transport
		# fault (proxy, restart) and is retryable; anything else we parse.
		if response.status_code >= 500:
			# Their application tier, not the network. Measured across the
			# whole fleet, an HTTP 500 with an empty body means "no AMR record
			# exists for this meter" -- their code crashes instead of returning
			# an empty result. Every serial that answered 200 had a reading,
			# none answered 200 with no record, and the failing set shrinks as
			# meters start reporting. So retrying cannot help: a second call
			# will not make data exist.
			return "server_error", f"HTTP {response.status_code}"
		try:
			return "ok", response.json()
		except Exception:
			# A non-JSON 4xx is their edge refusing us, not the API answering.
			# The API itself always replies 200 with a JSON code field.
			if 400 <= response.status_code < 500:
				raise BlockedByVendor(
					f"The SMP edge returned HTTP {response.status_code} with a non-JSON body "
					f"for {endpoint}. This is a client-fingerprint block, not an auth failure: "
					f"the User-Agent sent was {USER_AGENT!r}. Body: {self.last_raw[:200]!r}"
				)
			return "ok", None

	# ── auth ─────────────────────────────────────────────────────────────────

	def authenticate(self) -> str:
		"""Mint a new token. Revokes any token currently held by this account."""
		kind, parsed = self._raw_post(
			"req_authorization_token", {"username": self.username, "password": self.password}, None
		)
		if kind != "ok":
			raise AuthFailed(f"Could not reach the SMP to authenticate ({kind}): {parsed}")
		if not isinstance(parsed, dict) or parsed.get("code") != 200:
			message = (parsed or {}).get("message") if isinstance(parsed, dict) else None
			raise AuthFailed(f"SMP refused the credentials: {message or parsed}")
		token = parsed.get("authorization")
		if not token:
			raise AuthFailed("SMP returned code 200 with no authorization token")
		return token

	def _token(self) -> str:
		token = self.store.get()
		if token:
			return token
		return self._refresh(used=None)

	def _refresh(self, used: str | None) -> str:
		"""Single-flight refresh.

		``used`` is the token that just failed. Inside the lock we re-read the
		store: if it no longer matches, another worker already refreshed and we
		adopt its token rather than minting a second one and revoking theirs.
		"""
		with self.store.lock():
			current = self.store.get()
			if current and current != used:
				return current
			token = self.authenticate()
			self.store.set(token)
			return token

	# ── request with the vendor's error taxonomy applied ─────────────────────

	def call_write(self, endpoint: str, body: dict) -> tuple[Outcome, dict | None]:
		"""One authenticated write. Never retried.

		A transport failure on a write leaves us unable to tell whether the
		command reached their queue, so retrying blind could enqueue a second
		conflicting downlink. Retries belong on the Meter Command document,
		where the attempt count is visible and bounded.
		"""
		if endpoint not in WRITE_ENDPOINTS:
			raise ValueError(f"{endpoint} is not a write endpoint")
		token = self._token()
		outcome, parsed = self._classify_write(*self._raw_post(endpoint, body, token))
		if outcome is Outcome.UNAUTHORIZED:
			token = self._refresh(used=token)
			outcome, parsed = self._classify_write(*self._raw_post(endpoint, body, token))
			if outcome is Outcome.UNAUTHORIZED:
				raise AuthFailed("SMP rejected a freshly issued token on a write")
		return outcome, parsed

	@staticmethod
	def _classify_write(kind: str, parsed: Any) -> tuple[Outcome, dict | None]:
		"""Writes cannot be classified by ``code`` -- it is 200 either way.

		Measured::

			forceValve "Xyzzy" -> code 200 "Invalid valve control command"
			forceValve "Open"  -> code 200 "Operation success!"

		So the message is the only signal, which is the exact opposite of the
		rule for reads. Anything that is not an explicit success is treated as
		a rejection rather than optimistically assumed to have worked.
		"""
		if kind == "server_error":
			return Outcome.SERVER_ERROR, None
		if kind == "transport_error":
			return Outcome.TRANSPORT_ERROR, None
		if isinstance(parsed, list) and not parsed:
			return Outcome.UNAUTHORIZED, None
		if not isinstance(parsed, dict):
			return Outcome.BAD_RESPONSE, None
		if parsed.get("code") == 401:
			return Outcome.UNAUTHORIZED, parsed
		message = str(parsed.get("message") or "").lower()
		if "success" in message:
			return Outcome.OK, parsed
		return Outcome.REJECTED, parsed

	def set_valve(self, meter_sn: str, action: str) -> tuple[Outcome, dict | None]:
		"""Force a valve open or closed.

		``Outcome.OK`` means the SMP accepted the request into its downlink
		queue. It does **not** mean the valve moved -- the meter may not answer
		for hours on Class B. Confirmation comes only from a later reading.
		"""
		if action not in VALVE_ACTIONS:
			raise UnsupportedValveAction(
				f"{action!r} is not offered. Accepted: {', '.join(VALVE_ACTIONS)}."
			)
		return self.call_write(
			"valve_control", {"meterID": self._require_sn(meter_sn), "forceValve": action}
		)

	def call(self, endpoint: str, body: dict) -> tuple[Outcome, dict | None]:
		"""One authenticated read. Expected failures are returned, not raised.

		A network fault is retried with exponential backoff. Their HTTP 500 is
		not: it is reproducible per meter, so a retry would only slow the sweep
		down and still fail.
		"""
		if endpoint not in READ_ENDPOINTS:
			# The read path retries on transport errors, which is wrong for a
			# write: we could not tell whether the first attempt landed. Writes
			# must go through call_write.
			raise ValueError(f"{endpoint} is not a read endpoint; use call_write")
		token = self._token()
		outcome, parsed = self._attempt_with_retry(endpoint, body, token)

		if outcome is Outcome.UNAUTHORIZED:
			# Refresh once and retry once. A second 401 means the credentials
			# are wrong or a human took the session from the web console.
			token = self._refresh(used=token)
			outcome, parsed = self._attempt_with_retry(endpoint, body, token)
			if outcome is Outcome.UNAUTHORIZED:
				if reports_auth_failure(parsed):
					# Their code-200 message conflates "token expired" with
					# "unknown meterID". A freshly issued token rules out the
					# first, so the second is what is left. Returning rather
					# than raising matters: one unregistered serial must not
					# abort a sweep across the whole fleet.
					return Outcome.UNKNOWN_METER, None
				raise AuthFailed(
					"SMP rejected a freshly issued token. Either the credentials are "
					"wrong, or another session (a human on tagmeter.com, or a second "
					"deployment) revoked it -- this app needs its own service account."
				)
		return outcome, parsed

	def _attempt_with_retry(self, endpoint: str, body: dict, token: str | None):
		"""Retry only TRANSPORT_ERROR, the one outcome that is genuinely transient."""
		delay = self.retry_backoff
		for attempt in range(self.max_attempts):
			outcome, parsed = self._classify(*self._raw_post(endpoint, body, token))
			if outcome is not Outcome.TRANSPORT_ERROR or attempt == self.max_attempts - 1:
				return outcome, parsed
			time.sleep(delay)
			delay *= 2
		return outcome, parsed

	@staticmethod
	def _classify(kind: str, parsed: Any) -> tuple[Outcome, dict | None]:
		if kind == "server_error":
			return Outcome.SERVER_ERROR, None
		if kind == "transport_error":
			return Outcome.TRANSPORT_ERROR, None
		if isinstance(parsed, list) and not parsed:
			# get_customer_info answers a bare empty list -- HTTP 200, no code
			# field -- when the token has expired. Measured 2026-09-09.
			# Without this, an expired token on that endpoint classifies as
			# BAD_RESPONSE and never triggers the refresh-and-retry.
			return Outcome.UNAUTHORIZED, None
		if not isinstance(parsed, dict):
			return Outcome.BAD_RESPONSE, None
		code = parsed.get("code")
		if code == 200:
			# ...and get_latest_amr has a third way of saying it: code 200 with
			# the failure in the message. Measured 2026-09-11. This one is the
			# dangerous shape, because the body is well-formed and empty, so
			# parse_amr returns None and the caller records "no AMR record" --
			# a real, common state for this fleet. Left unchecked, every read
			# fails silently as "nothing new to report" until a human notices
			# the whole fleet has gone quiet.
			if reports_auth_failure(parsed):
				return Outcome.UNAUTHORIZED, parsed
			return Outcome.OK, parsed
		if code == 401:
			return Outcome.UNAUTHORIZED, parsed
		if code == 500:
			# Overloaded: covers both "unknown meterID" and "invalid input".
			# We never branch on their message text, so callers pre-validate
			# their inputs; a 500 on a well-formed request means the meter is
			# not on their platform.
			return Outcome.UNKNOWN_METER, parsed
		return Outcome.BAD_RESPONSE, parsed

	# ── endpoints ────────────────────────────────────────────────────────────

	def _require_sn(self, meter_sn: str) -> str:
		if not is_valid_meter_sn(meter_sn):
			raise InvalidMeterID(
				f"{meter_sn!r} is not 14 decimal digits. The SMP answers "
				'{"meterID": ""} with code 200 "Operation success!" and null data, '
				"so an unchecked blank would read as healthy forever."
			)
		return str(meter_sn).strip()

	def get_latest_amr(self, meter_sn: str) -> tuple[Outcome, dict | None]:
		"""Latest AMR record plus the rolling 24-hour series, normalised.

		``(OK, None)`` means the platform knows this meter but holds no AMR
		record for it -- see :func:`parse_amr`.
		"""
		outcome, body = self.call("get_latest_amr", {"meterID": self._require_sn(meter_sn)})
		if outcome is not Outcome.OK:
			return outcome, None
		return outcome, parse_amr(body, self.tz_name)

	def get_customer_info(self, meter_sn: str) -> tuple[Outcome, dict | None]:
		"""Vendor-side customer record. Null for every Upande meter by design:
		the SMP is our integration account and customers live in Frappe."""
		return self.call("get_customer_info", {"meterID": self._require_sn(meter_sn)})

	def get_gateway_status(self, gateway_id: str) -> tuple[Outcome, dict | None]:
		"""Gateway health. Note ``stat_time`` comes back in Dutch local time,
		not the UTC that meter records use."""
		outcome, body = self.call(
			"get_gateway_status", {"gatewayID": str(gateway_id).strip().upper()}
		)
		if outcome is not Outcome.OK:
			return outcome, None
		return outcome, parse_gateway_status(body)
