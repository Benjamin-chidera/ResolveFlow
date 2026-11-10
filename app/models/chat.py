from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.models.enums import EventVisibility, MessageSenderRole, OutboxStatus


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ChatSession(SQLModel, table=True):
    """Authenticated customer conversational session (Spec 08)."""

    __tablename__ = "chat_sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: str = Field(index=True, unique=True, description="e.g. CSESS-001")
    tenant_id: str = Field(default="lenfi_uk", index=True)
    customer_id: str = Field(index=True)
    beneficiary_account_id: str = Field(index=True)
    active_case_id: Optional[str] = Field(default=None, index=True)
    draft_sender_bank: Optional[str] = Field(default=None)
    draft_amount_minor: Optional[int] = Field(default=None)
    draft_reference: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class ChatMessage(SQLModel, table=True):
    """Persisted customer or agent chat message (Spec 08)."""

    __tablename__ = "chat_messages"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    message_id: str = Field(index=True, unique=True, description="e.g. MSG-001")
    session_id: str = Field(index=True)
    sender_role: MessageSenderRole = Field(default=MessageSenderRole.CUSTOMER)
    content: str
    evidence_version: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=get_utc_now)


class CustomerEvidenceClaim(SQLModel, table=True):
    """Immutable versioned customer payment clue requiring reconciliation (Spec 08)."""

    __tablename__ = "customer_evidence_claims"
    __table_args__ = (
        UniqueConstraint("case_id", "evidence_version", name="uq_case_evidence_version"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    case_id: str = Field(index=True)
    evidence_version: int = Field(ge=1)
    claimed_reference: Optional[str] = Field(default=None)
    claimed_amount_minor: Optional[int] = Field(default=None)
    claimed_sender_bank: Optional[str] = Field(default=None)
    source_message_id: str
    created_at: datetime = Field(default_factory=get_utc_now)


class OutboxEvent(SQLModel, table=True):
    """SQLite Transactional Outbox row for durable Redis Streams publication (Spec 08)."""

    __tablename__ = "outbox_events"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    outbox_id: str = Field(index=True, unique=True, description="e.g. OUT-001")
    stream_name: str = Field(default="resolveflow:investigations")
    idempotency_key: str = Field(
        index=True,
        unique=True,
        description="e.g. JOB-{case_id}-v{evidence_version}",
    )
    payload_json: str
    status: OutboxStatus = Field(default=OutboxStatus.PENDING, index=True)
    redis_message_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=get_utc_now)
    published_at: Optional[datetime] = Field(default=None)


class CaseEvent(SQLModel, table=True):
    """Persisted SSE event log separating CUSTOMER_SAFE from INTERNAL_ONLY events (Spec 08)."""

    __tablename__ = "case_events"

    sequence_id: Optional[int] = Field(default=None, primary_key=True)
    case_id: str = Field(index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    visibility: EventVisibility = Field(default=EventVisibility.CUSTOMER_SAFE, index=True)
    event_type: str = Field(
        description="CASE_OPENED, TRACE_PROGRESS, EVIDENCE_UPDATED, AWAITING_REVIEW, CASE_RESOLVED"
    )
    payload_json: str
    created_at: datetime = Field(default_factory=get_utc_now)

