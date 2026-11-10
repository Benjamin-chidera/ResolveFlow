from typing import List
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.graph import run_case_investigation
from app.cli.terminal_app import run_terminal_mvp
from app.models.account import CustomerAccount
from app.models.case import AuditEvent, InvestigationCase
from app.models.chat import CustomerEvidenceClaim
from app.models.enums import CaseState
from app.models.ledger import JournalEntry, LedgerEntry
from app.models.payment import IncomingPayment
from app.services.customer_portal_service import (
    PRIMARY_DEMO_ACCOUNT_ID,
    PRIMARY_DEMO_CUSTOMER_ID,
    CustomerIsolationError,
    InvalidPinError,
    PinValidationError,
    authenticate_customer_pin,
    get_customer_home_summary,
    get_customer_incoming_item_by_id,
    investigate_customer_incoming_item,
    list_customer_incoming_items,
    send_contextual_chat_turn,
    setup_customer_profile_and_pin,
    start_contextual_payment_chat,
)
from app.services.evaluation_service import verify_all_journals_balanced
from app.services.ingestion_service import ProviderEventPayload, ingest_provider_event
from app.services.ledger_service import (
    SETTLEMENT_CASH_ACCOUNT,
    UNALLOCATED_SUSPENSE_ACCOUNT,
    get_account_balance_minor,
)
from seed import populate_session_with_demo_data


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        populate_session_with_demo_data(session)
        yield session


def test_pin_hashed_and_never_stored_in_plaintext(session: Session):
    """Requirement 1: PINs are never stored in plain text; PBKDF2 hash + salt verified."""
    with pytest.raises(PinValidationError):
        setup_customer_profile_and_pin(
            session, PRIMARY_DEMO_CUSTOMER_ID, "Benjamin Chidera", "12ab", "12ab"
        )

    with pytest.raises(PinValidationError):
        setup_customer_profile_and_pin(
            session, PRIMARY_DEMO_CUSTOMER_ID, "Benjamin Chidera", "4829", "9999"
        )

    account = setup_customer_profile_and_pin(
        session=session,
        customer_id=PRIMARY_DEMO_CUSTOMER_ID,
        customer_name="Benjamin Chidera",
        pin="4829",
        confirm_pin="4829",
    )
    assert account.is_onboarded is True
    assert account.pin_hash is not None
    assert account.pin_salt is not None
    assert "4829" not in account.pin_hash
    assert "4829" not in account.pin_salt
    assert len(account.pin_hash) == 64

    # Ensure audit logs also never contain the plain PIN
    audits = session.exec(select(AuditEvent)).all()
    for audit in audits:
        assert "4829" not in (audit.details or "")

    with pytest.raises(InvalidPinError):
        authenticate_customer_pin(session, PRIMARY_DEMO_CUSTOMER_ID, "0000")

    verified = authenticate_customer_pin(session, PRIMARY_DEMO_CUSTOMER_ID, "4829")
    assert verified.account_id == PRIMARY_DEMO_ACCOUNT_ID
    assert verified.account_number == "84920101"
    assert verified.sort_code == "04-00-75"


