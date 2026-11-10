import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from app.models.account import CustomerAccount
from app.models.case import AuditEvent, InvestigationCase
from app.models.enums import AllocationStatus, CaseState, ProposedAction, ProviderPaymentStatus
from app.models.payment import IncomingPayment


class CaseValidationError(Exception):
    """Raised when case intake validation fails (e.g. unknown account)."""


class InvalidCaseStateTransitionError(Exception):
    """Raised when an invalid state machine transition is attempted."""


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Strict Case State Machine allowed transitions (Spec 03 Section 2)
ALLOWED_CASE_TRANSITIONS: Dict[CaseState, Set[CaseState]] = {
    CaseState.OPEN: {CaseState.INVESTIGATING, CaseState.CLOSED},
    CaseState.INVESTIGATING: {
        CaseState.RESOLVED,
        CaseState.AWAITING_REVIEW,
        CaseState.WAITING_PROVIDER,
        CaseState.CLOSED,
    },
    CaseState.WAITING_PROVIDER: {CaseState.INVESTIGATING, CaseState.CLOSED},
    CaseState.AWAITING_REVIEW: {CaseState.RESOLVED, CaseState.CLOSED, CaseState.INVESTIGATING},
    CaseState.RESOLVED: set(),
    CaseState.CLOSED: set(),
}

ACTIVE_CASE_STATES: Set[CaseState] = {
    CaseState.OPEN,
    CaseState.INVESTIGATING,
    CaseState.AWAITING_REVIEW,
    CaseState.WAITING_PROVIDER,
}


class CreateCaseRequest(BaseModel):
    """Payload for POST /api/v1/cases (Customer Complaint)."""

    customer_id: str = Field(min_length=1, description="Customer ID e.g. CUST-101")
    beneficiary_account_id: str = Field(min_length=1, description="Destination account e.g. ACC-LEN-001")
    sender_bank: str = Field(min_length=1, description="Origin bank e.g. Monzo")
    payment_reference: str = Field(min_length=1, description="Reference string e.g. MON-78291")
    amount_minor: int = Field(gt=0, description="Expected amount in minor units (pence)")
    currency: str = Field(default="GBP", min_length=3, max_length=3)
    expected_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD")


class CaseSummaryResponse(BaseModel):
    """Response returned on case creation."""

    case_id: str
    customer_id: str
    beneficiary_account_id: str
    sender_bank: Optional[str] = None
    reference: str
    amount_minor: int
    currency: str
    state: str
    created_at: str


class CaseDetailResponse(BaseModel):
    """Full case snapshot returned by GET /api/v1/cases/{id}."""

    case_id: str
    customer_id: str
    beneficiary_account_id: str
    sender_bank: Optional[str] = None
    reference: str
    amount_minor: int
    currency: str
    state: str
    findings: Optional[Dict[str, Any]] = None
    evidence_refs: List[str] = []
    proposed_action: Optional[str] = None
    created_at: str
    updated_at: str


class ProactiveReconciliationResponse(BaseModel):
    """Summary returned by POST /api/v1/reconciliation/run."""

    scanned_unallocated_count: int
    cases_created: int
    created_case_ids: List[str]


def transition_case_state(
    session: Session,
    case: InvestigationCase,
    target_state: CaseState,
    actor: str,
    details: str,
) -> InvestigationCase:
    """Validates and executes a state transition on an InvestigationCase."""
    current_state = case.state
    if current_state == target_state:
        return case

    allowed = ALLOWED_CASE_TRANSITIONS.get(current_state, set())
    if target_state not in allowed:
        raise InvalidCaseStateTransitionError(
            f"Cannot transition case {case.case_id} from {current_state.value} to {target_state.value}."
        )

    case.state = target_state
    case.updated_at = get_utc_now()
    session.add(case)

    session.add(
        AuditEvent(
            actor=actor,
            action="CASE_STATE_TRANSITION",
            before_state=current_state.value,
            after_state=target_state.value,
            details=f"[{case.case_id}] {details}",
            timestamp=get_utc_now(),
        )
    )
    session.flush()
    return case


