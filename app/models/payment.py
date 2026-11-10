from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel
from app.models.enums import AllocationStatus, ProviderPaymentStatus


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IncomingPayment(SQLModel, table=True):
    """Raw incoming payment reported by the provider/rail."""

    __tablename__ = "incoming_payments"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    payment_id: str = Field(index=True, unique=True, description="Internal unique payment ID, e.g. PAY-901")
    provider_reference: str = Field(index=True, description="External rail reference, e.g. FP-2026-X88")
    sender_bank: str = Field(description="e.g. Monzo, Barclays")
    beneficiary_account_id: Optional[str] = Field(default=None, index=True, description="Parsed beneficiary account ID")
    raw_reference: str = Field(description="Full raw payment reference string passed with the wire")
    amount_minor: int = Field(ge=0, description="Amount in minor units (pence/cents)")
    currency: str = Field(default="GBP", max_length=3)
    provider_status: ProviderPaymentStatus = Field(default=ProviderPaymentStatus.SETTLED)
    allocation_status: AllocationStatus = Field(default=AllocationStatus.UNALLOCATED, index=True)
    received_at: datetime = Field(default_factory=get_utc_now)


class PaymentEvent(SQLModel, table=True):
    """Event log capturing raw provider webhooks and state transitions."""

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("source", "event_id", name="uq_source_event_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_id: str = Field(index=True, description="Unique identifier within the provider source")
    source: str = Field(description="e.g. FASTER_PAYMENTS, CLEARBANK, MODULR")
    payment_id: str = Field(index=True)
    event_type: str = Field(description="e.g. PAYMENT_RECEIVED, FUNDS_SETTLED, STATUS_UPDATED")
    event_time: datetime
    ingested_at: datetime = Field(default_factory=get_utc_now)
    payload_hash: str = Field(description="Exact raw payload SHA-256 hash")
    canonical_fingerprint: str = Field(
        index=True,
        description="SHA-256 fingerprint of canonical immutable business fields (excluding volatile metadata)",
    )
