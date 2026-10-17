"""Deterministic tool planning and permission enforcement for the VERIS agent.

The runtime does not make insurance or financial decisions.  It can read evidence,
compute from supplied deterministic engines, prepare drafts, and queue a side effect for
one exact user confirmation.  The language-model boundary may propose a sequence only
when it is bound to explicit user intent; the deterministic planner remains the primary
and complete demo path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import IntEnum
from time import monotonic
from typing import Any, Callable, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import persistence
from .store import AgentStore, AgentStoreConflict, AgentStoreValidationError


class AgentPolicyError(RuntimeError):
    """A user-safe refusal from the agent policy engine."""

    http_status = 403


class Tier(IntEnum):
    OBSERVE = 0
    PREPARE = 1
    ACT = 2
    NEVER = 3


class SideEffect(str):
    OBSERVE = "OBSERVE"
    DRAFT = "DRAFT"
    EXTERNAL = "EXTERNAL"


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArgs(ToolArgs):
    pass


class PolicyArgs(ToolArgs):
    policy_id: str = Field(min_length=1, max_length=160)


class ClaimDraftArgs(PolicyArgs):
    member_id: Optional[str] = Field(default=None, max_length=160)
    claim_type: str = Field(default="HEALTH_REIMBURSEMENT", min_length=1, max_length=80)


class ClaimReferenceArgs(ToolArgs):
    claim_id: str = Field(min_length=1, max_length=160)


class ClaimSubmissionArgs(ClaimReferenceArgs):
    """Claim and verified packet manifest bound into one submission confirmation."""

    packet_manifest_sha256: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class ClaimReplyArgs(ClaimReferenceArgs):
    """The exact prepared query reply is visible and bound before it is sent."""

    reply_text: str = Field(min_length=1, max_length=4_000)
    reply_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class SettlementDisputeArgs(ClaimReferenceArgs):
    """A settlement dispute carries its visible draft and cited deduction identifiers."""

    deduction_ids: list[str] = Field(min_length=1, max_length=16)
    dispute_text: str = Field(min_length=1, max_length=4_000)
    dispute_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class InvoicePayArgs(ToolArgs):
    """Server-resolved invoice snapshot, never caller-supplied payment terms."""

    invoice_id: str = Field(min_length=1, max_length=160)
    invoice_payee: str = Field(min_length=1, max_length=240)
    invoice_amount: str = Field(min_length=1, max_length=80)
    invoice_tenant_id: str = Field(min_length=1, max_length=160)


class GrievanceArgs(ClaimReferenceArgs):
    """A claim escalation produced only by the cited overdue-SLA gate."""

    ladder_entry_id: str = Field(min_length=1, max_length=160)
    deadline_due_at: str = Field(min_length=1, max_length=80)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    tier: Tier
    required_scope: str
    side_effect: str
    args_model: type[ToolArgs]
    result_model: type[ToolResult]
    idempotency_key: str


# Tier 3 is absent by construction: no tool can decide a claim, quote a premium, sign a
# declaration or choose a product.  Adding one requires changing this registry and its
# corresponding invariant test, rather than a model prompt or a client flag.
TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "portfolio.read": ToolDefinition("portfolio.read", Tier.OBSERVE, "agent:read",
                                      SideEffect.OBSERVE, EmptyArgs, ToolResult,
                                      "tenant-and-portfolio-revision"),
    "policy.layout": ToolDefinition("policy.layout", Tier.OBSERVE, "agent:read",
                                     SideEffect.OBSERVE, PolicyArgs, ToolResult,
                                     "tenant-policy-id"),
    "evidence.search": ToolDefinition("evidence.search", Tier.OBSERVE, "agent:read",
                                       SideEffect.OBSERVE, PolicyArgs, ToolResult,
                                       "tenant-policy-id"),
    "deadline.compute": ToolDefinition("deadline.compute", Tier.OBSERVE, "agent:read",
                                        SideEffect.OBSERVE, PolicyArgs, ToolResult,
                                        "tenant-policy-id"),
    "health.what_if": ToolDefinition("health.what_if", Tier.OBSERVE, "agent:read",
                                      SideEffect.OBSERVE, PolicyArgs, ToolResult,
                                      "tenant-policy-id-and-operands"),
    "claim.status": ToolDefinition("claim.status", Tier.OBSERVE, "agent:read",
                                    SideEffect.OBSERVE, ClaimReferenceArgs, ToolResult,
                                    "tenant-claim-id"),
    "invoice.list": ToolDefinition("invoice.list", Tier.OBSERVE, "agent:read",
                                    SideEffect.OBSERVE, EmptyArgs, ToolResult,
                                    "tenant-invoices"),
    "claim.prepare": ToolDefinition("claim.prepare", Tier.PREPARE, "agent:prepare",
                                     SideEffect.DRAFT, ClaimDraftArgs, ToolResult,
                                     "tenant-policy-member-claim-type"),
    "query.draft": ToolDefinition("query.draft", Tier.PREPARE, "agent:prepare",
                                   SideEffect.DRAFT, ClaimReferenceArgs, ToolResult,
                                   "tenant-claim-id"),
    "letter.draft": ToolDefinition("letter.draft", Tier.PREPARE, "agent:prepare",
                                    SideEffect.DRAFT, ClaimDraftArgs, ToolResult,
                                    "tenant-policy-member-claim-type"),
    "reminder.draft": ToolDefinition("reminder.draft", Tier.PREPARE, "agent:prepare",
                                      SideEffect.DRAFT, ClaimReferenceArgs, ToolResult,
                                      "tenant-claim-id"),
    "claim.submit": ToolDefinition("claim.submit", Tier.ACT, "agent:act",
                                    SideEffect.EXTERNAL, ClaimSubmissionArgs, ToolResult,
                                    "tenant-claim-id-and-packet-manifest"),
    "claim.reply": ToolDefinition("claim.reply", Tier.ACT, "agent:act",
                                   SideEffect.EXTERNAL, ClaimReplyArgs, ToolResult,
                                   "tenant-claim-id-and-reply-hash"),
    "invoice.pay": ToolDefinition("invoice.pay", Tier.ACT, "agent:act",
                                   SideEffect.EXTERNAL, InvoicePayArgs, ToolResult,
                                   "tenant-invoice-id"),
    "endorsement.request": ToolDefinition("endorsement.request", Tier.ACT, "agent:act",
                                           SideEffect.EXTERNAL, PolicyArgs, ToolResult,
                                           "tenant-policy-id-and-request-hash"),
    "grievance.lodge": ToolDefinition("grievance.lodge", Tier.ACT, "agent:act",
                                       SideEffect.EXTERNAL, GrievanceArgs, ToolResult,
                                       "tenant-claim-id-and-cited-escalation"),
    "settlement.dispute": ToolDefinition("settlement.dispute", Tier.ACT, "agent:act",
                                          SideEffect.EXTERNAL, SettlementDisputeArgs, ToolResult,
                                          "tenant-claim-id-and-dispute-hash"),
}

MAX_PLAN_STEPS = 8
MAX_PLAN_SECONDS = 10.0
MAX_PLAN_TOKENS = 800


class AgentRuntime:
    """Policy engine and deterministic planner over a tenant-scoped AgentStore."""

    def __init__(self, store: AgentStore):
        self.store = store

    @staticmethod
    def tool_catalogue() -> list[dict[str, Any]]:
        return [
            {
                "name": definition.name,
                "tier": int(definition.tier),
                "required_scope": definition.required_scope,
                "side_effect": definition.side_effect,
                "idempotency_key": definition.idempotency_key,
            }
            for definition in TOOL_REGISTRY.values()
        ]

    @staticmethod
    def _definition(tool_name: str) -> ToolDefinition:
        definition = TOOL_REGISTRY.get(str(tool_name))
        if definition is None:
            raise AgentPolicyError("That action is not available to the VERIS agent.")
        return definition

    @staticmethod
    def _actor(user_id: str, mandate_id: Optional[str] = None) -> str:
        return f"agent:veris on behalf of user:{user_id} under mandate:{mandate_id or 'none'}"

    def _require_tool_permission(self, scope: persistence.TenantScope,
                                 definition: ToolDefinition) -> None:
        if scope.role != "SME":
            raise AgentPolicyError("Only the portfolio owner can run the household agent.")
        if not self.store.has_consent(scope.tenant_id, definition.required_scope):
            raise AgentPolicyError(
                "Agent consent for this scope is not active. No tool was called.")

    @staticmethod
    def _parse_args(definition: ToolDefinition, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return definition.args_model.model_validate(dict(payload)).model_dump()
        except ValidationError as exc:
            raise AgentStoreValidationError("The tool payload is invalid.") from exc

    @staticmethod
    def _step(definition: ToolDefinition, summary: str) -> dict[str, Any]:
        return {
            "tool": definition.name,
            "tier": int(definition.tier),
            "side_effect": definition.side_effect,
            "summary": summary,
            "state": "NEEDS_YOUR_OK" if definition.tier == Tier.ACT else "READY",
        }

    def plan(self, scope: persistence.TenantScope, *, intent: str,
             origin: str = "USER") -> dict[str, Any]:
        """Build a bounded plan only from explicit user intent, never document text."""
        started = monotonic()
        if str(origin).upper() != "USER":
            raise AgentPolicyError("Document text cannot originate an agent tool call.")
        if scope.role != "SME":
            raise AgentPolicyError("Only the portfolio owner can plan household agent work.")
        text = " ".join(str(intent or "").split())[:500]
        lowered = text.lower()
        if not text:
            raise AgentStoreValidationError("Describe the task you want the agent to prepare.")
        if any(word in lowered for word in ("surrender", "cancel", "switch", "buy", "recommend")):
            return {
                "mode": "DETERMINISTIC", "intent": text, "steps": [], "refused": True,
                "reason": ("VERIS can quote policy terms and prepare administrative drafts, "
                           "but it cannot recommend buying, cancelling, switching or surrendering."),
                "budgets": {"max_steps": MAX_PLAN_STEPS, "max_seconds": MAX_PLAN_SECONDS,
                            "max_tokens": MAX_PLAN_TOKENS},
            }
        if ("motor claim" in lowered or "vehicle claim" in lowered
                or "car accident" in lowered or "motor own damage" in lowered):
            tools = (
                ("portfolio.read", "Read the tenant-scoped motor policy and vehicle record."),
                ("policy.layout", "Read the selected motor policy layout and citations."),
                ("deadline.compute", "Compute only a documented motor notification timing."),
                ("claim.prepare", "Prepare the reversible motor checklist and surveyor packet."),
                ("letter.draft", "Prepare a cover-letter draft for the owner to review."),
                ("claim.submit", "Submitting to the synthetic insurer needs your OK."),
            )
        elif "claim" in lowered or "admitted" in lowered or "hospital" in lowered:
            tools = (
                ("portfolio.read", "Read the tenant-scoped portfolio."),
                ("policy.layout", "Read the selected health policy layout and citations."),
                ("deadline.compute", "Compute only a documented notice deadline."),
                ("claim.prepare", "Prepare a reversible claim checklist and packet draft."),
                ("letter.draft", "Prepare a cover-letter draft for the owner to review."),
                ("claim.submit", "Submitting to the synthetic insurer needs your OK."),
            )
        elif "settlement" in lowered or "deduction" in lowered or "dispute" in lowered:
            tools = (
                ("claim.status", "Read the current sandbox claim status."),
                ("health.what_if", "Recompute the deterministic illustration from cited operands."),
                ("query.draft", "Prepare a non-adjudicating query or dispute draft."),
                ("settlement.dispute", "Sending the draft to the synthetic insurer needs your OK."),
            )
        else:
            tools = (
                ("portfolio.read", "Read the tenant-scoped portfolio."),
                ("evidence.search", "Search policy evidence within the tenant boundary."),
            )
        if len(tools) > MAX_PLAN_STEPS or monotonic() - started > MAX_PLAN_SECONDS:
            raise AgentPolicyError("The deterministic agent plan exceeded its safety budget.")
        steps = [self._step(self._definition(name), summary) for name, summary in tools]
        plan = {
            "mode": "DETERMINISTIC", "intent": text, "steps": steps, "refused": False,
            "budgets": {"max_steps": MAX_PLAN_STEPS, "max_seconds": MAX_PLAN_SECONDS,
                        "max_tokens": MAX_PLAN_TOKENS},
        }
        self.store.append_ledger(
            scope.tenant_id, kind="PLAN", actor=self._actor(scope.user_id),
            detail={"mode": "DETERMINISTIC", "intent_hash": self._intent_hash(text),
                    "tools": [step["tool"] for step in steps], "origin": "USER"},
        )
        return plan

    @staticmethod
    def _intent_hash(value: str) -> str:
        import hashlib
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def prepare(self, scope: persistence.TenantScope, *, tool_name: str,
                payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run tier 0/1 work or create an exact-payload tier-2 confirmation."""
        return self._prepare(scope, tool_name=tool_name, payload=payload,
                             escalation_gated=False)

    def prepare_escalation(self, scope: persistence.TenantScope, *,
                           payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a grievance confirmation only after the API's cited overdue-SLA gate."""
        return self._prepare(scope, tool_name="grievance.lodge", payload=payload,
                             escalation_gated=True)

    def _prepare(self, scope: persistence.TenantScope, *, tool_name: str,
                 payload: Mapping[str, Any], escalation_gated: bool) -> dict[str, Any]:
        definition = self._definition(tool_name)
        self._require_tool_permission(scope, definition)
        if definition.name == "grievance.lodge" and not escalation_gated:
            raise AgentPolicyError(
                "A grievance can be prepared only from the cited overdue-claim escalation gate.")
        parsed = self._parse_args(definition, payload)
        actor = self._actor(scope.user_id)
        if definition.tier == Tier.ACT:
            summary = self._confirmation_summary(definition, parsed)
            pending = self.store.create_pending_action(
                scope.tenant_id, tool_name=definition.name, payload=parsed, summary=summary)
            pending["preview"] = self._confirmation_preview(
                scope, definition, {**pending, "payload": parsed})
            self.store.append_ledger(
                scope.tenant_id, kind="PENDING_CONFIRMATION", actor=actor,
                detail={"tool": definition.name, "tier": int(definition.tier),
                        "pending_action_id": pending["id"], "payload_hash": pending["payload_hash"]},
            )
            return {"state": "NEEDS_YOUR_OK", "pending_action": pending,
                    "tier": int(definition.tier), "mode": "DETERMINISTIC"}
        result = self._local_result(definition, parsed)
        self.store.append_ledger(
            scope.tenant_id, kind="TOOL_RESULT", actor=actor,
            detail={"tool": definition.name, "tier": int(definition.tier),
                    "state": result.state, "data_hash": self._intent_hash(str(result.data))},
        )
        return {"tier": int(definition.tier), "mode": "DETERMINISTIC", **result.model_dump()}

    @staticmethod
    def _local_result(definition: ToolDefinition, payload: Mapping[str, Any]) -> ToolResult:
        if definition.name == "claim.prepare":
            motor_claim = str(payload.get("claim_type") or "").upper() == "MOTOR_OWN_DAMAGE"
            checklist = (
                [
                    {"item": "Policy schedule", "reason": "Identifies the motor policy and insured vehicle."},
                    {"item": "Vehicle registration", "reason": "Records the vehicle identifier for the surveyor packet."},
                    {"item": "Repair estimate or invoice", "reason": "Records the declared repair evidence for review."},
                ] if motor_claim else [
                    {"item": "Policy schedule", "reason": "Identifies the policy and insured member."},
                    {"item": "Hospital discharge summary", "reason": "Describes the admission record."},
                    {"item": "Itemised hospital bills", "reason": "Supports the expense record."},
                ])
            return ToolResult(
                state="DRAFTED",
                summary=("A reversible motor-claim checklist is ready; the synthetic surveyor step remains counterparty-owned."
                         if motor_claim else
                         "A reversible health-claim checklist is ready. It has not been submitted."),
                data={"policy_id": payload["policy_id"], "checklist": checklist},
            )
        if definition.name == "letter.draft":
            return ToolResult(
                state="DRAFTED",
                summary="A claim cover-letter draft is ready for owner review; it makes no coverage assertion.",
                data={"policy_id": payload["policy_id"], "draft": (
                    "Please find the listed documents for review. This is a request to register "
                    "the claim; VERIS does not state an outcome.")},
            )
        if definition.name == "deadline.compute":
            return ToolResult(
                state="INFORMATION_MISSING",
                summary=("No deadline is calculated until a policy notice clause and its date operands "
                         "are selected from evidence."),
                data={"policy_id": payload["policy_id"], "required_document": "the policy wording"},
            )
        return ToolResult(
            state="COMPLETED",
            summary="The deterministic read-only step completed inside VERIS.",
            data=dict(payload),
        )

    @staticmethod
    def _confirmation_summary(definition: ToolDefinition, payload: Mapping[str, Any]) -> str:
        if definition.name == "claim.submit":
            return "Submit this prepared claim to the synthetic sandbox insurer."
        if definition.name == "settlement.dispute":
            return "Send this prepared settlement query to the synthetic sandbox insurer."
        if definition.name == "invoice.pay":
            return f"Pay the resolved insurer invoice to {payload['invoice_payee']} in the sandbox only."
        if definition.name == "grievance.lodge":
            return "Send this cited overdue-claim escalation to the synthetic sandbox insurer."
        return f"Perform {definition.name} with the exact reviewed payload."

    @staticmethod
    def _canonical_preview_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return str(value)

    def _confirmation_preview(self, scope: persistence.TenantScope,
                              definition: ToolDefinition,
                              pending: Mapping[str, Any]) -> dict[str, Any]:
        """Return the tenant-bound, canonical action terms that the owner reviews.

        The preview is derived server-side from the same encrypted payload whose canonical
        representation is hashed.  Clients receive no mutable action payload to alter or
        reinterpret before step-up.
        """
        payload = pending["payload"]
        if not isinstance(payload, Mapping):
            raise AgentStoreValidationError("The pending action payload is malformed.")
        canonical_payload = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        fields = [
            {"name": str(name), "value": self._canonical_preview_value(value)}
            for name, value in sorted(payload.items())
        ]
        if definition.name == "invoice.pay":
            mandate = self.store.active_mandate(
                scope.tenant_id, scope="invoice.pay", payee=str(payload["invoice_payee"]),
                amount=str(payload["invoice_amount"]), notice_at=str(pending.get("created_at") or ""))
            fields.append({"name": "mandate_status", "value": "MATCHED" if mandate else "MISSING"})
            if mandate is not None:
                fields.extend([
                    {"name": "mandate_id", "value": str(mandate["id"])},
                    {"name": "mandate_scope", "value": "invoice.pay"},
                    {"name": "mandate_per_payment_cap", "value": str(mandate["per_payment_cap"])},
                    {"name": "mandate_payee_allowlist",
                     "value": self._canonical_preview_value(mandate["payee_allowlist"])},
                    {"name": "mandate_valid_until", "value": str(mandate["valid_until"])},
                    {"name": "mandate_advance_notice",
                     "value": self._canonical_preview_value(mandate["advance_notice_required"])},
                ])
        return {
            "tenant_id": scope.tenant_id,
            "tenant_scoped": True,
            "payload_hash": str(pending["payload_hash"]),
            "canonical_payload": canonical_payload,
            "fields": fields,
        }

    def confirmation_inbox(self, scope: persistence.TenantScope) -> list[dict[str, Any]]:
        """Expose only canonical previews from the signed-in tenant's pending actions."""
        confirmations = []
        for pending in self.store.list_pending_actions(scope.tenant_id):
            definition = self._definition(str(pending["tool_name"]))
            confirmations.append({
                key: pending[key] for key in (
                    "id", "tool_name", "payload_hash", "summary", "expires_at", "created_at")
            } | {"preview": self._confirmation_preview(scope, definition, pending)})
        return confirmations

    def _mandate_for_pending(self, scope: persistence.TenantScope,
                             definition: ToolDefinition, pending: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Resolve the mandate that authorises a mandate-gated tier-2 action."""
        if definition.name != "invoice.pay":
            return None
        payload = pending["payload"]
        if str(payload.get("invoice_tenant_id") or "") != scope.tenant_id:
            raise AgentPolicyError("The resolved sandbox invoice does not belong to this tenant.")
        mandate = self.store.active_mandate(
            scope.tenant_id, scope="invoice.pay", payee=str(payload["invoice_payee"]),
            amount=str(payload["invoice_amount"]), notice_at=str(pending.get("created_at") or ""))
        if mandate is None:
            raise AgentPolicyError(
                "A live sandbox mandate with a matching scope, payee, cap, window and notice is required.")
        return mandate

    def confirm(self, scope: persistence.TenantScope, *, action_id: str, payload_hash: str,
                step_up_verified: bool,
                executor: Callable[[str, Mapping[str, Any], persistence.TenantScope], Mapping[str, Any]]) -> dict[str, Any]:
        """Consume a single-use confirmation and run a registered deterministic connector."""
        if not step_up_verified:
            raise AgentPolicyError("Step-up authentication is required to confirm this action.")
        pending = self.store.pending_action(scope.tenant_id, action_id)
        definition = self._definition(pending["tool_name"])
        self._require_tool_permission(scope, definition)
        if definition.tier != Tier.ACT:
            raise AgentPolicyError("Only tier-2 actions can be confirmed.")
        mandate = self._mandate_for_pending(scope, definition, pending)
        consumed = self.store.consume_pending_action(scope.tenant_id, action_id, payload_hash)
        mandate_id = str(mandate["id"]) if mandate is not None else None
        actor = self._actor(scope.user_id, mandate_id)
        self.store.append_ledger(
            scope.tenant_id, kind="CONFIRMED", actor=actor,
            detail={"tool": definition.name, "pending_action_id": action_id,
                    "payload_hash": consumed["payload_hash"], "step_up": True,
                    "mandate_id": mandate_id},
        )
        if mandate is not None:
            self.store.append_ledger(
                scope.tenant_id, kind="MANDATE_AUTHORISED", actor=actor,
                detail={"tool": definition.name, "mandate_id": mandate_id,
                        "scope": "invoice.pay", "payee": consumed["payload"]["invoice_payee"],
                        "amount": consumed["payload"]["invoice_amount"],
                        "invoice_id": consumed["payload"]["invoice_id"],
                        "advance_notice_required": mandate["advance_notice_required"]},
            )
        try:
            result = dict(executor(definition.name, consumed["payload"], scope))
        except Exception as exc:  # connector errors are recorded, never reported as success
            self.store.append_ledger(
                scope.tenant_id, kind="TOOL_ERROR", actor=actor,
                detail={"tool": definition.name, "pending_action_id": action_id,
                        "error": type(exc).__name__},
            )
            raise
        self.store.append_ledger(
            scope.tenant_id, kind="TOOL_RESULT", actor=actor,
            detail={"tool": definition.name, "pending_action_id": action_id,
                    "result_hash": self._intent_hash(str(result)), "mandate_id": mandate_id},
        )
        state = "SENT" if result.get("accepted", True) else "NOT_SENT"
        return {"state": state, "tier": int(definition.tier), "result": result,
                "confirmation": {key: consumed[key] for key in ("id", "payload_hash", "consumed_at")}}

    def model_sequence_is_allowed(self, *, user_intent: str, origin: str,
                                  proposed_tools: Sequence[str]) -> bool:
        """Gate a model proposal without granting it decision authority.

        The API does not call a provider for the demo path.  This explicit gate exists so a
        future provider integration can only offer known tools tied to user text, with the
        same bounded deterministic planner still available when profile ``none`` is used.
        """
        if str(origin).upper() != "USER" or not str(user_intent).strip():
            return False
        return (len(proposed_tools) <= MAX_PLAN_STEPS
                and all(str(name) in TOOL_REGISTRY for name in proposed_tools))
