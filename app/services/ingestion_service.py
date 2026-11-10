import hashlib
import json
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from app.models.case import AuditEvent
from app.models.enums import AllocationStatus, ProviderPaymentStatus
from app.models.payment import IncomingPayment, PaymentEvent
from app.services.ledger_service import post_initial_suspense_receipt
from app.services.reference_parser import parse_beneficiary_account_id


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderEventPayload(BaseModel):
    """Incoming payment event payload from an authorized clearing provider."""

    event_id: str = Field(min_length=1, description="Provider event identifier")
    source: str = Field(default="FASTER_PAYMENTS", min_length=1)
    event_type: str = Field(default="FUNDS_SETTLED", min_length=1)
    provider_status: Optional[ProviderPaymentStatus] = Field(
        default=None,
        description="Authoritative rail status; required for generic status-update events",
    )
    provider_reference: str = Field(min_length=1, description="Rail reference e.g. FP-2026-X881")
    sender_bank: str = Field(min_length=1, description="Origin bank e.g. Monzo")
    raw_reference: str = Field(default="", description="Unparsed reference text on wire")
    amount_minor: int = Field(gt=0, description="Positive amount in minor currency units (pence)")
    currency: str = Field(default="GBP", min_length=3, max_length=3)
    event_time: datetime = Field(default_factory=get_utc_now)


class IngestionResult(BaseModel):
    """Structured response returned by the payment ingestion service."""

    status: str
    payment_id: str
    allocation_status: str
    canonical_fingerprint: str
    journal_id: Optional[str] = None
    deduplication_tier: Optional[str] = None
    duplicate: bool


def compute_canonical_fingerprint(
    source: str,
    provider_reference: str,
    sender_bank: str,
    amount_minor: int,
    currency: str,
    raw_reference: str,
) -> str:
    """Computes the Tier 2 canonical business fingerprint over immutable fields.

    Deliberately excludes volatile delivery metadata such as event_id, event_time,
    retry counters, or HTTP headers.
    """
    canonical_parts = [
        source.strip().upper(),
        provider_reference.strip(),
        sender_bank.strip(),
        str(amount_minor),
        currency.strip().upper(),
        raw_reference.strip(),
    ]
    canonical_string = "|".join(canonical_parts)
    return hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()


