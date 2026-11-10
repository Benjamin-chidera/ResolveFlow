import json
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from app.models.account import CustomerAccount
from app.models.case import ApprovalRequest, AuditEvent, InvestigationCase
from app.models.enums import (
    AccountStatus,
    AllocationStatus,
    ApprovalDecision,
    CaseState,
    EventVisibility,
    ProviderPaymentStatus,
)
from app.models.payment import IncomingPayment
from app.services.case_event_helper import append_case_event
from app.services.case_service import transition_case_state
from app.services.ledger_service import (
    DuplicateAllocationError,
    post_customer_allocation,
)

AUTHORIZED_REVIEWER_ROLES = {"PAYMENT_OPERATIONS", "COMPLIANCE_OFFICER", "ADMIN"}


class ApprovalNotFoundError(Exception):
    """Raised when an approval_id does not exist."""


class ApprovalAuthorizationError(Exception):
    """Raised when a caller lacks an authorized reviewer role."""


class StaleApprovalError(Exception):
    """Raised when payment state, case state, or evidence changed since approval creation."""


class ApprovalValidationError(Exception):
    """Raised when decision payload or rail status is invalid."""


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalDecisionRequest(BaseModel):
    """Payload for POST /api/v1/approvals/{id}/decision (Spec 06)."""

    reviewer_id: str = Field(min_length=1, description="Operator username e.g. ops_sarah")
    reviewer_role: str = Field(min_length=1, description="Must be PAYMENT_OPERATIONS, COMPLIANCE_OFFICER, or ADMIN")
    decision: str = Field(min_length=1, description="APPROVED or REJECTED")
    selected_account_id: Optional[str] = Field(
        default=None,
        description="Disambiguated target customer account ID (e.g. ACC-LEN-003)",
    )
    reason: str = Field(min_length=3, description="Mandatory audit explanation for the decision")


class ApprovalDecisionResponse(BaseModel):
    """Response returned after processing an approval decision."""

    approval_id: str
    case_id: str
    decision: str
    reviewer_id: str
    selected_account_id: Optional[str] = None
    execution_status: str
    journal_id: Optional[str] = None
    decided_at: str


class PendingApprovalItem(BaseModel):
    """Summary of a pending approval request for operations reviewers."""

    approval_id: str
    case_id: str
    payment_id: Optional[str] = None
    proposed_action: str
    evidence_version: int
    decision: str
    created_at: str


def verify_reviewer_role(role: str) -> str:
    normalized = (role or "").strip().upper()
    if normalized not in AUTHORIZED_REVIEWER_ROLES:
        raise ApprovalAuthorizationError(
            f"Role '{role}' is not authorized to review or decide payment approvals."
        )
    return normalized


def list_pending_approvals(
    session: Session,
    reviewer_role: str,
) -> List[PendingApprovalItem]:
    """Returns all PENDING approval requests for authorized operations staff."""
    verify_reviewer_role(reviewer_role)

    approvals = session.exec(
        select(ApprovalRequest)
        .where(ApprovalRequest.decision == ApprovalDecision.PENDING)
        .order_by(ApprovalRequest.created_at)
    ).all()

    return [
        PendingApprovalItem(
            approval_id=a.approval_id,
            case_id=a.case_id,
            payment_id=a.payment_id,
            proposed_action=a.proposed_action,
            evidence_version=a.evidence_version,
            decision=a.decision.value,
            created_at=a.created_at.isoformat(),
        )
        for a in approvals
    ]


