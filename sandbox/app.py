"""Standalone synthetic insurer/bank FastAPI application (default port 8200)."""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from proven import sandbox

app = FastAPI(
    title="VERIS Synthetic Counterparty Sandbox",
    version="1.0.0",
    description=("Local synthetic insurer and bank only. It creates no real claims, "
                 "payments, credentials or external requests."),
)


@dataclass(frozen=True)
class _SandboxPrincipal:
    tenant_id: str


def _service() -> sandbox.SandboxService:
    return sandbox.default_service()


@app.exception_handler(sandbox.SandboxError)
async def sandbox_error_handler(_request, exc: sandbox.SandboxError):
    return JSONResponse(status_code=exc.http_status, content={"error": str(exc), "synthetic": True})


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: str = Field(min_length=1, max_length=160)
    claim_type: str = Field(default="HEALTH_REIMBURSEMENT", min_length=1, max_length=80)
    packet_sha256: str = Field(default="", max_length=128)


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=2000)
    claim_id: str = Field(default="", max_length=160)
    ladder_entry_id: str = Field(default="", max_length=160)


class ClockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(ge=0, le=365)


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payee: str = Field(default="Lotus General (Sandbox)", min_length=1, max_length=240)
    amount: str = Field(default="1200.00", min_length=1, max_length=80)


class InvoicePaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_payee: str = Field(min_length=1, max_length=240)
    expected_amount: str = Field(min_length=1, max_length=80)


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: dict[str, Any]
    signature: str = Field(min_length=64, max_length=64)


def _require_token(authorization: str = Header(default=""),
                   x_veris_tenant: str = Header(default="")) -> _SandboxPrincipal:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Synthetic sandbox bearer token required.")
    try:
        tenant_id = sandbox._tenant_id(x_veris_tenant)
        expected = sandbox.local_service_access_token(tenant_id)
    except sandbox.SandboxError as exc:
        raise HTTPException(401, "Synthetic sandbox tenant authority is invalid.") from exc
    if not hmac.compare_digest(authorization[7:], expected):
        raise HTTPException(401, "Synthetic sandbox bearer token is invalid for this tenant.")
    return _SandboxPrincipal(tenant_id)


def _tenant(principal: _SandboxPrincipal = Depends(_require_token)) -> str:
    """Use only the tenant that the authenticated local service token binds."""
    return principal.tenant_id


def _require_local_admin(x_veris_sandbox_admin: str = Header(default="")) -> None:
    if (os.getenv("VERIS_MODE") or "local").strip().lower() != "local":
        raise HTTPException(403, "Synthetic sandbox controls are available only in local mode.")
    if not sandbox.verify_local_admin_token(x_veris_sandbox_admin):
        raise HTTPException(403, "A separate local sandbox administrator capability is required.")


@app.post("/oauth/token")
def oauth_token(grant_type: str = Form(...), client_id: str = Form(...),
                client_secret: str = Form(...), tenant_id: str = Form(...)):
    expected_client_id, expected_secret, _raw_secret = sandbox.local_service_credentials()
    if (grant_type != "client_credentials"
            or not hmac.compare_digest(client_id, expected_client_id)
            or not hmac.compare_digest(client_secret, expected_secret)):
        raise HTTPException(401, "Synthetic client credentials were not accepted.")
    tenant = sandbox._tenant_id(tenant_id)
    return {"access_token": sandbox.local_service_access_token(tenant), "token_type": "bearer",
            "expires_in": 300, "synthetic": True}


@app.get("/scenarios")
def scenarios(_token: _SandboxPrincipal = Depends(_require_token)):
    return {"scenarios": _service().scenarios(), "synthetic": True}


@app.get("/clock")
def get_clock(_token: _SandboxPrincipal = Depends(_require_token)):
    return _service().clock()


@app.post("/clock/advance")
def advance_clock(body: ClockIn, _token: _SandboxPrincipal = Depends(_require_token),
                  _admin: None = Depends(_require_local_admin)):
    return _service().advance_clock(body.days)


