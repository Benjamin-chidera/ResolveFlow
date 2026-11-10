import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from main import app
from app.core.database import get_session
from app.models.account import CustomerAccount
from app.models.enums import AccountStatus, AccountType
from app.models.ledger import JournalEntry, LedgerEntry
from app.models.payment import IncomingPayment, PaymentEvent
from app.services.ledger_service import (
    DuplicateAllocationError,
    SETTLEMENT_CASH_ACCOUNT,
    UNALLOCATED_SUSPENSE_ACCOUNT,
    get_account_balance_minor,
    post_customer_allocation,
)
from app.services.rail_simulator import PaymentRailSimulator
from app.services.reference_parser import parse_beneficiary_account_id


@pytest.fixture(name="session")
def session_fixture():
    """Creates an isolated in-memory SQLite database for each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        # Seed institutional accounts and one active customer account
        session.add_all(
            [
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="INSTITUTION",
                    account_id=SETTLEMENT_CASH_ACCOUNT,
                    account_type=AccountType.ASSET,
                    currency="GBP",
                    status=AccountStatus.ACTIVE,
                ),
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="INSTITUTION",
                    account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
                    account_type=AccountType.LIABILITY,
                    currency="GBP",
                    status=AccountStatus.ACTIVE,
                ),
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="CUST-101",
                    account_id="ACC-LEN-001",
                    account_type=AccountType.LIABILITY,
                    currency="GBP",
                    status=AccountStatus.ACTIVE,
                ),
            ]
        )
        session.commit()
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session):
    """TestClient wired to the isolated test session."""
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_reference_parser_rules():
    """Verifies exact, shorthand, missing, and conflicting account code extraction."""
    assert parse_beneficiary_account_id("MON-78291 ACC-LEN-001") == "ACC-LEN-001"
    assert parse_beneficiary_account_id("Transfer for len-001") == "ACC-LEN-001"
    assert parse_beneficiary_account_id("MON-33112 BEN CHIDERA") is None
    assert parse_beneficiary_account_id("ACC-LEN-001 or ACC-LEN-002") is None


def test_new_payment_ingestion_and_balanced_suspense_booking(
    client: TestClient, session: Session
):
    """Verifies a new provider event returns 201 and books a balanced journal into suspense."""
    payload = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-2026-9001",
        event_id="EVT-9001",
        amount_minor=50000,
        raw_reference="MON-78291 ACC-LEN-001",
    )

    res = client.post("/api/v1/provider-events", json=payload.model_dump(mode="json"))
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "INGESTED"
    assert data["payment_id"] == "PAY-FP-2026-9001"
    assert data["allocation_status"] == "UNALLOCATED"
    assert data["duplicate"] is False

    # Verify double-entry balances: Cash Asset = +50000, Suspense Liability = +50000, Customer = 0
    assert get_account_balance_minor(session, SETTLEMENT_CASH_ACCOUNT) == 50000
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 50000
    assert get_account_balance_minor(session, "ACC-LEN-001") == 0


def test_tier1_deduplication_by_source_and_event_id(
    client: TestClient, session: Session
):
    """Verifies Tier 1 catches duplicate (source, event_id) even if timestamp changes."""
    original = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-2026-9002",
        event_id="EVT-9002",
        amount_minor=30000,
    )
    res1 = client.post("/api/v1/provider-events", json=original.model_dump(mode="json"))
    assert res1.status_code == 201

    # Replay with same event_id but shifted timestamp (+45s)
    replay = PaymentRailSimulator.replay_event(original, alter_event_id=False, shift_seconds=45)
    res2 = client.post("/api/v1/provider-events", json=replay.model_dump(mode="json"))
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "DUPLICATE_IGNORED"
    assert data2["deduplication_tier"] == "TIER_1_EVENT_ID"
    assert data2["duplicate"] is True

    # Ensure suspense was only credited once
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 30000


def test_tier2_deduplication_by_canonical_fingerprint(
    client: TestClient, session: Session
):
    """Verifies Tier 2 catches duplicate delivery when event_id and timestamp both change."""
    original = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-2026-9003",
        event_id="EVT-9003-A",
        amount_minor=45000,
        raw_reference="MON-55511 ACC-LEN-001",
    )
    res1 = client.post("/api/v1/provider-events", json=original.model_dump(mode="json"))
    assert res1.status_code == 201

    # Replay with a brand new event_id AND shifted timestamp
    replay = PaymentRailSimulator.replay_event(original, alter_event_id=True, shift_seconds=120)
    assert replay.event_id != original.event_id

    res2 = client.post("/api/v1/provider-events", json=replay.model_dump(mode="json"))
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "DUPLICATE_IGNORED"
    assert data2["deduplication_tier"] == "TIER_2_CANONICAL_FINGERPRINT"
    assert data2["duplicate"] is True

    # Verify only 1 payment record and 1 journal exist
    payments = session.exec(select(IncomingPayment)).all()
    journals = session.exec(select(JournalEntry)).all()
    assert len(payments) == 1
    assert len(journals) == 1
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 45000


def test_tier3_financial_allocation_key_blocks_double_credit(
    client: TestClient, session: Session
):
    """Verifies Tier 3 unique allocation key physically prevents crediting a customer twice."""
    payload = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-2026-9004",
        event_id="EVT-9004",
        amount_minor=50000,
        raw_reference="MON-78291 ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=payload.model_dump(mode="json"))

    # First allocation succeeds: Suspense -> 0, Customer ACC-LEN-001 -> +50000
    jrn = post_customer_allocation(
        session=session,
        payment_id="PAY-FP-2026-9004",
        beneficiary_account_id="ACC-LEN-001",
        amount_minor=50000,
        case_reference="CASE-S1-001",
    )
    session.commit()
    assert jrn.idempotency_key == "ALLOC-PAY-FP-2026-9004"
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 0
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000

    # Second allocation attempt for the same payment MUST raise DuplicateAllocationError
    with pytest.raises(DuplicateAllocationError):
        post_customer_allocation(
            session=session,
            payment_id="PAY-FP-2026-9004",
            beneficiary_account_id="ACC-LEN-001",
            amount_minor=50000,
            case_reference="CASE-S1-001-RETRY",
        )

    # Customer balance remains strictly 50000 (never 100000)
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_payment_trace_endpoint(client: TestClient):
    """Verifies GET /api/v1/payments/{id}/trace returns timeline and ledger legs."""
    payload = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-2026-9005",
        event_id="EVT-9005",
        amount_minor=75000,
        raw_reference="MON-12345 ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=payload.model_dump(mode="json"))

    trace_res = client.get("/api/v1/payments/PAY-FP-2026-9005/trace")
    assert trace_res.status_code == 200
    trace_data = trace_res.json()
    assert trace_data["payment_id"] == "PAY-FP-2026-9005"
    assert trace_data["beneficiary_account_id"] == "ACC-LEN-001"
    assert len(trace_data["ledger_legs"]) == 2
    stages = [item["stage"] for item in trace_data["timeline"]]
    assert "PROVIDER_EVENT" in stages
    assert "SUSPENSE_BOOKED" in stages