def create_complaint_case(
    session: Session,
    payload: CreateCaseRequest,
) -> Tuple[InvestigationCase, bool]:
    """Creates a missing payment case or returns an existing active case.

    Returns:
        Tuple[InvestigationCase, bool]: (case_record, created_new)
    """
    # 1. Verify beneficiary_account_id exists in customers_accounts
    account = session.exec(
        select(CustomerAccount).where(
            CustomerAccount.account_id == payload.beneficiary_account_id
        )
    ).first()
    if account is None or account.customer_id == "INSTITUTION":
        raise CaseValidationError(
            f"Beneficiary customer account '{payload.beneficiary_account_id}' does not exist."
        )

    normalized_ref = payload.payment_reference.strip()

    # 2. Check for existing active case for the same account + reference
    existing_cases = session.exec(
        select(InvestigationCase).where(
            InvestigationCase.beneficiary_account_id == payload.beneficiary_account_id,
            InvestigationCase.reference == normalized_ref,
        )
    ).all()

    for existing in existing_cases:
        if existing.state in ACTIVE_CASE_STATES:
            return existing, False

    short_id = uuid4().hex[:6].upper()
    new_case = InvestigationCase(
        case_id=f"CASE-2026-{short_id}",
        customer_id=payload.customer_id,
        beneficiary_account_id=payload.beneficiary_account_id,
        sender_bank=payload.sender_bank.strip(),
        reference=normalized_ref,
        amount_minor=payload.amount_minor,
        currency=payload.currency.upper(),
        expected_date=payload.expected_date,
        state=CaseState.OPEN,
        created_at=get_utc_now(),
        updated_at=get_utc_now(),
    )
    session.add(new_case)

    session.add(
        AuditEvent(
            actor=payload.customer_id,
            action="CASE_OPENED",
            before_state="NONE",
            after_state=CaseState.OPEN.value,
            details=(
                f"Complaint case {new_case.case_id} opened for account "
                f"{new_case.beneficiary_account_id} (ref={normalized_ref}, "
                f"amount_minor={new_case.amount_minor})"
            ),
            timestamp=get_utc_now(),
        )
    )
    session.commit()
    session.refresh(new_case)
    return new_case, True


def format_case_detail(case: InvestigationCase) -> CaseDetailResponse:
    """Formats an InvestigationCase into a structured CaseDetailResponse."""
    parsed_findings: Optional[Dict[str, Any]] = None
    if case.findings_json:
        try:
            parsed_findings = json.loads(case.findings_json)
        except json.JSONDecodeError:
            parsed_findings = {"raw": case.findings_json}

    parsed_refs: List[str] = []
    if case.evidence_refs:
        try:
            parsed_refs = json.loads(case.evidence_refs)
        except json.JSONDecodeError:
            parsed_refs = [case.evidence_refs]

    return CaseDetailResponse(
        case_id=case.case_id,
        customer_id=case.customer_id,
        beneficiary_account_id=case.beneficiary_account_id,
        sender_bank=case.sender_bank,
        reference=case.reference,
        amount_minor=case.amount_minor,
        currency=case.currency,
        state=case.state.value,
        findings=parsed_findings,
        evidence_refs=parsed_refs,
        proposed_action=case.proposed_action.value if case.proposed_action else None,
        created_at=case.created_at.isoformat(),
        updated_at=case.updated_at.isoformat(),
    )


def run_proactive_reconciliation_scan(
    session: Session,
    min_age_minutes: int = 15,
) -> ProactiveReconciliationResponse:
    """Scans settled unallocated payments older than min_age_minutes and

    opens proactive investigation cases when no active case exists.
    """
    cutoff_time = get_utc_now() - timedelta(minutes=min_age_minutes)

    unallocated_payments = session.exec(
        select(IncomingPayment).where(
            IncomingPayment.allocation_status == AllocationStatus.UNALLOCATED,
            IncomingPayment.provider_status == ProviderPaymentStatus.SETTLED,
        )
    ).all()

    eligible_payments = []
    for pay in unallocated_payments:
        received_utc = (
            pay.received_at.replace(tzinfo=timezone.utc)
            if pay.received_at.tzinfo is None
            else pay.received_at
        )
        if received_utc <= cutoff_time:
            eligible_payments.append(pay)

    created_case_ids: List[str] = []

    for pay in eligible_payments:
        # Check if any active case already references this payment's provider_reference or raw_reference
        existing_all = session.exec(select(InvestigationCase)).all()
        already_covered = False
        for c in existing_all:
            if c.state in ACTIVE_CASE_STATES:
                if (
                    c.reference == pay.provider_reference
                    or c.reference in pay.raw_reference
                    or pay.raw_reference in c.reference
                ):
                    already_covered = True
                    break

        if already_covered:
            continue

        short_id = uuid4().hex[:6].upper()
        proactive_case = InvestigationCase(
            case_id=f"CASE-PRO-{short_id}",
            customer_id="SYSTEM_PROACTIVE",
            beneficiary_account_id=pay.beneficiary_account_id or "UNKNOWN_BENEFICIARY",
            sender_bank=pay.sender_bank,
            reference=pay.provider_reference,
            amount_minor=pay.amount_minor,
            currency=pay.currency,
            state=CaseState.OPEN,
            evidence_refs=json.dumps([pay.payment_id]),
            created_at=get_utc_now(),
            updated_at=get_utc_now(),
        )
        session.add(proactive_case)
        session.add(
            AuditEvent(
                actor="ProactiveReconciliationJob",
                action="PROACTIVE_CASE_CREATED",
                before_state="UNALLOCATED",
                after_state=CaseState.OPEN.value,
                details=(
                    f"Created proactive case {proactive_case.case_id} for unallocated "
                    f"payment {pay.payment_id} ({pay.amount_minor} {pay.currency})"
                ),
                timestamp=get_utc_now(),
            )
        )
        created_case_ids.append(proactive_case.case_id)

    session.commit()

    return ProactiveReconciliationResponse(
        scanned_unallocated_count=len(eligible_payments),
        cases_created=len(created_case_ids),
        created_case_ids=created_case_ids,
    )

