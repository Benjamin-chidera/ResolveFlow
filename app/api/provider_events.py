from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlmodel import Session, select
from app.core.database import get_session
from app.models.case import InvestigationCase
from app.models.ledger import JournalEntry, LedgerEntry
from app.models.payment import IncomingPayment, PaymentEvent
from app.services.ingestion_service import (
    IngestionResult,
    ProviderEventPayload,
    ingest_provider_event,
)

router = APIRouter(prefix="/api/v1", tags=["Provider Events & Payments"])


class PaymentTimelineItem(BaseModel):
    time: str
    stage: str
    description: str


class PaymentTraceResponse(BaseModel):
    payment_id: str
    provider_reference: str
    sender_bank: str
    beneficiary_account_id: Optional[str]
    raw_reference: str
    amount_minor: int
    currency: str
    current_status: str
    allocation_status: str
    timeline: List[PaymentTimelineItem]
    ledger_legs: List[Dict[str, Any]]


@router.post(
    "/provider-events",
    response_model=IngestionResult,
    status_code=status.HTTP_201_CREATED,
)
def receive_provider_event(
    payload: ProviderEventPayload,
    response: Response,
    session: Session = Depends(get_session),
) -> IngestionResult:
    """Ingests a validated payment provider webhook event with 3-tier idempotency."""
    result = ingest_provider_event(session=session, payload=payload)
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED
    return result


@router.get("/payments/{payment_id}/trace", response_model=PaymentTraceResponse)
def get_payment_trace(
    payment_id: str,
    session: Session = Depends(get_session),
) -> PaymentTraceResponse:
    """Returns the complete forensic lifecycle and ledger postings for a payment."""
    payment = session.exec(
        select(IncomingPayment).where(IncomingPayment.payment_id == payment_id)
    ).first()

    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found.",
        )

    events = session.exec(
        select(PaymentEvent)
        .where(PaymentEvent.payment_id == payment_id)
        .order_by(PaymentEvent.event_time)
    ).all()

    legs = session.exec(
        select(LedgerEntry)
        .where(LedgerEntry.source_payment_id == payment_id)
        .order_by(LedgerEntry.posted_at)
    ).all()

    timeline: List[PaymentTimelineItem] = []

    for evt in events:
        timeline.append(
            PaymentTimelineItem(
                time=evt.event_time.isoformat(),
                stage="PROVIDER_EVENT",
                description=f"{evt.source} reported {evt.event_type} (event_id={evt.event_id})",
            )
        )

    # Map journal entries linked to this payment
    seen_journal_ids = set()
    for leg in legs:
        if leg.journal_id not in seen_journal_ids:
            seen_journal_ids.add(leg.journal_id)
            jrn = session.exec(
                select(JournalEntry).where(JournalEntry.id == leg.journal_id)
            ).first()
            if jrn:
                stage_name = (
                    "ALLOCATED"
                    if jrn.idempotency_key.startswith("ALLOC-")
                    else "SUSPENSE_BOOKED"
                )
                timeline.append(
                    PaymentTimelineItem(
                        time=jrn.posted_at.isoformat(),
                        stage=stage_name,
                        description=f"{jrn.description} (journal_id={jrn.journal_id})",
                    )
                )

    # Check if any investigation cases match this payment's reference or payment_id
    all_cases = session.exec(select(InvestigationCase)).all()
    for c in all_cases:
        if (
            c.reference == payment.provider_reference
            or (c.reference and c.reference in payment.raw_reference)
            or (c.evidence_refs and payment.payment_id in c.evidence_refs)
        ):
            timeline.append(
                PaymentTimelineItem(
                    time=c.created_at.isoformat(),
                    stage="COMPLAINT_OPENED",
                    description=f"Case {c.case_id} opened by {c.customer_id} (state={c.state.value})",
                )
            )

    timeline.sort(key=lambda item: item.time)

    ledger_legs_out = [
        {
            "account_id": leg.account_id,
            "debit_minor": leg.debit_minor,
            "credit_minor": leg.credit_minor,
            "posted_at": leg.posted_at.isoformat(),
        }
        for leg in legs
    ]

    return PaymentTraceResponse(
        payment_id=payment.payment_id,
        provider_reference=payment.provider_reference,
        sender_bank=payment.sender_bank,
        beneficiary_account_id=payment.beneficiary_account_id,
        raw_reference=payment.raw_reference,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        current_status=payment.provider_status.value,
        allocation_status=payment.allocation_status.value,
        timeline=timeline,
        ledger_legs=ledger_legs_out,
    )

