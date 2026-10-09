"""Typed ingestion and intake errors.

Layer 1's input is "whatever documents a small business holds", so malformed input
is the normal case, not the exceptional one. Every rejection carries a message a
non-technical owner can act on, because a bare 500 teaches the user nothing.

`http_status` lets the API translate without a chain of isinstance checks, and
keeps the taxonomy of failures in one place instead of spread across endpoints.
"""
from __future__ import annotations


class ProvenError(Exception):
    """Base class. `http_status` and `code` are part of the contract."""

    http_status = 400
    code = "PROVEN_ERROR"

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class IngestError(ProvenError):
    """A document could not be read. The document is named in `message`."""

    http_status = 400
    code = "INGEST_FAILED"


class UnsupportedDocument(IngestError):
    """Not a PDF, or not a shape Layer 1 can extract natively."""

    http_status = 415
    code = "UNSUPPORTED_DOCUMENT"


class EncryptedDocument(IngestError):
    """Password-protected. We do not attempt to crack it."""

    http_status = 400
    code = "ENCRYPTED_DOCUMENT"


class DocumentTooLarge(IngestError):
    """Over the byte or page cap. Caps exist because pypdf on an untrusted file is
    a denial-of-service surface."""

    http_status = 413
    code = "DOCUMENT_TOO_LARGE"


class EmptyDocument(IngestError):
    http_status = 400
    code = "EMPTY_DOCUMENT"


class NoTextLayer(IngestError):
    """A native PDF with no extractable text — almost always a scan. OCR is out of
    the demo path by design, so this is reported, never guessed at."""

    http_status = 422
    code = "NO_TEXT_LAYER"


class BundleError(ProvenError):
    http_status = 400
    code = "BUNDLE_INVALID"


class ConsentRequired(ProvenError):
    """No live disclosure names this recipient. Consent is a pull, not a share."""

    http_status = 403
    code = "CONSENT_REQUIRED"
