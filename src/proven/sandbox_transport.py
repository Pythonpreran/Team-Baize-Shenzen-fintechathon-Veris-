"""HTTP boundary between VERIS and the local synthetic counterparty.

The sandbox owns its database and HMAC signing key.  VERIS talks only to the sandbox's
local HTTP surface, including signature verification, so it cannot create or validate a
counterparty event from shared storage or key material.  The transport is intentionally
limited to the synthetic local service; it never contacts a real insurer, bank, or payment
rail.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from . import sandbox


class SandboxHttpConnector:
    """Bounded-retry client for the separate local synthetic sandbox API.

    ``client`` exists solely for the offline test suite, where a FastAPI TestClient drives
    the sandbox application's public HTTP contract without opening a network socket.
    Production accepts only the compose service or a loopback sandbox at port 8200.
    """

    _LOCAL_SANDBOX_HOSTS = frozenset({"sandbox", "localhost", "127.0.0.1", "::1"})

    class _NoRedirect(urlrequest.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def __init__(self, base_url: Optional[str] = None, *, client: Any = None,
                 retries: int = 2):
        self.base_url = self._validated_base_url(base_url) if base_url else ""
        self._client = client
        self.retries = retries
        self._tokens: dict[str, str] = {}
        self._opener = urlrequest.build_opener(self._NoRedirect())
        self._failures = 0
        self._open_until: Optional[datetime] = None

    @classmethod
    def _validated_base_url(cls, base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        parsed = urlparse.urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise sandbox.SandboxError(
                f"{sandbox.SANDBOX_URL_ENV} has an invalid local sandbox port.") from exc
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "http" or host not in cls._LOCAL_SANDBOX_HOSTS or port != 8200
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise sandbox.SandboxError(
                f"{sandbox.SANDBOX_URL_ENV} must be exactly the local sandbox service "
                "or loopback URL on http port 8200.")
        return value

    @classmethod
    def from_environment(cls) -> "SandboxHttpConnector":
        base_url = os.getenv(sandbox.SANDBOX_URL_ENV, "").strip()
        if not base_url:
            raise sandbox.SandboxUnavailable(
                f"Set {sandbox.SANDBOX_URL_ENV} to the local synthetic sandbox URL.")
        return cls(base_url)

    @staticmethod
    def _error(status: int, payload: Mapping[str, Any]) -> sandbox.SandboxError:
        message = str(payload.get("error") or "The synthetic sandbox rejected the request.")
        if status == 404:
            return sandbox.SandboxNotFound(message)
        if status == 409:
            return sandbox.SandboxConflict(message)
        if status == 503:
            return sandbox.SandboxUnavailable(message)
        return sandbox.SandboxError(message)

    @staticmethod
    def _payload(raw: bytes | str) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _raw_request(self, method: str, path: str, *, headers: Mapping[str, str],
                     json_body: Optional[Mapping[str, Any]] = None,
                     form_body: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
        if self._client is not None:
            response = self._client.request(
                method, path, headers=dict(headers), json=dict(json_body) if json_body is not None else None,
                data=dict(form_body) if form_body is not None else None,
            )
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            if not 200 <= int(response.status_code) < 300:
                raise self._error(int(response.status_code), payload)
            return payload
        if not self.base_url:
            raise sandbox.SandboxUnavailable("The local synthetic sandbox URL is unavailable.")
        data: Optional[bytes] = None
        request_headers = dict(headers)
        if json_body is not None:
            data = json.dumps(dict(json_body), sort_keys=True, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urlparse.urlencode(dict(form_body)).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urlrequest.Request(
            self.base_url + path, data=data, headers=request_headers, method=method)
        try:
            # The constructor validates this as the local synthetic sandbox URL.
            with self._opener.open(request, timeout=5) as response:  # nosec B310
                status, raw = int(response.status), response.read()
        except urlerror.HTTPError as exc:
            status, raw = int(exc.code), exc.read()
        except urlerror.URLError as exc:
            raise RuntimeError("The local synthetic sandbox transport is unavailable.") from exc
        payload = self._payload(raw)
        if not 200 <= status < 300:
            raise self._error(status, payload)
        return payload

    def _access_token(self, tenant_id: str) -> str:
        tenant = sandbox._tenant_id(tenant_id)
        token = self._tokens.get(tenant)
        if token is None:
            client_id, client_secret, _raw_secret = sandbox.local_service_credentials()
            response = self._raw_request(
                "POST", "/oauth/token", headers={}, form_body={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "tenant_id": tenant,
                })
            token = str(response.get("access_token") or "")
            if not token:
                raise sandbox.SandboxUnavailable("The local synthetic sandbox did not issue a client token.")
            self._tokens[tenant] = token
        return token

    def _request(self, method: str, path: str, *, tenant_id: Optional[str] = None,
                 json_body: Optional[Mapping[str, Any]] = None,
                 idempotency_key: Optional[str] = None) -> dict[str, Any]:
        tenant = sandbox._tenant_id(
            sandbox.SANDBOX_SERVICE_TENANT if tenant_id is None else tenant_id)
        headers = {
            "Authorization": f"Bearer {self._access_token(tenant)}",
            "X-VERIS-Tenant": tenant,
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = str(idempotency_key)
        return self._raw_request(method, path, headers=headers, json_body=json_body)

    def _call(self, func):
        now = datetime.now(timezone.utc)
        if self._open_until is not None and now < self._open_until:
            raise sandbox.SandboxUnavailable("The synthetic counterparty circuit is open; retry later.")
        last: Optional[Exception] = None
        for _attempt in range(self.retries + 1):
            try:
                value = func()
                self._failures, self._open_until = 0, None
                return value
            except sandbox.SandboxError:
                raise
            except Exception as exc:  # transport failures alone are retried
                last = exc
                self._failures += 1
        self._open_until = now + timedelta(seconds=30)
        raise sandbox.SandboxUnavailable(
            "The synthetic counterparty did not respond after retries.") from last

    def submit_claim(self, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        tenant_id = str(payload.get("tenant_id") or "")
        body = {
            "policy_id": str(payload.get("policy_id") or ""),
            "claim_type": str(payload.get("claim_type") or "HEALTH_REIMBURSEMENT"),
            "packet_sha256": str(payload.get("packet_sha256") or ""),
        }
        return self._call(lambda: self._request(
            "POST", "/claims", tenant_id=tenant_id, json_body=body,
            idempotency_key=idempotency_key))

    def claim_status(self, claim_id: str, *, tenant_id: str) -> dict[str, Any]:
        return self._call(lambda: self._request(
            "GET", f"/claims/{claim_id}", tenant_id=tenant_id))

    def reply_to_query(self, claim_id: str, payload: Mapping[str, Any], *, tenant_id: str,
                       idempotency_key: str) -> dict[str, Any]:
        return self._call(lambda: self._request(
            "POST", f"/claims/{claim_id}/query-replies", tenant_id=tenant_id,
            json_body={"note": str(payload.get("note") or "")}, idempotency_key=idempotency_key))

    def dispute_settlement(self, claim_id: str, payload: Mapping[str, Any], *, tenant_id: str,
                           idempotency_key: str) -> dict[str, Any]:
        return self._call(lambda: self._request(
            "POST", f"/claims/{claim_id}/disputes", tenant_id=tenant_id,
            json_body={"note": str(payload.get("note") or "")}, idempotency_key=idempotency_key))

    def bank_transfer_receipt(self, transfer_id: str, *, tenant_id: str) -> dict[str, Any]:
        return self._call(lambda: self._request(
            "GET", f"/bank/transfers/{transfer_id}/receipt", tenant_id=tenant_id))

    def get_invoice(self, invoice_id: str, *, tenant_id: str) -> dict[str, Any]:
        return self._call(lambda: self._request(
            "GET", f"/invoices/{invoice_id}", tenant_id=tenant_id))

    def pay_invoice(self, invoice_id: str, *, tenant_id: str, expected_payee: str,
                    expected_amount: str, idempotency_key: str) -> dict[str, Any]:
        return self._call(lambda: self._request(
            "POST", f"/invoices/{invoice_id}/pay", tenant_id=tenant_id,
            json_body={"expected_payee": expected_payee, "expected_amount": expected_amount},
            idempotency_key=idempotency_key))

    def request(self, kind: str, payload: Mapping[str, Any], *, tenant_id: str,
                idempotency_key: str) -> dict[str, Any]:
        paths = {"ENDORSEMENT_REQUEST": "/endorsements", "GRIEVANCE": "/grievances"}
        path = paths.get(kind)
        if path is None:
            raise sandbox.SandboxError("Unsupported synthetic counterparty request.")
        body = {
            "note": str(payload.get("note") or ""),
            "claim_id": str(payload.get("claim_id") or ""),
            "ladder_entry_id": str(payload.get("ladder_entry_id") or ""),
        }
        return self._call(lambda: self._request(
            "POST", path, tenant_id=tenant_id, json_body=body,
            idempotency_key=idempotency_key))

    def clock(self) -> dict[str, Any]:
        return self._call(lambda: self._request("GET", "/clock"))

    def operation_artifact(self, tenant_id: str) -> dict[str, Any]:
        return self._call(lambda: self._request("GET", "/operations", tenant_id=tenant_id))

    def verify_webhook(self, event: Mapping[str, Any], signature: str) -> bool:
        tenant_id = str(event.get("tenant_id") or "")
        result = self._call(lambda: self._request(
            "POST", "/webhooks/verify", tenant_id=tenant_id,
            json_body={"event": dict(event), "signature": str(signature)}))
        return result.get("valid") is True