def test_settled_incoming_payment_posted_to_cash_and_suspense_once(session: Session):
    """Requirement 2: Settled incoming payment is posted to settlement cash and suspense once."""
    payload = ProviderEventPayload(
        event_id="EVT-NEW-900",
        source="FASTER_PAYMENTS",
        event_type="FUNDS_SETTLED",
        provider_reference="FP-2026-900",
        sender_bank="Monzo",
        raw_reference="MON-90001 ACC-LEN-001",
        amount_minor=40000,
        currency="GBP",
    )
    res1 = ingest_provider_event(session, payload)
    res2 = ingest_provider_event(session, payload)  # Duplicate replay

    assert res1.status == "INGESTED"
    assert res2.status == "DUPLICATE_IGNORED"

    receipt_legs = session.exec(
        select(LedgerEntry).where(LedgerEntry.source_payment_id == "PAY-FP-2026-900")
    ).all()
    assert len(receipt_legs) == 2
    cash_leg = [l for l in receipt_legs if l.account_id == SETTLEMENT_CASH_ACCOUNT][0]
    susp_leg = [
        l for l in receipt_legs if l.account_id == UNALLOCATED_SUSPENSE_ACCOUNT
    ][0]
    assert cash_leg.debit_minor == 40000
    assert susp_leg.credit_minor == 40000


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("FUNDS_PENDING", "PENDING"),
        ("FUNDS_FAILED", "FAILED"),
        ("FUNDS_REVERSED", "REVERSED"),
    ],
)
def test_non_settled_provider_events_are_never_booked_to_suspense(
    session: Session, event_type: str, expected_status: str
):
    cash_before = get_account_balance_minor(session, SETTLEMENT_CASH_ACCOUNT)
    suspense_before = get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT)
    result = ingest_provider_event(
        session,
        ProviderEventPayload(
            event_id=f"EVT-{event_type}",
            source="FASTER_PAYMENTS",
            event_type=event_type,
            provider_reference=f"FP-{event_type}",
            sender_bank="Monzo",
            raw_reference="MON-STATUS ACC-LEN-001",
            amount_minor=12500,
            currency="GBP",
        ),
    )

    payment = session.exec(
        select(IncomingPayment).where(
            IncomingPayment.payment_id == f"PAY-FP-{event_type}"
        )
    ).one()
    legs = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.source_payment_id == payment.payment_id
        )
    ).all()

    assert result.journal_id is None
    assert payment.provider_status.value == expected_status
    assert payment.allocation_status.value == "ON_HOLD"
    assert legs == []
    assert get_account_balance_minor(session, SETTLEMENT_CASH_ACCOUNT) == cash_before
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == suspense_before


def test_safe_uniquely_matched_payment_allocated_and_duplicate_prevented(
    session: Session,
):
    """Requirements 3 & 4: Safe uniquely matched payment allocates once; replays never double-credit."""
    # Benjamin starts with £150.00 (15000) from PAY-S4-004
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == 15000

    # First investigation of PAY-S1-001 (£500.00 in Suspense) allocates £500.00
    outcome1 = investigate_customer_incoming_item(
        session=session,
        customer_id=PRIMARY_DEMO_CUSTOMER_ID,
        item_id="PAY-S1-001",
    )
    assert outcome1.credited_to_customer is True
    assert outcome1.execution_status == "EXECUTED"
    assert outcome1.new_balance_minor == 65000  # £150 + £500 = £650.00

    # Repeated investigation of PAY-S1-001 is a safe idempotent no-op
    outcome2 = investigate_customer_incoming_item(
        session=session,
        customer_id=PRIMARY_DEMO_CUSTOMER_ID,
        item_id="PAY-S1-001",
    )
    assert outcome2.credited_to_customer is False
    assert outcome2.execution_status == "IDEMPOTENT_NOOP"
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == 65000

    credits_s1 = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.source_payment_id == "PAY-S1-001",
            LedgerEntry.account_id == PRIMARY_DEMO_ACCOUNT_ID,
            LedgerEntry.credit_minor > 0,
        )
    ).all()
    assert len(credits_s1) == 1