def process_approval_decision(
    session: Session,
    approval_id: str,
    payload: ApprovalDecisionRequest,
) -> ApprovalDecisionResponse:
    """Processes an operator's APPROVED or REJECTED decision with Stale Approval Protection."""
    normalized_role = verify_reviewer_role(payload.reviewer_role)
    normalized_decision = payload.decision.strip().upper()
    if normalized_decision not in (ApprovalDecision.APPROVED.value, ApprovalDecision.REJECTED.value):
        raise ApprovalValidationError("Decision must be either 'APPROVED' or 'REJECTED'.")

    approval = session.exec(
        select(ApprovalRequest).where(ApprovalRequest.approval_id == approval_id)
    ).first()
    if approval is None:
        raise ApprovalNotFoundError(f"Approval request '{approval_id}' not found.")

    # Stale Check 1: Prevent repeated or duplicate decision submissions
    if approval.decision != ApprovalDecision.PENDING:
        raise StaleApprovalError(
            f"Approval '{approval_id}' has already been decided ({approval.decision.value})."
        )

    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == approval.case_id)
    ).first()
    if case is None:
        raise ApprovalNotFoundError(f"Linked case '{approval.case_id}' not found.")

    # Stale Check 2: Case must still be in AWAITING_REVIEW
    if case.state != CaseState.AWAITING_REVIEW:
        raise StaleApprovalError(
            f"Case '{case.case_id}' is in state '{case.state.value}', not AWAITING_REVIEW."
        )

    # Stale Check 3: Evidence version must match the version bound to the approval
    if case.latest_evidence_version != approval.evidence_version:
        raise StaleApprovalError(
            f"Stale approval: case '{case.case_id}' evidence version advanced from "
            f"v{approval.evidence_version} to v{case.latest_evidence_version}."
        )

    now = get_utc_now()

    # Path A: Operator REJECTS the claim
    if normalized_decision == ApprovalDecision.REJECTED.value:
        approval.decision = ApprovalDecision.REJECTED
        approval.reviewer_id = payload.reviewer_id
        approval.reviewer_role = normalized_role
        approval.reason = payload.reason
        approval.decided_at = now
        session.add(approval)

        transition_case_state(
            session=session,
            case=case,
            target_state=CaseState.CLOSED,
            actor=payload.reviewer_id,
            details=f"Approval {approval_id} REJECTED: {payload.reason}",
        )
        append_case_event(
            session=session,
            case_id=case.case_id,
            event_type="CASE_CLOSED",
            visibility=EventVisibility.CUSTOMER_SAFE,
            payload={
                "case_id": case.case_id,
                "status": CaseState.CLOSED.value,
                "message": (
                    f"Our operations team completed the review of case #{case.case_id} "
                    f"and could not verify this transfer for credit to your account."
                ),
            },
        )
        session.add(
            AuditEvent(
                actor=payload.reviewer_id,
                action="APPROVAL_REJECTED",
                before_state=CaseState.AWAITING_REVIEW.value,
                after_state=CaseState.CLOSED.value,
                details=f"[{case.case_id}] Rejected {approval_id}. Reason: {payload.reason}",
                timestamp=now,
            )
        )
        session.commit()
        return ApprovalDecisionResponse(
            approval_id=approval.approval_id,
            case_id=case.case_id,
            decision=ApprovalDecision.REJECTED.value,
            reviewer_id=payload.reviewer_id,
            selected_account_id=None,
            execution_status="REJECTED_NOT_EXECUTED",
            journal_id=None,
            decided_at=now.isoformat(),
        )

    # Path B: Operator APPROVES the allocation — Revalidate payment & account under DB lock
    target_account_id = (payload.selected_account_id or case.beneficiary_account_id).strip()

    if not approval.payment_id:
        raise StaleApprovalError(
            f"Cannot approve '{approval_id}': no authoritative payment_id is bound to this request."
        )

    fresh_payment = session.exec(
        select(IncomingPayment).where(IncomingPayment.payment_id == approval.payment_id)
    ).first()
    if fresh_payment is None:
        raise StaleApprovalError(f"Payment '{approval.payment_id}' no longer exists.")

    # Stale Check 4: If payment was already allocated while awaiting review -> 409 Conflict
    if fresh_payment.allocation_status == AllocationStatus.ALLOCATED:
        raise StaleApprovalError(
            f"Stale approval: payment '{fresh_payment.payment_id}' was already allocated."
        )

    # Stale Check 5: If payment was returned or reversed on the rail -> 400 Bad Request
    if fresh_payment.provider_status != ProviderPaymentStatus.SETTLED:
        raise ApprovalValidationError(
            f"Payment '{fresh_payment.payment_id}' status is '{fresh_payment.provider_status.value}', not SETTLED."
        )

    # Stale Check 6: Target account must exist and be ACTIVE
    target_account = session.exec(
        select(CustomerAccount).where(CustomerAccount.account_id == target_account_id)
    ).first()
    if target_account is None or target_account.customer_id == "INSTITUTION":
        raise ApprovalValidationError(
            f"Selected beneficiary account '{target_account_id}' is not a valid customer account."
        )

    if target_account.status != AccountStatus.ACTIVE:
        raise StaleApprovalError(
            f"Cannot execute allocation: account '{target_account_id}' status is '{target_account.status.value}'."
        )

    try:
        journal = post_customer_allocation(
            session=session,
            payment_id=fresh_payment.payment_id,
            beneficiary_account_id=target_account.account_id,
            amount_minor=fresh_payment.amount_minor,
            case_reference=case.case_id,
        )
    except DuplicateAllocationError as exc:
        raise StaleApprovalError(str(exc)) from exc

    fresh_payment.allocation_status = AllocationStatus.ALLOCATED
    fresh_payment.beneficiary_account_id = target_account.account_id
    session.add(fresh_payment)

    case.beneficiary_account_id = target_account.account_id
    if case.findings_json:
        try:
            findings = json.loads(case.findings_json)
            findings["suspense_cleared"] = True
            findings["approved_by"] = payload.reviewer_id
            findings["disambiguated_account_id"] = target_account.account_id
            case.findings_json = json.dumps(findings)
        except json.JSONDecodeError:
            pass
    session.add(case)

    approval.decision = ApprovalDecision.APPROVED
    approval.reviewer_id = payload.reviewer_id
    approval.reviewer_role = normalized_role
    approval.selected_account_id = target_account.account_id
    approval.reason = payload.reason
    approval.decided_at = now
    session.add(approval)

    transition_case_state(
        session=session,
        case=case,
        target_state=CaseState.RESOLVED,
        actor=payload.reviewer_id,
        details=(
            f"Approval {approval_id} GRANTED by {payload.reviewer_id}; allocated "
            f"{fresh_payment.payment_id} to {target_account.account_id} under {journal.journal_id}"
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
            "beneficiary_account_id": target_account.account_id,
            "message": (
                f"We verified your transfer ({case.reference}) and credited "
                f"£{formatted_amount} to your account ({target_account.account_id})."
            ),
        },
    )

    session.add(
        AuditEvent(
            actor=payload.reviewer_id,
            action="APPROVAL_GRANTED_AND_EXECUTED",
            before_state=CaseState.AWAITING_REVIEW.value,
            after_state=CaseState.RESOLVED.value,
            details=(
                f"[{case.case_id}] Approved {approval_id} ({normalized_role}). "
                f"Posted {journal.journal_id} to {target_account.account_id}. Reason: {payload.reason}"
            ),
            timestamp=now,
        )
    )
    session.commit()

    return ApprovalDecisionResponse(
        approval_id=approval.approval_id,
        case_id=case.case_id,
        decision=ApprovalDecision.APPROVED.value,
        reviewer_id=payload.reviewer_id,
        selected_account_id=target_account.account_id,
        execution_status="EXECUTED",
        journal_id=journal.journal_id,
        decided_at=now.isoformat(),
    )

