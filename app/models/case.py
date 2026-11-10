from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import Field, SQLModel
from app.models.enums import ApprovalDecision, CaseState, ProposedAction


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InvestigationCase(SQLModel, table=True):
    """Missing payment complaint or proactive investigation case."""

    __tablename__ = "investigation_cases"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    case_id: str = Field(index=True, unique=True, description="e.g. CASE-2026-001")
    customer_id: str = Field(index=True)
    beneficiary_account_id: str = Field(index=True)
    sender_bank: Optional[str] = Field(default=None, description="Origin bank reported by customer")
    reference: str = Field(index=True, description="Reference provided by customer/sender")
    amount_minor: int = Field(ge=0)
    currency: str = Field(default="GBP", max_length=3)
    expected_date: Optional[str] = Field(default=None, description="Expected settlement date YYYY-MM-DD")
    state: CaseState = Field(default=CaseState.OPEN, index=True)
    findings_json: Optional[str] = Field(default=None, description="Serialized investigation findings")
    evidence_refs: Optional[str] = Field(default=None, description="JSON array of evidence IDs")
    proposed_action: Optional[ProposedAction] = Field(default=None)
    latest_evidence_version: int = Field(default=1, ge=1)
    processed_evidence_version: int = Field(default=0, ge=0)
    active_worker_lease_id: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class ApprovalRequest(SQLModel, table=True):
    """Human review approval request bound to exact payment, action, and evidence version."""

    __tablename__ = "approval_requests"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    approval_id: str = Field(index=True, unique=True, description="e.g. APP-001")
    case_id: str = Field(index=True)
    payment_id: Optional[str] = Field(default=None, index=True)
    proposed_action: str = Field(default="HUMAN_REVIEW")
    evidence_version: int = Field(default=1, ge=1)
    selected_account_id: Optional[str] = Field(default=None)
    reviewer_id: Optional[str] = None
    reviewer_role: Optional[str] = None
    decision: ApprovalDecision = Field(default=ApprovalDecision.PENDING, index=True)
    reason: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=get_utc_now)


class AuditEvent(SQLModel, table=True):
    """Immutable audit trail for all system and agent decisions."""

    __tablename__ = "audit_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    actor: str = Field(description="e.g. PaymentTraceAgent, DeterministicPolicyEngine, ops_sarah")
    action: str = Field(description="e.g. INTAKE_CASE, RECONCILE, AUTO_ALLOCATE, APPROVAL_GRANTED")
    policy_version: str = Field(default="v1.0.0")
    before_state: Optional[str] = None
    after_state: Optional[str] = None
    details: Optional[str] = None
    timestamp: datetime = Field(default_factory=get_utc_now)
