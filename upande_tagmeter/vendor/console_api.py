"""Client for the SMP's *internal console* API at ``iotcloud.tagmeter.com:8099``.

This is a second, separate platform API from the documented one in
``client.py``. The vendor's own web console uses it, and it answers questions
the documented API cannot:

* **Full reading history.** ``get_latest_amr`` returns one record;
  ``listRecordNewHistory`` returns every record it holds -- 54 for a meter
  three weeks into service.
* **When the meter measured, not when the platform stored it.** Records carry
  ``meterTime`` *and* ``createTime``. The documented API exposes only the
  latter, so every "last reported" we show is really "last ingested", about
  two minutes late. The gap is remarkably stable: 2m04s on every row sampled
  on 2026-09-11.
* **The raw frame.** ``rawData`` is the meter's hex payload, which is what any
  future work on the undecoded status bits will need.

Discovered 2026-09-11 by capturing the console's own network traffic.

Three things to know before depending on it
-------------------------------------------

**It is undocumented.** The vendor may change or withdraw it without notice.
Nothing that must keep working should depend on it alone; treat it as an
enrichment over the documented API, never as the only source.

**It is plain HTTP.** Port 8099, no TLS. Credentials and meter data cross the
network in the clear. That is the vendor's choice, not ours, and it is worth
pressing them on -- but it also means this client must never be handed a
password that is not already exposed by the documented integration.

**Its envelope differs.** ``{"code", "msg", "data"}`` here versus
``{"code", "message", ...}`` there, and the login token arrives at
``data.data`` rather than a ``token`` key. Sharing parsing between the two
would be a mistake, so this module deliberately shares none.

Writes -- ``updateValveStatus`` and its numeric ``valveFlag`` -- are
**deliberately absent**. Mapping those values means actuating live valves on a
production water network, and the documented ``valve_control`` endpoint is
already measured across 43 probed values. That work belongs in a session where
someone is watching the hardware.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import AuthFailed, ConfigError, Outcome

DEFAULT_BASE_URL = "http://iotcloud.tagmeter.com:8099/prod-api"
DEFAULT_TIMEOUT = 30

# The console sends these; the API is fussy enough about headers elsewhere
# (see client.py's User-Agent note) that it is not worth finding out which of
# these it checks.
BASE_HEADERS = {
	"Accept": "application/json, text/plain, */*",
	"Content-Type": "application/json;charset=UTF-8",
	"content-language": "en",
	"User-Agent": "upande-tagmeter/0.1 (+https://upande.com)",
}

# Their 401 arrives as HTTP 200 with this code in the body, exactly like the
# documented API's habit of signalling failure inside a success envelope.
UNAUTHORIZED_CODE = 401
OK_CODE = 200


class ConsoleClient:
	"""One console session. Token is fetched lazily and refreshed on a 401.

	The token store is injected for the same reason ``client.py`` injects one:
	so the Frappe cache can hold a single token shared across workers, rather
	than every worker minting its own.
	"""

	def __init__(
		self,
		base_url: str,
		username: str,
		password: str,
		*,
		token_store: Any = None,
		opener: Any = None,
		timeout: int = DEFAULT_TIMEOUT,
	) -> None:
		if not base_url or not username or not password:
			raise ConfigError(
				"The console API needs tagmeter_console_url, tagmeter_api_user and "
				"tagmeter_api_password in site_config.json."
			)
		self.base_url = base_url.rstrip("/")
		self.username = username
		self.password = password
		self.timeout = timeout
		self.store = token_store if token_store is not None else _MemoryToken()
		self._opener = opener
		self._lock = threading.Lock()

	# ── transport ────────────────────────────────────────────────────────────

	def _post(self, path: str, body: dict | None, token: str | None) -> Any:
		headers = dict(BASE_HEADERS)
		if token:
			headers["token"] = token
		data = json.dumps(body).encode() if body is not None else None
		request = urllib.request.Request(
			self.base_url + path, data=data, headers=headers, method="POST"
		)
		opener = self._opener or urllib.request.urlopen
		try:
			with opener(request, timeout=self.timeout) as response:
				return json.loads(response.read() or b"{}")
		except urllib.error.HTTPError as exc:
			try:
				return json.loads(exc.read() or b"{}")
			except Exception:
				return {"code": exc.code, "msg": f"HTTP {exc.code}"}

	def _get(self, path: str, token: str | None) -> Any:
		headers = dict(BASE_HEADERS)
		if token:
			headers["token"] = token
		request = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
		opener = self._opener or urllib.request.urlopen
		try:
			with opener(request, timeout=self.timeout) as response:
				return json.loads(response.read() or b"{}")
		except urllib.error.HTTPError as exc:
			try:
				return json.loads(exc.read() or b"{}")
			except Exception:
				return {"code": exc.code, "msg": f"HTTP {exc.code}"}

	# ── auth ─────────────────────────────────────────────────────────────────

	def authenticate(self) -> str:
		"""Log in and return a token.

		The token is at ``data.data``, not ``data.token``. Getting this wrong
		reads as a successful login that yields nothing, which is exactly how
		it presented when first probed.
		"""
		with self._lock:
			body = self._post("/login", {"username": self.username, "password": self.password}, None)
			if not isinstance(body, dict) or body.get("code") != OK_CODE:
				raise AuthFailed(
					f"The console API refused the credentials for {self.username!r}: "
					f"{(body or {}).get('msg')!r}"
				)
			token = ((body.get("data") or {}) or {}).get("data")
			if not token:
				raise AuthFailed(
					"The console API accepted the login but returned no token at data.data. "
					f"Keys present: {sorted((body.get('data') or {}).keys())}"
				)
			self.store.set(token)
			return token

	def _token(self) -> str:
		return self.store.get() or self.authenticate()

	def call(self, path: str, body: dict | None = None, method: str = "POST") -> tuple[Outcome, Any]:
		"""One authenticated request, refreshing the token once on a 401."""
		token = self._token()
		out = self._post(path, body, token) if method == "POST" else self._get(path, token)
		if isinstance(out, dict) and out.get("code") == UNAUTHORIZED_CODE:
			self.store.set(None)
			token = self.authenticate()
			out = self._post(path, body, token) if method == "POST" else self._get(path, token)
			if isinstance(out, dict) and out.get("code") == UNAUTHORIZED_CODE:
				raise AuthFailed(
					"The console API rejected a freshly issued token. If the app and a "
					"human share one account, their logins may be evicting each other -- "
					"this integration needs its own service account."
				)
		if not isinstance(out, dict):
			return Outcome.BAD_RESPONSE, None
		if out.get("code") != OK_CODE:
			return Outcome.BAD_RESPONSE, out
		return Outcome.OK, out.get("data")

	# ── endpoints ────────────────────────────────────────────────────────────

	def reading_history(self, meter_sn: str, page: int = 1, page_size: int = 50) -> tuple[Outcome, list]:
		"""Every stored reading for one meter, newest first.

		Each row carries both ``meterTime`` and ``createTime``; see the module
		docstring for why that distinction is the point of this whole client.
		"""
		outcome, data = self.call("/record/listRecordNewHistory", {
			"data": {"address": str(meter_sn).strip(), "startDate": "", "endDate": ""},
			"pageInfo": {"pageNum": page, "pageSize": page_size, "total": 0},
		})
		if outcome is not Outcome.OK:
			return outcome, []
		return outcome, (data or {}).get("rows") or []

	def meter_detail(self, meter_sn: str) -> tuple[Outcome, dict | None]:
		"""Full console view of one meter: identity, balance, newest record,
		gateway binding and the prepaid ``icCardMeter`` block."""
		return self.call(f"/meter/getMeterDetailInfo/{urllib.parse.quote(str(meter_sn).strip())}",
		                 None, method="GET")


class _MemoryToken:
	"""Process-local fallback store, for tests and one-off scripts."""

	def __init__(self) -> None:
		self._token: str | None = None

	def get(self) -> str | None:
		return self._token

	def set(self, token: str | None) -> None:
		self._token = token
