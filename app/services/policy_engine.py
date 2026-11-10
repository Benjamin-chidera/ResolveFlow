import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
from pydantic import BaseModel
from sqlmodel import Session, select
from app.agents.state import InvestigationWorkflowState
from app.core.config import settings
from app.models.account import CustomerAccount
from app.models.case import ApprovalRequest, AuditEvent, InvestigationCase
from app.models.enums import (
    AccountStatus,
    AllocationStatus,
    ApprovalDecision,
    CaseState,
    EventVisibility,
    ProposedAction,
    ProviderPaymentStatus,
)
from app.models.ledger import JournalEntry
from app.models.payment import IncomingPayment
from app.services.case_event_helper import append_case_event
from app.services.case_service import transition_case_state
from app.services.ledger_service import (
    DuplicateAllocationError,
    post_customer_allocation,
)
from app.services.reference_parser import parse_beneficiary_account_id


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PolicyExecutionResult(BaseModel):
    """Structured result returned after the LangGraph workflow and Policy Engine run."""

    case_id: str
    case_state: str
    matched_payment_id: Optional[str] = None
    provider_status: Optional[str] = None
    proposed_action: str
    policy_approved: bool
    requires_human_approval: bool
    execution_status: str  # EXECUTED, NOT_EXECUTED, AWAITING_HUMAN_APPROVAL, IDEMPOTENT_NOOP
    journal_id: Optional[str] = None
    approval_id: Optional[str] = None
    reconciliation_flags: List[str] = []
    suspected_root_cause: str
    investigation_summary: str
    evidence_refs: List[str] = []


def _create_or_get_pending_approval(
    session: Session,
    case: InvestigationCase,
    payment_id: Optional[str],
    proposed_action: str,
) -> ApprovalRequest:
    existing = session.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.case_id == case.case_id,
            ApprovalRequest.decision == ApprovalDecision.PENDING,
        )
    ).first()
    if existing is not None:
        existing.payment_id = payment_id
        existing.proposed_action = proposed_action
        existing.evidence_version = case.latest_evidence_version
        session.add(existing)
        session.flush()
        return existing

    short_id = uuid4().hex[:6].upper()
    approval = ApprovalRequest(
        approval_id=f"APP-{short_id}",
        case_id=case.case_id,
        payment_id=payment_id,
        proposed_action=proposed_action,
        evidence_version=case.latest_evidence_version,
        decision=ApprovalDecision.PENDING,
        created_at=get_utc_now(),
    )
    session.add(approval)
    session.flush()
    return approval


