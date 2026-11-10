import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from main import app
from app.core.database import get_session
from app.models.account import CustomerAccount
from app.models.case import ApprovalRequest
from app.models.enums import AccountStatus, AccountType
from app.models.ledger import JournalEntry
from app.services.ledger_service import (
    SETTLEMENT_CASH_ACCOUNT,
    UNALLOCATED_SUSPENSE_ACCOUNT,
    get_account_balance_minor,
)
from app.services.rail_simulator import PaymentRailSimulator


@pytest.fixture(name="session")
def session_fixture():
    """Isolated in-memory SQLite database seeded with institutional and customer accounts."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
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
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="CUST-102",
                    account_id="ACC-LEN-002",
                    account_type=AccountType.LIABILITY,
                    currency="GBP",
                    status=AccountStatus.ACTIVE,
                ),
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="CUST-103",
                    account_id="ACC-LEN-003",
                    account_type=AccountType.LIABILITY,
                    currency="GBP",
                    status=AccountStatus.ACTIVE,
                ),
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="CUST-104",
                    account_id="ACC-LEN-004",
                    account_type=AccountType.LIABILITY,
                    currency="GBP",
                    status=AccountStatus.ACTIVE,
                ),
                CustomerAccount(
                    tenant_id="lenfi_uk",
                    customer_id="CUST-105",
                    account_id="ACC-LEN-005",
                    account_type=AccountType.LIABILITY,
                    currency="GBP",
                    status=AccountStatus.FROZEN,
                ),
            ]
        )
        session.commit()
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_scenario_s1_auto_allocate_happy_path(client: TestClient, session: Session):
    """Scenario S1: Funds received in suspense + unique active account match -> safe AUTO_ALLOCATE."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-S1-001",
        event_id="EVT-S1-001",
        amount_minor=50000,
        raw_reference="MON-78291 ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=wire.model_dump(mode="json"))

    case_res = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-78291",
            "amount_minor": 50000,
            "currency": "GBP",
        },
    )
    case_id = case_res.json()["case_id"]

    inv_res = client.post(f"/api/v1/investigations/{case_id}/run")
    assert inv_res.status_code == 200
    result = inv_res.json()

    assert result["proposed_action"] == "AUTO_ALLOCATE"
    assert result["policy_approved"] is True
    assert result["execution_status"] == "EXECUTED"
    assert result["case_state"] == "RESOLVED"
    assert result["journal_id"] == "JRN-ALLOC-PAY-FP-S1-001"

    # Suspense cleared (0), Customer credited (+50000)
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 0
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_scenario_s2_missing_receipt_refuses_credit_and_traces(
    client: TestClient, session: Session
):
    """Scenario S2: Customer gives reference, no authoritative receipt -> TRACE_WITH_PROVIDER, 0 credit."""
    case_res = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-102",
            "beneficiary_account_id": "ACC-LEN-002",
            "sender_bank": "Monzo",
            "payment_reference": "MON-99881",
            "amount_minor": 25000,
            "currency": "GBP",
        },
    )
    case_id = case_res.json()["case_id"]

    inv_res = client.post(f"/api/v1/investigations/{case_id}/run")
    assert inv_res.status_code == 200
    result = inv_res.json()

    assert result["proposed_action"] == "TRACE_WITH_PROVIDER"
    assert result["execution_status"] == "NOT_EXECUTED"
    assert result["case_state"] == "WAITING_PROVIDER"
    assert "RECEIPT_ABSENT" in result["reconciliation_flags"]
    assert get_account_balance_minor(session, "ACC-LEN-002") == 0


def test_scenario_s3_ambiguous_beneficiary_routes_to_human_review(
    client: TestClient, session: Session
):
    """Scenario S3: Receipt exists in suspense, but reference lacks unique account code -> HUMAN_REVIEW."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-S3-003",
        event_id="EVT-S3-003",
        amount_minor=120000,
        raw_reference="MON-33112 BEN CHIDERA",
    )
    client.post("/api/v1/provider-events", json=wire.model_dump(mode="json"))

    case_res = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-103",
            "beneficiary_account_id": "ACC-LEN-003",
            "sender_bank": "Monzo",
            "payment_reference": "MON-33112",
            "amount_minor": 120000,
            "currency": "GBP",
        },
    )
    case_id = case_res.json()["case_id"]

    inv_res = client.post(f"/api/v1/investigations/{case_id}/run")
    assert inv_res.status_code == 200
    result = inv_res.json()

    assert result["proposed_action"] == "HUMAN_REVIEW"
    assert result["requires_human_approval"] is True
    assert result["execution_status"] == "AWAITING_HUMAN_APPROVAL"
    assert result["case_state"] == "AWAITING_REVIEW"
    assert result["approval_id"].startswith("APP-")
    assert "AMBIGUOUS_BENEFICIARY" in result["reconciliation_flags"]

    # Funds remain held in suspense; zero customer credit
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 120000
    assert get_account_balance_minor(session, "ACC-LEN-003") == 0


def test_scenario_s4_already_allocated_idempotent_noop(
    client: TestClient, session: Session
):
    """Scenario S4: Re-running investigation or duplicate complaint on allocated payment is a safe no-op."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-S4-004",
        event_id="EVT-S4-004",
        amount_minor=15000,
        raw_reference="MON-11223 ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=wire.model_dump(mode="json"))

    case1 = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-11223",
            "amount_minor": 15000,
            "currency": "GBP",
        },
    ).json()["case_id"]

    # First run allocates £150
    res1 = client.post(f"/api/v1/investigations/{case1}/run")
    assert res1.json()["execution_status"] == "EXECUTED"
    assert get_account_balance_minor(session, "ACC-LEN-001") == 15000

    # Customer files a new complaint after case1 resolved, or re-triggers investigation
    case2 = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-11223",
            "amount_minor": 15000,
            "currency": "GBP",
        },
    ).json()["case_id"]

    res2 = client.post(f"/api/v1/investigations/{case2}/run")
    assert res2.status_code == 200
    assert res2.json()["execution_status"] == "IDEMPOTENT_NOOP"
    assert res2.json()["case_state"] == "RESOLVED"
    assert "DUPLICATE_CREDIT_DETECTED" in res2.json()["reconciliation_flags"]

    # Balance is strictly 15000 (never 30000)
    assert get_account_balance_minor(session, "ACC-LEN-001") == 15000


