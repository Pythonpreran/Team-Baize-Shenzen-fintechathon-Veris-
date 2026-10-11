"""Hosted-mode security configuration and outbound-boundary helpers.

This module deliberately has no dependency on application services.  It is safe for
transport modules to import, which keeps the hosted outbound allowlist at the actual
network boundary rather than at one convenient API route.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

MODE_ENV = "VERIS_MODE"
RECORD_HMAC_KEY_ENV = "VERIS_RECORD_HMAC_KEY"
OUTBOUND_ALLOWLIST_ENV = "VERIS_OUTBOUND_ALLOWLIST"
REQUEST_LIMIT_ENV = "VERIS_MAX_REQUEST_BYTES"
PDF_PARSE_TIMEOUT_ENV = "VERIS_PDF_PARSE_TIMEOUT_SECONDS"
PDF_PARSE_MEMORY_ENV = "VERIS_PDF_PARSE_MEMORY_BYTES"

CSRF_COOKIE = "veris_csrf"
CSRF_HEADER = "X-CSRF-Token"

_DEFAULT_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_DEFAULT_PDF_PARSE_TIMEOUT_SECONDS = 8.0
_DEFAULT_PDF_PARSE_MEMORY_BYTES = 512 * 1024 * 1024

# These are the only public services the application knows how to use.  A hosted
# operator may add a reviewed host through the environment; application input never
# influences this list.
_DEFAULT_OUTBOUND_HOSTS = frozenset({
    "api.openai.com",
    "api.deepseek.com",
    "dashscope.aliyuncs.com",
    "data.gov.sg",
    "api-open.data.gov.sg",
    "tablebuilder.singstat.gov.sg",
    "www.onemap.gov.sg",
})


class SecurityConfigurationError(RuntimeError):
    """A deployment setting is unsafe or incomplete."""


def mode() -> str:
    value = (os.getenv(MODE_ENV) or "local").strip().lower()
    if value not in {"local", "hosted"}:
        raise SecurityConfigurationError(
            f"{MODE_ENV} must be 'local' or 'hosted', not {value!r}.")
    return value


def hosted_mode() -> bool:
    return mode() == "hosted"


def _positive_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SecurityConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise SecurityConfigurationError(
            f"{name} must be between {minimum} and {maximum}.")
    return value


def max_request_bytes() -> int:
    return _positive_int(
        REQUEST_LIMIT_ENV, _DEFAULT_MAX_REQUEST_BYTES,
        minimum=1_024, maximum=512 * 1024 * 1024)


def pdf_parse_timeout_seconds() -> float:
    raw = (os.getenv(PDF_PARSE_TIMEOUT_ENV) or "").strip()
    if not raw:
        return _DEFAULT_PDF_PARSE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise SecurityConfigurationError(
            f"{PDF_PARSE_TIMEOUT_ENV} must be a number of seconds.") from exc
    if not 0.001 <= value <= 120:
        raise SecurityConfigurationError(
            f"{PDF_PARSE_TIMEOUT_ENV} must be between 0.001 and 120 seconds.")
    return value


def pdf_parse_memory_bytes() -> int:
    return _positive_int(
        PDF_PARSE_MEMORY_ENV, _DEFAULT_PDF_PARSE_MEMORY_BYTES,
        minimum=64 * 1024 * 1024, maximum=2 * 1024 * 1024 * 1024)


def outbound_allowlist() -> frozenset[str]:
    configured = {
        host.strip().lower().rstrip(".")
        for host in (os.getenv(OUTBOUND_ALLOWLIST_ENV) or "").split(",")
        if host.strip()
    }
    return frozenset(_DEFAULT_OUTBOUND_HOSTS | configured)


def require_outbound_url(url: str, *, purpose: str) -> None:
    """Reject unreviewed destinations before a hosted process opens a socket.

    Local mode deliberately retains the demo-site and mocked-transport conveniences.
    In hosted mode every URL must be HTTPS, free of embedded credentials, and have a
    hostname explicitly configured or built into the reviewed product boundary.
    """
    if not hosted_mode():
        return
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower().rstrip(".")
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or host not in outbound_allowlist()):
        raise SecurityConfigurationError(
            f"Hosted outbound {purpose} destination is not allowlisted.")


def record_hmac_key(*, required: bool = False) -> bytes | None:
    """Return the deployment-only record-integrity key without exposing it.

    An HMAC needs a secret held outside the portable export.  Hosted mode therefore
    requires an environment value; local deterministic demos may omit it and report
    that the portable chain has structural SHA-256 verification only.
    """
    value = os.getenv(RECORD_HMAC_KEY_ENV)
    if not value:
        if required or hosted_mode():
            raise SecurityConfigurationError(
                f"Set {RECORD_HMAC_KEY_ENV} in the hosted environment.")
        return None
    raw = value.encode("utf-8")
    if len(raw) < 32:
        raise SecurityConfigurationError(
            f"{RECORD_HMAC_KEY_ENV} must contain at least 32 UTF-8 bytes.")
    return raw