def compute_raw_payload_hash(payload: ProviderEventPayload) -> str:
    """Computes SHA-256 over the full serialized event payload."""
    serialized = json.dumps(payload.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def resolve_provider_status(payload: ProviderEventPayload) -> ProviderPaymentStatus:
    """Maps a rail event to its authoritative payment status.

    Only settled receipts are eligible for suspense booking. Generic status-update
    events must include an explicit status so an unknown rail event can never be
    treated as settled by default.
    """
    if payload.provider_status is not None:
        return payload.provider_status

    event_type = payload.event_type.strip().upper()
    event_statuses = {
        "FUNDS_SETTLED": ProviderPaymentStatus.SETTLED,
        "FUNDS_PENDING": ProviderPaymentStatus.PENDING,
        "PAYMENT_PENDING": ProviderPaymentStatus.PENDING,
        "FUNDS_FAILED": ProviderPaymentStatus.FAILED,
        "PAYMENT_FAILED": ProviderPaymentStatus.FAILED,
        "FUNDS_RETURNED": ProviderPaymentStatus.REVERSED,
        "FUNDS_REVERSED": ProviderPaymentStatus.REVERSED,
        "PAYMENT_REVERSED": ProviderPaymentStatus.REVERSED,
    }
    try:
        return event_statuses[event_type]
    except KeyError as exc:
        raise ValueError(
            "Unsupported provider event type. Supply provider_status for status updates."
        ) from exc


def ingest_provider_event(session: Session, payload: ProviderEventPayload) -> IngestionResult:
    """Ingests a provider payment event enforcing Tier 1 and Tier 2 idempotency

    and booking new settled receipts into the double-entry suspense account.
    """
    canonical_fp = compute_canonical_fingerprint(
        source=payload.source,
        provider_reference=payload.provider_reference,
        sender_bank=payload.sender_bank,
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        raw_reference=payload.raw_reference,
    )

    # Tier 1 Check: Primary deduplication by (source, event_id)
    tier1_match = session.exec(
        select(PaymentEvent).where(
            PaymentEvent.source == payload.source,
            PaymentEvent.event_id == payload.event_id,
        )
    ).first()

    if tier1_match is not None:
        existing_payment = session.exec(
            select(IncomingPayment).where(IncomingPayment.payment_id == tier1_match.payment_id)
        ).first()
        alloc_status = (
            existing_payment.allocation_status.value
            if existing_payment
            else AllocationStatus.UNALLOCATED.value
        )
        session.add(
            AuditEvent(
                actor="IngestionService",
                action="DUPLICATE_EVENT_TIER_1",
                before_state=alloc_status,
                after_state=alloc_status,
                details=f"Ignored duplicate (source={payload.source}, event_id={payload.event_id})",
            )
        )
        session.commit()
        return IngestionResult(
            status="DUPLICATE_IGNORED",
            payment_id=tier1_match.payment_id,
            allocation_status=alloc_status,
            canonical_fingerprint=tier1_match.canonical_fingerprint,
            deduplication_tier="TIER_1_EVENT_ID",
            duplicate=True,
        )

    # Tier 2 Check: Secondary deduplication by canonical business fingerprint
    tier2_match = session.exec(
        select(PaymentEvent).where(PaymentEvent.canonical_fingerprint == canonical_fp)
    ).first()

    if tier2_match is not None:
        existing_payment = session.exec(
            select(IncomingPayment).where(IncomingPayment.payment_id == tier2_match.payment_id)
        ).first()
        alloc_status = (
            existing_payment.allocation_status.value
            if existing_payment
            else AllocationStatus.UNALLOCATED.value
        )
        session.add(
            AuditEvent(
                actor="IngestionService",
                action="DUPLICATE_EVENT_TIER_2",
                before_state=alloc_status,
                after_state=alloc_status,
                details=(
                    f"Ignored duplicate via canonical fingerprint for provider_ref="
                    f"{payload.provider_reference} (new event_id={payload.event_id})"
                ),
            )
        )
        session.commit()
        return IngestionResult(
            status="DUPLICATE_IGNORED",
            payment_id=tier2_match.payment_id,
            allocation_status=alloc_status,
            canonical_fingerprint=canonical_fp,
            deduplication_tier="TIER_2_CANONICAL_FINGERPRINT",
            duplicate=True,
        )

    # Truly New Event: parse the beneficiary and persist the authoritative rail status.
    # Only settled funds are booked into settlement cash and suspense.
    payment_id = f"PAY-{payload.provider_reference}"
    parsed_account_id = parse_beneficiary_account_id(payload.raw_reference)
    provider_status = resolve_provider_status(payload)
    allocation_status = (
        AllocationStatus.UNALLOCATED
        if provider_status == ProviderPaymentStatus.SETTLED
        else AllocationStatus.ON_HOLD
    )

    incoming_payment = IncomingPayment(
        payment_id=payment_id,
        provider_reference=payload.provider_reference,
        sender_bank=payload.sender_bank,
        beneficiary_account_id=parsed_account_id,
        raw_reference=payload.raw_reference,
        amount_minor=payload.amount_minor,
        currency=payload.currency.upper(),
        provider_status=provider_status,
        allocation_status=allocation_status,
        received_at=get_utc_now(),
    )
    session.add(incoming_payment)

    payment_event = PaymentEvent(
        event_id=payload.event_id,
        source=payload.source,
        payment_id=payment_id,
        event_type=payload.event_type,
        event_time=payload.event_time,
        ingested_at=get_utc_now(),
        payload_hash=compute_raw_payload_hash(payload),
        canonical_fingerprint=canonical_fp,
    )
    session.add(payment_event)

    journal = None
    if provider_status == ProviderPaymentStatus.SETTLED:
        journal = post_initial_suspense_receipt(
            session=session,
            payment_id=payment_id,
            amount_minor=payload.amount_minor,
        )

    session.add(
        AuditEvent(
            actor="IngestionService",
        action=(
            "PAYMENT_INGESTED_TO_SUSPENSE"
            if journal is not None
            else "PAYMENT_STATUS_RECORDED"
        ),
            before_state="NONE",
        after_state=allocation_status.value,
            details=(
                f"Ingested {payment_id} with rail status {provider_status.value}. "
                + (
                    f"Booked into suspense under journal {journal.journal_id}."
                    if journal is not None
                    else "No settlement-cash or suspense journal was posted."
                )
            ),
        )
    )
    session.commit()

    return IngestionResult(
        status="INGESTED",
        payment_id=payment_id,
        allocation_status=allocation_status.value,
        canonical_fingerprint=canonical_fp,
        journal_id=journal.journal_id if journal is not None else None,
        deduplication_tier=None,
        duplicate=False,
    )