def test_scenario_s5_frozen_account_strictly_blocked_by_policy(
    client: TestClient, session: Session
):
    """Scenario S5: Exact receipt match for a FROZEN account -> blocked & routed to HUMAN_REVIEW."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-S5-005",
        event_id="EVT-S5-005",
        amount_minor=75000,
        raw_reference="MON-55441 ACC-LEN-005",
    )
    client.post("/api/v1/provider-events", json=wire.model_dump(mode="json"))

    case_id = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-105",
            "beneficiary_account_id": "ACC-LEN-005",
            "sender_bank": "Monzo",
            "payment_reference": "MON-55441",
            "amount_minor": 75000,
            "currency": "GBP",
        },
    ).json()["case_id"]

    res = client.post(f"/api/v1/investigations/{case_id}/run")
    assert res.status_code == 200
    data = res.json()

    assert "ACCOUNT_RESTRICTED" in data["reconciliation_flags"]
    assert data["proposed_action"] == "HUMAN_REVIEW"
    assert data["execution_status"] == "AWAITING_HUMAN_APPROVAL"
    assert data["case_state"] == "AWAITING_REVIEW"
    assert get_account_balance_minor(session, "ACC-LEN-005") == 0


def test_scenario_s6_late_arrival_resumes_and_allocates(
    client: TestClient, session: Session
):
    """Scenario S6: Initial S2 case waits for provider; wire arrives later -> re-evaluation allocates."""
    case_id = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-102",
            "beneficiary_account_id": "ACC-LEN-002",
            "sender_bank": "Monzo",
            "payment_reference": "MON-LATE-77",
            "amount_minor": 25000,
            "currency": "GBP",
        },
    ).json()["case_id"]

    # Step 1: Run before wire arrives -> WAITING_PROVIDER
    res1 = client.post(f"/api/v1/investigations/{case_id}/run")
    assert res1.json()["case_state"] == "WAITING_PROVIDER"
    assert get_account_balance_minor(session, "ACC-LEN-002") == 0

    # Step 2: Provider webhook arrives 2 hours later
    late_wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-LATE-77",
        event_id="EVT-LATE-77",
        amount_minor=25000,
        raw_reference="MON-LATE-77 ACC-LEN-002",
    )
    client.post("/api/v1/provider-events", json=late_wire.model_dump(mode="json"))

    # Step 3: Resume investigation on fresh evidence -> RESOLVED & EXECUTED
    res2 = client.post(f"/api/v1/investigations/{case_id}/run")
    assert res2.status_code == 200
    assert res2.json()["execution_status"] == "EXECUTED"
    assert res2.json()["case_state"] == "RESOLVED"
    assert get_account_balance_minor(session, "ACC-LEN-002") == 25000


def test_high_value_threshold_blocks_auto_allocation(
    client: TestClient, session: Session
):
    """Invariant 5: Payments >= £10,000 (1,000,000 pence) mandate human dual-authorization."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-HIGH-999",
        event_id="EVT-HIGH-999",
        amount_minor=1_500_000,  # £15,000.00
        raw_reference="MON-HIGH-999 ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=wire.model_dump(mode="json"))

    case_id = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-HIGH-999",
            "amount_minor": 1_500_000,
            "currency": "GBP",
        },
    ).json()["case_id"]

    res = client.post(f"/api/v1/investigations/{case_id}/run")
    data = res.json()
    assert "HIGH_VALUE_THRESHOLD_EXCEEDED" in data["reconciliation_flags"]
    assert data["proposed_action"] == "HUMAN_REVIEW"
    assert data["execution_status"] == "AWAITING_HUMAN_APPROVAL"
    assert get_account_balance_minor(session, "ACC-LEN-001") == 0

    # Verify all journals in database remain 100% balanced
    journals = session.exec(select(JournalEntry)).all()
    for jrn in journals:
        assert sum(leg.debit_minor for leg in jrn.lines) == sum(
            leg.credit_minor for leg in jrn.lines
        )