@app.post("/claims")
def submit_claim(body: ClaimIn, idempotency_key: str = Header(alias="Idempotency-Key"),
                 tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().submit_claim({**body.model_dump(), "tenant_id": tenant_id},
                                   idempotency_key=idempotency_key)


@app.get("/claims/{claim_id}")
def claim_status(claim_id: str, tenant_id: str = Depends(_tenant),
                 _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().claim_status(claim_id, tenant_id=tenant_id)


@app.post("/claims/{claim_id}/query-replies")
def reply_to_query(claim_id: str, body: RequestIn,
                   idempotency_key: str = Header(alias="Idempotency-Key"),
                   tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().reply_to_query(
        claim_id, body.model_dump(), tenant_id=tenant_id, idempotency_key=idempotency_key)


@app.post("/claims/{claim_id}/disputes")
def dispute_settlement(claim_id: str, body: RequestIn,
                       idempotency_key: str = Header(alias="Idempotency-Key"),
                       tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().dispute_settlement(
        claim_id, body.model_dump(), tenant_id=tenant_id, idempotency_key=idempotency_key)


@app.post("/claims/{claim_id}/events/query")
def open_claim_query(claim_id: str, tenant_id: str = Depends(_tenant),
                     _token: _SandboxPrincipal = Depends(_require_token),
                     _admin: None = Depends(_require_local_admin)):
    return {"webhook": _service().open_claim_query(claim_id, tenant_id=tenant_id), "synthetic": True}


@app.post("/claims/{claim_id}/events/decision")
def decide_claim(claim_id: str, tenant_id: str = Depends(_tenant),
                 _token: _SandboxPrincipal = Depends(_require_token),
                 _admin: None = Depends(_require_local_admin)):
    return {"webhook": _service().decide_claim(claim_id, tenant_id=tenant_id), "synthetic": True}


@app.post("/invoices")
def issue_invoice(body: InvoiceIn, idempotency_key: str = Header(alias="Idempotency-Key"),
                  tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().issue_invoice({**body.model_dump(), "tenant_id": tenant_id},
                                    idempotency_key=idempotency_key)


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, tenant_id: str = Depends(_tenant),
                _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().get_invoice(invoice_id, tenant_id=tenant_id)


@app.post("/invoices/{invoice_id}/pay")
def pay_invoice(invoice_id: str, body: InvoicePaymentIn,
                idempotency_key: str = Header(alias="Idempotency-Key"),
                tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().pay_invoice(
        invoice_id, tenant_id=tenant_id, expected_payee=body.expected_payee,
        expected_amount=body.expected_amount, idempotency_key=idempotency_key)


@app.post("/endorsements")
def request_endorsement(body: RequestIn, idempotency_key: str = Header(alias="Idempotency-Key"),
                        tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().request("ENDORSEMENT_REQUEST", {**body.model_dump(), "tenant_id": tenant_id},
                              idempotency_key=idempotency_key)


@app.post("/grievances")
def lodge_grievance(body: RequestIn, idempotency_key: str = Header(alias="Idempotency-Key"),
                    tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().request("GRIEVANCE", {**body.model_dump(), "tenant_id": tenant_id},
                              idempotency_key=idempotency_key)


@app.get("/bank/transfers/{transfer_id}/receipt")
def bank_transfer_receipt(transfer_id: str, tenant_id: str = Depends(_tenant),
                          _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().bank_transfer_receipt(transfer_id, tenant_id=tenant_id)


@app.get("/bank/accounts/{account_id}/statement")
def bank_statement(account_id: str, tenant_id: str = Depends(_tenant),
                   _token: _SandboxPrincipal = Depends(_require_token)):
    return _service().bank_statement(account_id, tenant_id=tenant_id)


@app.get("/operations")
def operation_log(tenant_id: str = Depends(_tenant), _token: _SandboxPrincipal = Depends(_require_token)):
    service = _service()
    return {"operations": service.operation_log(tenant_id),
            "verification": service.verify_operation_log(tenant_id),
            "synthetic": True}


@app.post("/webhooks/verify")
def verify_webhook(body: WebhookIn, principal: _SandboxPrincipal = Depends(_require_token)):
    if str(body.event.get("tenant_id") or "") != principal.tenant_id:
        raise HTTPException(403, "Synthetic webhook tenant does not match the authenticated local service.")
    return {"valid": _service().verify_webhook(body.event, body.signature), "synthetic": True}