def evaluate_and_execute_policy(
    session: Session,
    case: InvestigationCase,
    workflow_state: InvestigationWorkflowState,
) -> PolicyExecutionResult:
    """Deterministic Policy Engine (Spec 05).

    The LLM/LangGraph nodes only investigate and propose; this service owns all
    financial authorization, re-reads database records under lock, enforces the
    5 non-negotiable banking invariants, and executes balanced journal postings.
    """
    # Ensure case is in INVESTIGATING state before applying final policy outcome
    if case.state in (CaseState.OPEN, CaseState.WAITING_PROVIDER, CaseState.AWAITING_REVIEW):
        transition_case_state(
            session=session,
            case=case,
            target_state=CaseState.INVESTIGATING,
            actor="InvestigationWorkflow",
            details="Running LangGraph trace, reconciliation, and policy check",
        )

    # Persist node audit entries into the authoritative audit_events table
    for entry in workflow_state.get("audit_entries", []):
        session.add(
            AuditEvent(
                actor=entry["actor"],
                action=entry["action"],
                details=f"[{case.case_id}] {entry['details']}",
                timestamp=get_utc_now(),
            )
        )

    matched_payment_id = workflow_state.get("matched_payment_id")
    proposed_action = workflow_state.get("proposed_action") or ProposedAction.HUMAN_REVIEW
    flags = list(workflow_state.get("reconciliation_flags", []))
    evidence_ids = list(workflow_state.get("evidence_ids", []))
    summary = workflow_state.get("investigation_summary", "")
    root_cause = workflow_state.get("suspected_root_cause", "UNKNOWN")
    duplicate_noop = bool(workflow_state.get("duplicate_noop", False))

    findings_payload: Dict[str, Any] = {
        "provider_receipt_found": matched_payment_id is not None,
        "payment_id": matched_payment_id,
        "provider_status": workflow_state.get("provider_status"),
        "receipt_amount_minor": workflow_state.get("receipt_amount_minor"),
        "account_status": workflow_state.get("account_status"),
        "candidate_accounts": workflow_state.get("candidate_account_ids", []),
        "reconciliation_flags": flags,
        "suspected_root_cause": root_cause,
        "investigation_summary": summary,
        "funds_in_suspense": workflow_state.get("funds_in_suspense", False),
    }

    case.findings_json = json.dumps(findings_payload)
    case.evidence_refs = json.dumps(evidence_ids)
    case.proposed_action = proposed_action
    case.processed_evidence_version = case.latest_evidence_version
    case.active_worker_lease_id = None
    case.lease_expires_at = None
    case.updated_at = get_utc_now()
    session.add(case)

    # Case A: Scenario S4 — Duplicate event / Already credited (Idempotent No-Op)
    if duplicate_noop and matched_payment_id:
        existing_alloc = session.exec(
            select(JournalEntry).where(
                JournalEntry.idempotency_key == f"ALLOC-{matched_payment_id}"
            )
        ).first()
        transition_case_state(
            session=session,
            case=case,
            target_state=CaseState.RESOLVED,
            actor="DeterministicPolicyEngine",
            details=f"Idempotent no-op: payment {matched_payment_id} already allocated",
        )
        append_case_event(
            session=session,
            case_id=case.case_id,
            event_type="CASE_RESOLVED",
            visibility=EventVisibility.CUSTOMER_SAFE,
            payload={
                "case_id": case.case_id,
                "status": CaseState.RESOLVED.value,
                "message": (
                    f"We confirmed your transfer ({case.reference}) was already credited "
                    f"to your account ({case.beneficiary_account_id})."
                ),
            },
        )
        session.add(
            AuditEvent(
                actor="DeterministicPolicyEngine",
                action="DUPLICATE_CREDIT_NOOP",
                before_state="ALLOCATED",
                after_state="ALLOCATED",
                details=f"[{case.case_id}] Prevented duplicate credit for {matched_payment_id}",
                timestamp=get_utc_now(),
            )
        )
        session.commit()
        return PolicyExecutionResult(
            case_id=case.case_id,
            case_state=case.state.value,
            matched_payment_id=matched_payment_id,
            provider_status=workflow_state.get("provider_status"),
            proposed_action=proposed_action.value,
            policy_approved=True,
            requires_human_approval=False,
            execution_status="IDEMPOTENT_NOOP",
            journal_id=existing_alloc.journal_id if existing_alloc else None,
            reconciliation_flags=flags,
            suspected_root_cause=root_cause,
            investigation_summary=summary,
            evidence_refs=evidence_ids,
        )

    # Case B: Scenario S2 — No Receipt or Pending on Rail
    if proposed_action in (ProposedAction.TRACE_WITH_PROVIDER, ProposedAction.WAIT_AND_RECHECK):
        transition_case_state(
            session=session,
            case=case,
            target_state=CaseState.WAITING_PROVIDER,
            actor="DeterministicPolicyEngine",
            details=f"No authoritative settled receipt; routed to {proposed_action.value}",
        )
        append_case_event(
            session=session,
            case_id=case.case_id,
            event_type="TRACE_PROGRESS",
            visibility=EventVisibility.CUSTOMER_SAFE,
            payload={
                "case_id": case.case_id,
                "status": CaseState.WAITING_PROVIDER.value,
                "message": (
                    f"We checked our clearing records for reference {case.reference} and have "
                    f"not received settlement from {case.sender_bank or 'the sending bank'} yet. "
                    f"We have opened a provider trace under case #{case.case_id}."
                ),
            },
        )
        session.add(
            AuditEvent(
                actor="DeterministicPolicyEngine",
                action="PROVIDER_TRACE_OPENED",
                before_state="INVESTIGATING",
                after_state=CaseState.WAITING_PROVIDER.value,
                details=(
                    f"[{case.case_id}] Strictly refused credit without authoritative receipt; "
                    f"action={proposed_action.value}"
                ),
                timestamp=get_utc_now(),
            )
        )
        session.commit()
        return PolicyExecutionResult(
            case_id=case.case_id,
            case_state=case.state.value,
            matched_payment_id=matched_payment_id,
            provider_status=workflow_state.get("provider_status"),
            proposed_action=proposed_action.value,
            policy_approved=False,
            requires_human_approval=False,
            execution_status="NOT_EXECUTED",
            reconciliation_flags=flags,
            suspected_root_cause=root_cause,
            investigation_summary=summary,
            evidence_refs=evidence_ids,
        )

    # Case C: Proposed AUTO_ALLOCATE — Must pass all 5 Non-Negotiable Policy Invariants
    if proposed_action == ProposedAction.AUTO_ALLOCATE and matched_payment_id:
        # Invariant 1: Fresh re-read of payment and target account under DB transaction
        fresh_payment = session.exec(
            select(IncomingPayment).where(IncomingPayment.payment_id == matched_payment_id)
        ).first()
        fresh_account = session.exec(
            select(CustomerAccount).where(
                CustomerAccount.account_id == case.beneficiary_account_id
            )
        ).first()

        parsed_wire_account = (
            parse_beneficiary_account_id(fresh_payment.raw_reference)
            if fresh_payment
            else None
        )

        # Verify Invariants 2, 3, 4, 5 deterministically
        invariant_violations: List[str] = []
        if fresh_payment is None:
            invariant_violations.append("FRESH_PAYMENT_MISSING")
        else:
            if fresh_payment.provider_status != ProviderPaymentStatus.SETTLED:
                invariant_violations.append("PAYMENT_NOT_SETTLED")
            if fresh_payment.allocation_status != AllocationStatus.UNALLOCATED:
                invariant_violations.append("PAYMENT_NOT_UNALLOCATED")
            if fresh_payment.amount_minor != case.amount_minor:
                invariant_violations.append("AMOUNT_MISMATCH")
            if fresh_payment.amount_minor >= settings.HIGH_VALUE_THRESHOLD_CENTS:
                invariant_violations.append("EXCEEDS_HIGH_VALUE_THRESHOLD")
            if parsed_wire_account != case.beneficiary_account_id:
                invariant_violations.append("UNVERIFIED_WIRE_BENEFICIARY")

        if fresh_account is None or fresh_account.status != AccountStatus.ACTIVE:
            invariant_violations.append("BENEFICIARY_ACCOUNT_NOT_ACTIVE")

        if not invariant_violations and fresh_payment and fresh_account:
            try:
                journal = post_customer_allocation(
                    session=session,
                    payment_id=fresh_payment.payment_id,
                    beneficiary_account_id=fresh_account.account_id,
                    amount_minor=fresh_payment.amount_minor,
                    case_reference=case.case_id,
                )
                fresh_payment.allocation_status = AllocationStatus.ALLOCATED
                session.add(fresh_payment)

                findings_payload["suspense_cleared"] = True
                case.findings_json = json.dumps(findings_payload)
                session.add(case)

                transition_case_state(
                    session=session,
                    case=case,
                    target_state=CaseState.RESOLVED,
                    actor="DeterministicPolicyEngine",
                    details=(
                        f"Allocated {fresh_payment.payment_id} to {fresh_account.account_id} "
                        f"under journal {journal.journal_id}"
                    ),
                )
                formatted_amount = f"{fresh_payment.amount_minor / 100:.2f}"
                append_case_event(
                    session=session,
                    case_id=case.case_id,
                    event_type="CASE_RESOLVED",
                    visibility=EventVisibility.CUSTOMER_SAFE,
                    payload={
                        "case_id": case.case_id,
                        "status": CaseState.RESOLVED.value,
                        "journal_id": journal.journal_id,
                        "amount_minor": fresh_payment.amount_minor,
                        "currency": fresh_payment.currency,
                        "beneficiary_account_id": fresh_account.account_id,
                        "message": (
                            f"We verified your transfer ({case.reference}) and credited "
                            f"£{formatted_amount} to your account ({fresh_account.account_id})."
                        ),
                    },
                )
                session.add(
                    AuditEvent(
                        actor="DeterministicPolicyEngine",
                        action="AUTO_ALLOCATE_SUCCESS",
                        before_state=AllocationStatus.UNALLOCATED.value,
                        after_state=AllocationStatus.ALLOCATED.value,
                        details=(
                            f"[{case.case_id}] Journal {journal.journal_id} posted. "
                            f"Credited {fresh_payment.amount_minor} {fresh_payment.currency} "
                            f"to {fresh_account.account_id}."
                        ),
                        timestamp=get_utc_now(),
                    )
                )
                session.commit()
                return PolicyExecutionResult(
                    case_id=case.case_id,
                    case_state=case.state.value,
                    matched_payment_id=fresh_payment.payment_id,
                    provider_status=fresh_payment.provider_status.value,
                    proposed_action=ProposedAction.AUTO_ALLOCATE.value,
                    policy_approved=True,
                    requires_human_approval=False,
                    execution_status="EXECUTED",
                    journal_id=journal.journal_id,
                    reconciliation_flags=flags,
                    suspected_root_cause=root_cause,
                    investigation_summary=summary,
                    evidence_refs=evidence_ids,
                )
            except DuplicateAllocationError:
                invariant_violations.append("DUPLICATE_ALLOCATION_KEY_REJECTED")

        # If any invariant failed during fresh re-read, override to HUMAN_REVIEW
        flags.extend(invariant_violations)
        proposed_action = ProposedAction.HUMAN_REVIEW
        case.proposed_action = ProposedAction.HUMAN_REVIEW

    # Case D: HUMAN_REVIEW (Scenarios S3 Ambiguous, S5 Frozen, or Policy Override)
    if matched_payment_id:
        held_payment = session.exec(
            select(IncomingPayment).where(IncomingPayment.payment_id == matched_payment_id)
        ).first()
        if held_payment and held_payment.allocation_status == AllocationStatus.UNALLOCATED:
            held_payment.allocation_status = AllocationStatus.ON_HOLD
            session.add(held_payment)

    approval = _create_or_get_pending_approval(
        session=session,
        case=case,
        payment_id=matched_payment_id,
        proposed_action=proposed_action.value,
    )
    transition_case_state(
        session=session,
        case=case,
        target_state=CaseState.AWAITING_REVIEW,
        actor="DeterministicPolicyEngine",
        details=(
            f"Blocked automatic posting (flags={flags}); created approval request {approval.approval_id}"
        ),
    )
    append_case_event(
        session=session,
        case_id=case.case_id,
        event_type="AWAITING_REVIEW",
        visibility=EventVisibility.CUSTOMER_SAFE,
        payload={
            "case_id": case.case_id,
            "status": CaseState.AWAITING_REVIEW.value,
            "message": (
                f"We have located your incoming transfer and it is undergoing a standard "
                f"review by our specialist team under case #{case.case_id}."
            ),
        },
    )
    append_case_event(
        session=session,
        case_id=case.case_id,
        event_type="INTERNAL_ESCALATION_CREATED",
        visibility=EventVisibility.INTERNAL_ONLY,
        payload={
            "case_id": case.case_id,
            "approval_id": approval.approval_id,
            "reconciliation_flags": flags,
            "account_status": workflow_state.get("account_status"),
            "candidate_accounts": workflow_state.get("candidate_account_ids", []),
        },
    )
    session.add(
        AuditEvent(
            actor="DeterministicPolicyEngine",
            action="ROUTED_TO_HUMAN_REVIEW",
            before_state="INVESTIGATING",
            after_state=CaseState.AWAITING_REVIEW.value,
            details=(
                f"[{case.case_id}] Routed to human review under {approval.approval_id}; "
                f"flags={flags}"
            ),
            timestamp=get_utc_now(),
        )
    )
    session.commit()

    return PolicyExecutionResult(
        case_id=case.case_id,
        case_state=case.state.value,
        matched_payment_id=matched_payment_id,
        provider_status=workflow_state.get("provider_status"),
        proposed_action=ProposedAction.HUMAN_REVIEW.value,
        policy_approved=False,
        requires_human_approval=True,
        execution_status="AWAITING_HUMAN_APPROVAL",
        approval_id=approval.approval_id,
        reconciliation_flags=flags,
        suspected_root_cause=root_cause,
        investigation_summary=summary,
        evidence_refs=evidence_ids,
    )
