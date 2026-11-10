from typing import Any, Dict, List, Optional, Tuple
from sqlmodel import Session, select
from app.models.account import CustomerAccount
from app.models.case import InvestigationCase
from app.models.enums import AllocationStatus
from app.models.ledger import JournalEntry, LedgerEntry
from app.models.payment import IncomingPayment, PaymentEvent
from app.services.ledger_service import UNALLOCATED_SUSPENSE_ACCOUNT
from app.services.reference_parser import parse_beneficiary_account_id


def tool_get_case(session: Session, case_id: str) -> Optional[InvestigationCase]:
    """Read-only tool: fetches the InvestigationCase by case_id."""
    return session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()


def tool_get_internal_incoming_payments(
    session: Session,
    reference: str,
    amount_minor: int,
    currency: str,
) -> Tuple[Optional[IncomingPayment], bool]:
    """Read-only tool: searches incoming_payments for a matching receipt.

    Priority 1: Exact reference match on provider_reference or substring token in raw_reference.
    Priority 2: Supplementary match by amount_minor + currency on unallocated receipts.

    Returns:
        Tuple[Optional[IncomingPayment], bool]: (matched_payment, is_exact_reference_match)
    """
    normalized_ref = reference.strip()
    all_payments = session.exec(select(IncomingPayment)).all()

    # Priority 1: Exact reference match
    for pay in all_payments:
        if pay.provider_reference == normalized_ref or (
            normalized_ref and normalized_ref in pay.raw_reference
        ):
            return pay, True

    # Priority 2: Supplementary match on unallocated receipt with same amount & currency
    for pay in all_payments:
        if (
            pay.allocation_status == AllocationStatus.UNALLOCATED
            and pay.amount_minor == amount_minor
            and pay.currency.upper() == currency.upper()
        ):
            return pay, False

    return None, False


def tool_get_event_history(session: Session, payment_id: str) -> List[Dict[str, Any]]:
    """Read-only tool: fetches ordered provider events for a payment_id."""
    events = session.exec(
        select(PaymentEvent)
        .where(PaymentEvent.payment_id == payment_id)
        .order_by(PaymentEvent.event_time)
    ).all()

    return [
        {
            "event_id": evt.event_id,
            "source": evt.source,
            "event_type": evt.event_type,
            "event_time": evt.event_time.isoformat(),
        }
        for evt in events
    ]


def tool_get_ledger_reconciliation_state(
    session: Session,
    payment_id: str,
) -> Dict[str, Any]:
    """Read-only tool: inspects ledger entries for a payment_id to determine

    whether funds are currently sitting in unallocated suspense or have already
    been credited to a customer account.
    """
    legs = session.exec(
        select(LedgerEntry).where(LedgerEntry.source_payment_id == payment_id)
    ).all()

    suspense_net = sum(
        leg.credit_minor - leg.debit_minor
        for leg in legs
        if leg.account_id == UNALLOCATED_SUSPENSE_ACCOUNT
    )

    alloc_journal = session.exec(
        select(JournalEntry).where(JournalEntry.idempotency_key == f"ALLOC-{payment_id}")
    ).first()

    credited_customer_accounts = [
        leg.account_id
        for leg in legs
        if leg.account_id not in (UNALLOCATED_SUSPENSE_ACCOUNT, "ACC-SETTLEMENT-CASH")
        and leg.credit_minor > 0
    ]

    return {
        "suspense_available_minor": suspense_net,
        "funds_in_suspense": suspense_net > 0,
        "prior_allocation_journal_id": alloc_journal.journal_id if alloc_journal else None,
        "credited_customer_accounts": credited_customer_accounts,
        "prior_credit_detected": alloc_journal is not None or len(credited_customer_accounts) > 0,
    }


def tool_resolve_candidate_accounts(
    session: Session,
    raw_reference: str,
    claimed_beneficiary_account_id: str,
) -> Tuple[List[str], Optional[CustomerAccount]]:
    """Read-only tool: determines whether the wire's raw_reference uniquely identifies

    the target customer account or is ambiguous across multiple accounts.
    """
    target_account = session.exec(
        select(CustomerAccount).where(
            CustomerAccount.account_id == claimed_beneficiary_account_id
        )
    ).first()

    parsed_code = parse_beneficiary_account_id(raw_reference)
    if parsed_code is not None:
        # Wire explicitly contains a single unambiguous account code
        return [parsed_code], target_account

    # No explicit account code in wire reference (e.g. "MON-33112 BEN CHIDERA"):
    # Return active customer accounts as ambiguous candidates
    active_customer_accounts = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id != "INSTITUTION")
    ).all()

    # In Scenario S3 (ACC-LEN-003 and ACC-LEN-004 both belong to Ben Chidera),
    # if no account code is on the wire, multiple accounts could be the intended target.
    candidate_ids = [acc.account_id for acc in active_customer_accounts]
    return candidate_ids, target_account