def test_no_receipt_and_pending_failed_reversed_never_credit_benjamin(
    session: Session,
):
    """Requirements 5 & 6: Missing receipts, PENDING, FAILED, and REVERSED payments never credit Benjamin."""
    initial_balance = get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID)
    assert initial_balance == 15000

    # 1. No receipt found (CASE-BEN-NORECEIPT: £250.00 MON-99881)
    no_receipt_res = investigate_customer_incoming_item(
        session, PRIMARY_DEMO_CUSTOMER_ID, "CASE-BEN-NORECEIPT"
    )
    assert no_receipt_res.credited_to_customer is False
    assert no_receipt_res.case_state == CaseState.WAITING_PROVIDER.value
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == initial_balance

    # 2. Pending payment (PAY-PEND-001: £180.00 BARC-44910)
    pending_res = investigate_customer_incoming_item(
        session, PRIMARY_DEMO_CUSTOMER_ID, "PAY-PEND-001"
    )
    assert pending_res.credited_to_customer is False
    assert pending_res.case_state == CaseState.WAITING_PROVIDER.value
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == initial_balance

    # 3. Failed payment (PAY-FAIL-001: £95.00 REV-66201)
    failed_res = investigate_customer_incoming_item(
        session, PRIMARY_DEMO_CUSTOMER_ID, "PAY-FAIL-001"
    )
    assert failed_res.credited_to_customer is False
    assert failed_res.case_state == CaseState.AWAITING_REVIEW.value
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == initial_balance

    # 4. Reversed payment (PAY-REV-001: £210.00 HSBC-88319)
    reversed_res = investigate_customer_incoming_item(
        session, PRIMARY_DEMO_CUSTOMER_ID, "PAY-REV-001"
    )
    assert reversed_res.credited_to_customer is False
    assert reversed_res.case_state == CaseState.AWAITING_REVIEW.value
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == initial_balance


def test_ambiguous_restricted_high_value_and_contradictory_route_to_human_review(
    session: Session,
):
    """Requirement 7: Ambiguous, restricted, high-value, and contradictory cases route to human review."""
    # 1. Ambiguous beneficiary (CASE-BEN-AMBIGUOUS / PAY-S3-003: MON-33112 BEN CHIDERA)
    ambig_res = investigate_customer_incoming_item(
        session, PRIMARY_DEMO_CUSTOMER_ID, "CASE-BEN-AMBIGUOUS"
    )
    assert ambig_res.credited_to_customer is False
    assert ambig_res.case_state == CaseState.AWAITING_REVIEW.value
    assert "ACC-LEN-003" not in ambig_res.customer_safe_message
    assert "ACC-LEN-004" not in ambig_res.customer_safe_message

    # 2. Restricted / Frozen account (CASE-S5-005 / ACC-LEN-005)
    frozen_res = run_case_investigation(session, "CASE-S5-005")
    assert frozen_res.proposed_action == "HUMAN_REVIEW"
    assert frozen_res.case_state == CaseState.AWAITING_REVIEW.value
    assert get_account_balance_minor(session, "ACC-LEN-005") == 0

    # 3. High-value incoming wire (£12,000.00 >= £10,000.00 threshold)
    ingest_provider_event(
        session,
        ProviderEventPayload(
            event_id="EVT-HV-001",
            source="FASTER_PAYMENTS",
            event_type="FUNDS_SETTLED",
            provider_reference="FP-HV-001",
            sender_bank="Barclays",
            raw_reference="BARC-HV-99 ACC-LEN-001",
            amount_minor=1_200_000,
            currency="GBP",
        ),
    )
    hv_outcome = investigate_customer_incoming_item(
        session, PRIMARY_DEMO_CUSTOMER_ID, "PAY-FP-HV-001"
    )
    assert hv_outcome.credited_to_customer is False
    assert hv_outcome.case_state == CaseState.AWAITING_REVIEW.value

    # 4. Contradictory amount claim (£500 wire PAY-S1-001 vs £450 customer claim)
    contradict_case = InvestigationCase(
        case_id="CASE-CONTRADICT-01",
        customer_id=PRIMARY_DEMO_CUSTOMER_ID,
        beneficiary_account_id=PRIMARY_DEMO_ACCOUNT_ID,
        sender_bank="Monzo",
        reference="MON-78291",
        amount_minor=45000,  # Contradicts £500.00 (50000) settled receipt
        currency="GBP",
        state=CaseState.OPEN,
    )
    session.add(contradict_case)
    session.commit()

    contradict_res = run_case_investigation(session, "CASE-CONTRADICT-01")
    assert contradict_res.proposed_action == "HUMAN_REVIEW"
    assert contradict_res.case_state == CaseState.AWAITING_REVIEW.value


