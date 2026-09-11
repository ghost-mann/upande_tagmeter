"""A stand-in for requests.Session that replays queued responses."""

import json


class FakeResponse:
	def __init__(self, payload, status_code=200):
		self._payload = payload
		self.status_code = status_code
		self.text = json.dumps(payload) if payload is not None else "<not json>"

	def json(self):
		if self._payload is None:
			raise ValueError("no json")
		return self._payload


class FakeSession:
	"""Queue of (payload, status) or exceptions, plus a record of every call."""

	def __init__(self, script):
		self.script = list(script)
		self.calls = []

	def post(self, url, json=None, headers=None, timeout=None):
		self.calls.append({"url": url, "body": json, "headers": headers or {}})
		if not self.script:
			raise AssertionError(f"unexpected extra call to {url}")
		item = self.script.pop(0)
		if isinstance(item, Exception):
			raise item
		payload, status = item if isinstance(item, tuple) else (item, 200)
		return FakeResponse(payload, status)

	@property
	def endpoints(self):
		return [c["url"].rsplit("/", 1)[-1] for c in self.calls]
