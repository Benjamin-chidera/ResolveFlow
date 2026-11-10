import hashlib
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Session, SQLModel, delete
from app.core.database import engine, init_db
from app.models import (
    AccountStatus,
    AccountType,
    AllocationStatus,
    ApprovalRequest,
    AuditEvent,
    CaseEvent,
    CaseState,
    ChatMessage,
    ChatSession,
    CustomerAccount,
    CustomerEvidenceClaim,
    IncomingPayment,
    InvestigationCase,
    JournalEntry,
    LedgerEntry,
    OutboxEvent,
    PaymentEvent,
    ProviderPaymentStatus,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hash_payload(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def compute_canonical_fingerprint(
    source: str,
    provider_ref: str,
    sender_bank: str,
    amount_minor: int,
    currency: str,
    raw_reference: str,
) -> str:
    payload = f"{source}|{provider_ref}|{sender_bank}|{amount_minor}|{currency}|{raw_reference}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def populate_session_with_demo_data(session: Session) -> None:
    """Populates a given SQLite session with institutional accounts, Benjamin Chidera's
    primary account, and all 7 deterministic incoming-payment scenarios.
    """
    session.exec(delete(CaseEvent))
    session.exec(delete(OutboxEvent))
    session.exec(delete(CustomerEvidenceClaim))
    session.exec(delete(ChatMessage))
    session.exec(delete(ChatSession))
    session.exec(delete(ApprovalRequest))
    session.exec(delete(AuditEvent))
    session.exec(delete(LedgerEntry))
    session.exec(delete(JournalEntry))
    session.exec(delete(PaymentEvent))
    session.exec(delete(IncomingPayment))
    session.exec(delete(InvestigationCase))
    session.exec(delete(CustomerAccount))
    session.commit()

    accounts = [
        # Institutional Accounts
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="INSTITUTION",
            customer_name="ResolveFlow Settlement Cash",
            account_id="ACC-SETTLEMENT-CASH",
            account_number="00000001",
            sort_code="04-00-75",
            account_type=AccountType.ASSET,
            currency="GBP",
            status=AccountStatus.ACTIVE,
            is_onboarded=True,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="INSTITUTION",
            customer_name="ResolveFlow Unallocated Suspense",
            account_id="ACC-UNALLOCATED-SUSPENSE",
            account_number="00000002",
            sort_code="04-00-75",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
            is_onboarded=True,
        ),
        # Primary Demo Customer: Benjamin Chidera (CUST-101 / ACC-LEN-001)
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-101",
            customer_name="Benjamin Chidera",
            account_id="ACC-LEN-001",
            account_number="84920101",
            sort_code="04-00-75",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
            is_onboarded=False,
        ),
        # Secondary Customer Accounts (for multi-tenant isolation & S2/S3/S5 scenarios)
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-102",
            customer_name="Marcus Reid",
            account_id="ACC-LEN-002",
            account_number="84920102",
            sort_code="04-00-75",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
            is_onboarded=True,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-103",
            customer_name="Ben Chidera (Secondary)",
            account_id="ACC-LEN-003",
            account_number="84920103",
            sort_code="04-00-75",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
            is_onboarded=True,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-104",
            customer_name="Emily Watson",
            account_id="ACC-LEN-004",
            account_number="84920104",
            sort_code="04-00-75",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
            is_onboarded=True,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-105",
            customer_name="Alex Rivera (Restricted)",
            account_id="ACC-LEN-005",
            account_number="84920105",
            sort_code="04-00-75",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.FROZEN,  # Compliance hold
            is_onboarded=True,
        ),
    ]
    session.add_all(accounts)
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 1 (and S4): Successful incoming payment already allocated (£150.00)
    # -------------------------------------------------------------------------
    pay_s4 = IncomingPayment(
        payment_id="PAY-S4-004",
        provider_reference="FP-2026-X884",
        sender_bank="Monzo",
        beneficiary_account_id="ACC-LEN-001",
        raw_reference="MON-11223 ACC-LEN-001",
        amount_minor=15000,  # £150.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.ALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s4)
    session.commit()

    jrn_s4_init = JournalEntry(
        journal_id="JRN-S4-INIT",
        idempotency_key="RECEIPT-PAY-S4-004",
        reference="PAY-S4-004",
        description="Initial receipt parked in suspense for PAY-S4-004",
        posted_at=now_utc(),
    )
    session.add(jrn_s4_init)
    session.commit()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s4_init.id,
                account_id="ACC-SETTLEMENT-CASH",
                debit_minor=15000,
                credit_minor=0,
                source_payment_id="PAY-S4-004",
            ),
            LedgerEntry(
                journal_id=jrn_s4_init.id,
                account_id="ACC-UNALLOCATED-SUSPENSE",
                debit_minor=0,
                credit_minor=15000,
                source_payment_id="PAY-S4-004",
            ),
        ]
    )

    jrn_s4 = JournalEntry(
        journal_id="JRN-S4-001",
        idempotency_key="ALLOC-PAY-S4-004",
        reference="PAY-S4-004",
        description="Allocated customer credit for PAY-S4-004",
        posted_at=now_utc(),
    )
    session.add(jrn_s4)
    session.commit()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s4.id,
                account_id="ACC-UNALLOCATED-SUSPENSE",
                debit_minor=15000,
                credit_minor=0,
                source_payment_id="PAY-S4-004",
            ),
            LedgerEntry(
                journal_id=jrn_s4.id,
                account_id="ACC-LEN-001",
                debit_minor=0,
                credit_minor=15000,
                source_payment_id="PAY-S4-004",
            ),
        ]
    )
    session.add(
        PaymentEvent(
            event_id="EVT-S4-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-S4-004",
            event_type="FUNDS_SETTLED",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-S4-004-SETTLED"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="FP-2026-X884",
                sender_bank="Monzo",
                amount_minor=15000,
                currency="GBP",
                raw_reference="MON-11223 ACC-LEN-001",
            ),
        )
    )
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 2 (and S1): Payment received by ResolveFlow, settled in Suspense, not yet allocated (£500.00)
    # -------------------------------------------------------------------------
    pay_s1 = IncomingPayment(
        payment_id="PAY-S1-001",
        provider_reference="FP-2026-X881",
        sender_bank="Monzo",
        beneficiary_account_id="ACC-LEN-001",
        raw_reference="MON-78291 ACC-LEN-001",
        amount_minor=50000,  # £500.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s1)
    session.commit()

    jrn_s1 = JournalEntry(
        journal_id="JRN-S1-001",
        idempotency_key="RECEIPT-PAY-S1-001",
        reference="PAY-S1-001",
        description="Initial receipt parked in suspense for PAY-S1-001",
        posted_at=now_utc(),
    )
    session.add(jrn_s1)
    session.commit()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s1.id,
                account_id="ACC-SETTLEMENT-CASH",
                debit_minor=50000,
                credit_minor=0,
                source_payment_id="PAY-S1-001",
            ),
            LedgerEntry(
                journal_id=jrn_s1.id,
                account_id="ACC-UNALLOCATED-SUSPENSE",
                debit_minor=0,
                credit_minor=50000,
                source_payment_id="PAY-S1-001",
            ),
        ]
    )
    session.add(
        PaymentEvent(
            event_id="EVT-S1-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-S1-001",
            event_type="FUNDS_SETTLED",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-S1-001-SETTLED"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="FP-2026-X881",
                sender_bank="Monzo",
                amount_minor=50000,
                currency="GBP",
                raw_reference="MON-78291 ACC-LEN-001",
            ),
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S1-001",
            customer_id="CUST-101",
            beneficiary_account_id="ACC-LEN-001",
            sender_bank="Monzo",
            reference="MON-78291",
            amount_minor=50000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 3 (and S2): No incoming receipt found (£250.00 claimed from Monzo)
    # -------------------------------------------------------------------------
    session.add(
        InvestigationCase(
            case_id="CASE-S2-002",
            customer_id="CUST-102",
            beneficiary_account_id="ACC-LEN-002",
            sender_bank="Monzo",
            reference="MON-99881",
            amount_minor=25000,  # £250.00
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-BEN-NORECEIPT",
            customer_id="CUST-101",
            beneficiary_account_id="ACC-LEN-001",
            sender_bank="Monzo",
            reference="MON-99881",
            amount_minor=25000,  # £250.00
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 4: Pending incoming payment (£180.00 from Barclays, PENDING on rail)
    # -------------------------------------------------------------------------
    pay_pending = IncomingPayment(
        payment_id="PAY-PEND-001",
        provider_reference="BARC-2026-P449",
        sender_bank="Barclays",
        beneficiary_account_id="ACC-LEN-001",
        raw_reference="BARC-44910 ACC-LEN-001",
        amount_minor=18000,  # £180.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.PENDING,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_pending)
    session.add(
        PaymentEvent(
            event_id="EVT-PEND-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-PEND-001",
            event_type="PAYMENT_INITIATED_PENDING",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-PEND-001-PENDING"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="BARC-2026-P449",
                sender_bank="Barclays",
                amount_minor=18000,
                currency="GBP",
                raw_reference="BARC-44910 ACC-LEN-001",
            ),
        )
    )
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 5: Failed & Reversed incoming payments (£95.00 FAILED, £210.00 REVERSED)
    # -------------------------------------------------------------------------
    pay_failed = IncomingPayment(
        payment_id="PAY-FAIL-001",
        provider_reference="REV-2026-F662",
        sender_bank="Revolut",
        beneficiary_account_id="ACC-LEN-001",
        raw_reference="REV-66201 ACC-LEN-001",
        amount_minor=9500,  # £95.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.FAILED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_failed)
    session.add(
        PaymentEvent(
            event_id="EVT-FAIL-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-FAIL-001",
            event_type="PAYMENT_FAILED_AT_PROVIDER",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-FAIL-001-FAILED"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="REV-2026-F662",
                sender_bank="Revolut",
                amount_minor=9500,
                currency="GBP",
                raw_reference="REV-66201 ACC-LEN-001",
            ),
        )
    )

    pay_reversed = IncomingPayment(
        payment_id="PAY-REV-001",
        provider_reference="HSBC-2026-R883",
        sender_bank="HSBC",
        beneficiary_account_id="ACC-LEN-001",
        raw_reference="HSBC-88319 ACC-LEN-001",
        amount_minor=21000,  # £210.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.REVERSED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_reversed)
    session.add(
        PaymentEvent(
            event_id="EVT-REV-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-REV-001",
            event_type="PAYMENT_REVERSED_BY_SENDER_BANK",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-REV-001-REVERSED"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="HSBC-2026-R883",
                sender_bank="HSBC",
                amount_minor=21000,
                currency="GBP",
                raw_reference="HSBC-88319 ACC-LEN-001",
            ),
        )
    )
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 6 (and S3): Ambiguous beneficiary payment (£1,200.00 in Suspense)
    # -------------------------------------------------------------------------
    pay_s3 = IncomingPayment(
        payment_id="PAY-S3-003",
        provider_reference="FP-2026-X883",
        sender_bank="Monzo",
        beneficiary_account_id=None,  # No unique account code on wire
        raw_reference="MON-33112 BEN CHIDERA",
        amount_minor=120000,  # £1,200.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s3)
    session.commit()

    jrn_s3 = JournalEntry(
        journal_id="JRN-S3-001",
        idempotency_key="RECEIPT-PAY-S3-003",
        reference="PAY-S3-003",
        description="Ambiguous receipt in suspense for PAY-S3-003",
        posted_at=now_utc(),
    )
    session.add(jrn_s3)
    session.commit()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s3.id,
                account_id="ACC-SETTLEMENT-CASH",
                debit_minor=120000,
                credit_minor=0,
                source_payment_id="PAY-S3-003",
            ),
            LedgerEntry(
                journal_id=jrn_s3.id,
                account_id="ACC-UNALLOCATED-SUSPENSE",
                debit_minor=0,
                credit_minor=120000,
                source_payment_id="PAY-S3-003",
            ),
        ]
    )
    session.add(
        PaymentEvent(
            event_id="EVT-S3-003",
            source="FASTER_PAYMENTS",
            payment_id="PAY-S3-003",
            event_type="FUNDS_SETTLED",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-S3-003-SETTLED"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="FP-2026-X883",
                sender_bank="Monzo",
                amount_minor=120000,
                currency="GBP",
                raw_reference="MON-33112 BEN CHIDERA",
            ),
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S3-003",
            customer_id="CUST-103",
            beneficiary_account_id="ACC-LEN-003",
            sender_bank="Monzo",
            reference="MON-33112",
            amount_minor=120000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-BEN-AMBIGUOUS",
            customer_id="CUST-101",
            beneficiary_account_id="ACC-LEN-001",
            sender_bank="Monzo",
            reference="MON-33112",
            amount_minor=120000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.commit()

    # -------------------------------------------------------------------------
    # Scenario 7 (and S5): Restricted / Frozen Account (£750.00 in Suspense)
    # -------------------------------------------------------------------------
    pay_s5 = IncomingPayment(
        payment_id="PAY-S5-005",
        provider_reference="FP-2026-X885",
        sender_bank="Monzo",
        beneficiary_account_id="ACC-LEN-005",
        raw_reference="MON-55441 ACC-LEN-005",
        amount_minor=75000,  # £750.00
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s5)
    session.commit()

    jrn_s5 = JournalEntry(
        journal_id="JRN-S5-001",
        idempotency_key="RECEIPT-PAY-S5-005",
        reference="PAY-S5-005",
        description="Receipt parked in suspense for frozen account PAY-S5-005",
        posted_at=now_utc(),
    )
    session.add(jrn_s5)
    session.commit()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s5.id,
                account_id="ACC-SETTLEMENT-CASH",
                debit_minor=75000,
                credit_minor=0,
                source_payment_id="PAY-S5-005",
            ),
            LedgerEntry(
                journal_id=jrn_s5.id,
                account_id="ACC-UNALLOCATED-SUSPENSE",
                debit_minor=0,
                credit_minor=75000,
                source_payment_id="PAY-S5-005",
            ),
        ]
    )
    session.add(
        PaymentEvent(
            event_id="EVT-S5-005",
            source="FASTER_PAYMENTS",
            payment_id="PAY-S5-005",
            event_type="FUNDS_SETTLED",
            event_time=now_utc(),
            payload_hash=hash_payload("PAY-S5-005-SETTLED"),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_ref="FP-2026-X885",
                sender_bank="Monzo",
                amount_minor=75000,
                currency="GBP",
                raw_reference="MON-55441 ACC-LEN-005",
            ),
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S5-005",
            customer_id="CUST-105",
            beneficiary_account_id="ACC-LEN-005",
            sender_bank="Monzo",
            reference="MON-55441",
            amount_minor=75000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.commit()


def seed_database(session: Optional[Session] = None) -> None:
    """Re-creates all SQLite tables and seeds the 7 core ResolveFlow scenarios."""
    if session is not None:
        populate_session_with_demo_data(session)
        return

    print("1. Re-creating database tables with latest schema in SQLite...")
    import app.models  # noqa: F401

    SQLModel.metadata.drop_all(engine)
    init_db()

    with Session(engine) as db_session:
        populate_session_with_demo_data(db_session)

    print("\n✅ Seed complete! SQLite database populated with all 7 ResolveFlow scenarios.")


if __name__ == "__main__":
    seed_database()