def test_strict_customer_isolation_and_untrusted_chat_claims(session: Session):
    """Requirements 8, 9 & 10: Customer isolation, untrusted evidence in chat, and 100% balanced journals."""
    # Benjamin can only see his own items
    ben_items = list_customer_incoming_items(session, PRIMARY_DEMO_CUSTOMER_ID)
    ben_ids = {i.item_id for i in ben_items}
    assert "PAY-S5-005" not in ben_ids
    assert "CASE-S2-002" not in ben_ids
    assert "CASE-S3-003" not in ben_ids

    with pytest.raises(CustomerIsolationError):
        get_customer_incoming_item_by_id(
            session, PRIMARY_DEMO_CUSTOMER_ID, "PAY-S5-005"
        )

    # Agent chat treats customer statements as untrusted evidence and never credits unverified claims
    chat_ctx = start_contextual_payment_chat(
        session, PRIMARY_DEMO_CUSTOMER_ID, selected_item=None
    )
    turn_res = send_contextual_chat_turn(
        session=session,
        customer_id=PRIMARY_DEMO_CUSTOMER_ID,
        session_id=chat_ctx["session_id"],
        user_message="I sent £800 from Monzo yesterday with reference MON-UNVERIFIED-800, please credit me!",
    )
    assert turn_res["investigation_ran"] is True
    assert turn_res["case_state"] == CaseState.WAITING_PROVIDER.value
    assert turn_res["execution_status"] == "NOT_EXECUTED"
    assert get_account_balance_minor(session, PRIMARY_DEMO_ACCOUNT_ID) == 15000

    # Verify CustomerEvidenceClaim v1 was recorded
    claim = session.exec(
        select(CustomerEvidenceClaim).where(
            CustomerEvidenceClaim.case_id == turn_res["case_id"]
        )
    ).first()
    assert claim is not None
    assert claim.claimed_reference == "MON-UNVERIFIED-800"
    assert claim.claimed_amount_minor == 80000

    # Verify all ledger journals remain 100% balanced
    balance_report = verify_all_journals_balanced(session)
    assert balance_report["passed"] is True
    assert balance_report["compliance_percentage"] == 100.0


def test_interactive_terminal_mvp_end_to_end_journey(session: Session):
    """Drives the terminal MVP through onboarding, home screen, investigating PAY-S1-001, and chat."""
    scripted_inputs = iter(
        [
            "Benjamin Chidera",  # First-time setup: Name
            "2468",              # First-time setup: Choose PIN
            "2468",              # First-time setup: Confirm PIN
            "2",                 # Main menu: 2. Incoming payments
            "2",                 # Select payment #2 (PAY-S1-001 £500.00 from Monzo)
            "1",                 # Action [1]: Get help with this payment (allocates £500.00!)
            "0",                 # Back to Incoming payments list
            "0",                 # Back to Home screen (now showing £650.00!)
            "3",                 # Main menu: 3. Help
            "2",                 # Category 2: Card payment (shows honest MVP scope message)
            "1",                 # Category 1: Help with an incoming bank transfer
            "M",                 # Report a payment missing completely
            "I sent £300 from Monzo with reference MON-77700",
            "0",                 # Exit chat
            "0",                 # Back to Home
            "0",                 # Exit app
        ]
    )
    outputs: List[str] = []

    run_terminal_mvp(
        session=session,
        input_fn=lambda prompt: next(scripted_inputs),
        output_fn=lambda text: outputs.append(text),
    )

    full_transcript = "\n".join(outputs)
    assert "ACCOUNT SETUP CONFIRMED" in full_transcript
    assert "84920101" in full_transcript
    assert "04-00-75" in full_transcript
    assert "Payment Verified & Credited" in full_transcript
    assert "£650.00" in full_transcript
    assert "Not included in this demo" in full_transcript
    assert "WAITING_PROVIDER" not in full_transcript
    assert "Case #" not in full_transcript
    assert "Evidence v" not in full_transcript

