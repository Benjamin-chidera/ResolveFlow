from datetime import datetime, timezone
from typing import List
from sqlmodel import Session, select
from app.models.account import CustomerAccount
from app.models.enums import AccountType
from app.models.ledger import JournalEntry, LedgerEntry

SETTLEMENT_CASH_ACCOUNT = "ACC-SETTLEMENT-CASH"
UNALLOCATED_SUSPENSE_ACCOUNT = "ACC-UNALLOCATED-SUSPENSE"


class LedgerError(Exception):
    """Base exception for double-entry ledger invariant violations."""


class UnbalancedJournalError(LedgerError):
    """Raised when total debits do not equal total credits."""


class DuplicateAllocationError(LedgerError):
    """Raised when attempting to allocate an incoming payment more than once."""


class InsufficientSuspenseFundsError(LedgerError):
    """Raised when unallocated suspense does not hold backing funds for the payment."""


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def verify_balanced_legs(legs: List[LedgerEntry]) -> None:
    """Enforces the fundamental double-entry invariant: sum(debits) - sum(credits) == 0."""
    if len(legs) < 2:
        raise UnbalancedJournalError("A double-entry journal must contain at least two legs.")

    total_debits = sum(leg.debit_minor for leg in legs)
    total_credits = sum(leg.credit_minor for leg in legs)

    if total_debits <= 0 or total_credits <= 0:
        raise UnbalancedJournalError("Journal amounts must be strictly positive minor units.")

    if total_debits != total_credits:
        raise UnbalancedJournalError(
            f"Journal out of balance: debits={total_debits}, credits={total_credits}"
        )


def post_initial_suspense_receipt(
    session: Session,
    payment_id: str,
    amount_minor: int,
) -> JournalEntry:
    """Books an incoming provider receipt into the unallocated suspense pool.

    Accounting Entry:
    - Debit:  ACC-SETTLEMENT-CASH (+Asset)
    - Credit: ACC-UNALLOCATED-SUSPENSE (+Liability)
    """
    if amount_minor <= 0:
        raise UnbalancedJournalError("Receipt amount_minor must be greater than zero.")

    journal = JournalEntry(
        journal_id=f"JRN-RECEIPT-{payment_id}",
        idempotency_key=f"RECEIPT-{payment_id}",
        reference=payment_id,
        description=f"Initial incoming receipt parked in suspense for {payment_id}",
        posted_at=get_utc_now(),
    )
    session.add(journal)
    session.flush()

    legs = [
        LedgerEntry(
            journal_id=journal.id,
            account_id=SETTLEMENT_CASH_ACCOUNT,
            debit_minor=amount_minor,
            credit_minor=0,
            source_payment_id=payment_id,
            posted_at=get_utc_now(),
        ),
        LedgerEntry(
            journal_id=journal.id,
            account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
            debit_minor=0,
            credit_minor=amount_minor,
            source_payment_id=payment_id,
            posted_at=get_utc_now(),
        ),
    ]
    verify_balanced_legs(legs)
    session.add_all(legs)
    session.flush()
    return journal


def post_customer_allocation(
    session: Session,
    payment_id: str,
    beneficiary_account_id: str,
    amount_minor: int,
    case_reference: str,
) -> JournalEntry:
    """Reclassifies funds from Unallocated Suspense into the verified Customer Account.

    Enforces Tier 3 Financial Protection:
    - Deterministic unique allocation key: ALLOC-{payment_id}
    - Verifies backing funds exist in ACC-UNALLOCATED-SUSPENSE for payment_id
    - Accounting Entry:
      - Debit:  ACC-UNALLOCATED-SUSPENSE (-Liability)
      - Credit: {beneficiary_account_id} (+Liability)
    """
    if amount_minor <= 0:
        raise UnbalancedJournalError("Allocation amount_minor must be greater than zero.")

    allocation_key = f"ALLOC-{payment_id}"

    # Check 1: Application-level check for existing allocation journal
    existing_journal = session.exec(
        select(JournalEntry).where(JournalEntry.idempotency_key == allocation_key)
    ).first()
    if existing_journal is not None:
        raise DuplicateAllocationError(
            f"Payment {payment_id} has already been allocated under journal {existing_journal.journal_id}."
        )

    # Check 2: Verify backing funds in unallocated suspense for this specific payment_id
    suspense_legs = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.source_payment_id == payment_id,
            LedgerEntry.account_id == UNALLOCATED_SUSPENSE_ACCOUNT,
        )
    ).all()
    available_suspense_minor = sum(leg.credit_minor - leg.debit_minor for leg in suspense_legs)
    if available_suspense_minor < amount_minor:
        raise InsufficientSuspenseFundsError(
            f"Insufficient backing suspense funds for {payment_id}: "
            f"available={available_suspense_minor}, required={amount_minor}"
        )

    journal = JournalEntry(
        journal_id=f"JRN-ALLOC-{payment_id}",
        idempotency_key=allocation_key,
        reference=case_reference,
        description=f"Allocated {payment_id} from suspense to {beneficiary_account_id}",
        posted_at=get_utc_now(),
    )
    session.add(journal)
    session.flush()

    legs = [
        LedgerEntry(
            journal_id=journal.id,
            account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
            debit_minor=amount_minor,
            credit_minor=0,
            source_payment_id=payment_id,
            posted_at=get_utc_now(),
        ),
        LedgerEntry(
            journal_id=journal.id,
            account_id=beneficiary_account_id,
            debit_minor=0,
            credit_minor=amount_minor,
            source_payment_id=payment_id,
            posted_at=get_utc_now(),
        ),
    ]
    verify_balanced_legs(legs)
    session.add_all(legs)
    session.flush()
    return journal


def get_account_balance_minor(session: Session, account_id: str) -> int:
    """Computes the current balance of an account in minor units.

    - ASSET accounts: sum(debits) - sum(credits)
    - LIABILITY accounts (Suspense & Customer Deposits): sum(credits) - sum(debits)
    """
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.account_id == account_id)
    ).first()

    legs = session.exec(
        select(LedgerEntry).where(LedgerEntry.account_id == account_id)
    ).all()

    total_debits = sum(leg.debit_minor for leg in legs)
    total_credits = sum(leg.credit_minor for leg in legs)

    if account and account.account_type == AccountType.ASSET:
        return total_debits - total_credits

    return total_credits - total_debits

