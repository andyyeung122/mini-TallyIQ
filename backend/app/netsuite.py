"""A fake NetSuite that behaves like a real remote system: it can be slow, down,
reject bad data, or (the nasty one) save your record and then drop the response.

A real client would make HTTP calls to NetSuite's REST API with the same
`create_invoice(payload, idempotency_key)` shape; the sync worker can't tell the difference.
"""
import hashlib
import json
import threading


class TransientError(Exception):
    """Worth retrying: timeouts, 5xx, connection resets, rate limits."""


class PermanentError(Exception):
    """Retrying won't help: validation errors, conflicts. A person needs to look."""


MODES = {
    "healthy": "Every call succeeds",
    "flaky": "The next 2 calls time out, then it recovers",
    "lost_response": "The next call saves the invoice but the response is lost",
    "down": "Every call fails with 503 until you change the mode",
    "reject": "Every call is rejected as invalid (a permanent error)",
}


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class FakeNetSuite:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.invoices: dict[str, dict] = {}  # NetSuite id -> record
        self._by_key: dict[str, tuple[str, str]] = {}  # idempotency key -> (fingerprint, NetSuite id)
        self._next_id = 1001
        self.mode = "healthy"
        self._flaky_left = 0
        self.calls = 0

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown mode: {mode}")
        with self._lock:
            self.mode = mode
            self._flaky_left = 2 if mode == "flaky" else 0

    def create_invoice(self, payload: dict, idempotency_key: str) -> str:
        with self._lock:
            self.calls += 1
            if self.mode == "down":
                raise TransientError("503 Service Unavailable")
            if self.mode == "reject":
                raise PermanentError("400 Invalid record: field 'subsidiary' is required")
            if self.mode == "flaky":
                self._flaky_left -= 1
                if self._flaky_left <= 0:
                    self.mode = "healthy"
                raise TransientError("Read timed out after 30s")

            fp = fingerprint(payload)
            if idempotency_key in self._by_key:
                seen_fp, ns_id = self._by_key[idempotency_key]
                if seen_fp != fp:
                    raise PermanentError("409 Idempotency key was already used with a different payload")
                return ns_id  # replay: same answer, no duplicate record

            if sum(line["amount_cents"] for line in payload["lines"]) != payload["total_cents"]:
                raise PermanentError("400 Invalid record: line amounts don't add up to the total")

            ns_id = f"NS-{self._next_id}"
            self._next_id += 1
            self.invoices[ns_id] = {**payload, "netsuite_id": ns_id}
            self._by_key[idempotency_key] = (fp, ns_id)

            if self.mode == "lost_response":
                self.mode = "healthy"
                raise TransientError("Connection reset after the request was sent")
            return ns_id


netsuite = FakeNetSuite()


def get_netsuite() -> FakeNetSuite:
    return netsuite
