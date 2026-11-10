from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from main import app
from app.core.database import get_session
from app.models.account import CustomerAccount
from app.models.case import AuditEvent, InvestigationCase
from app.models.enums import AccountStatus, AccountType, CaseState
from app.models.payment import IncomingPayment
from app.services.case_service import (
    InvalidCaseStateTransitionError,
    transition_case_state,
)
from app.services.ledger_service import (
    SETTLEMENT_CASH_ACCOUNT,
    UNALLOCATED_SUSPENSE_ACCOUNT,
)
from app.services.rail_simulator import PaymentRailSimulator


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


def test_create_case_happy_path_and_get_case(client: TestClient, session: Session):
    """Verifies POST /api/v1/cases creates an OPEN case and GET /api/v1/cases/{id} reads it."""
    payload = {
        "customer_id": "CUST-101",
        "beneficiary_account_id": "ACC-LEN-001",
        "sender_bank": "Monzo",
        "payment_reference": "MON-78291",
        "amount_minor": 50000,
        "currency": "GBP",
        "expected_date": "2026-11-09",
    }

    res = client.post("/api/v1/cases", json=payload)
    assert res.status_code == 201
    created = res.json()
    assert created["case_id"].startswith("CASE-2026-")
    assert created["state"] == "OPEN"
    assert created["reference"] == "MON-78291"
    assert created["amount_minor"] == 50000

    # Verify GET /api/v1/cases/{case_id}
    get_res = client.get(f"/api/v1/cases/{created['case_id']}")
    assert get_res.status_code == 200
    detail = get_res.json()
    assert detail["case_id"] == created["case_id"]
    assert detail["beneficiary_account_id"] == "ACC-LEN-001"
    assert detail["state"] == "OPEN"

    # Verify audit event was written
    audits = session.exec(
        select(AuditEvent).where(AuditEvent.action == "CASE_OPENED")
    ).all()
    assert len(audits) == 1


def test_duplicate_active_case_returns_409_conflict(
    client: TestClient, session: Session
):
    """Verifies duplicate active complaint for same account + reference returns 409."""
    payload = {
        "customer_id": "CUST-101",
        "beneficiary_account_id": "ACC-LEN-001",
        "sender_bank": "Monzo",
        "payment_reference": "MON-78291",
        "amount_minor": 50000,
        "currency": "GBP",
    }

    res1 = client.post("/api/v1/cases", json=payload)
    assert res1.status_code == 201
    case_id_1 = res1.json()["case_id"]

    res2 = client.post("/api/v1/cases", json=payload)
    assert res2.status_code == 409
    assert res2.json()["case_id"] == case_id_1

    all_cases = session.exec(select(InvestigationCase)).all()
    assert len(all_cases) == 1


def test_unknown_or_institutional_beneficiary_account_rejected(client: TestClient):
    """Verifies invalid or institutional account IDs are rejected on intake."""
    bad_payload = {
        "customer_id": "CUST-999",
        "beneficiary_account_id": "ACC-NON-EXISTENT",
        "sender_bank": "Monzo",
        "payment_reference": "MON-11111",
        "amount_minor": 10000,
        "currency": "GBP",
    }
    res = client.post("/api/v1/cases", json=bad_payload)
    assert res.status_code == 404

    inst_payload = {
        "customer_id": "INSTITUTION",
        "beneficiary_account_id": SETTLEMENT_CASH_ACCOUNT,
        "sender_bank": "Monzo",
        "payment_reference": "MON-11111",
        "amount_minor": 10000,
        "currency": "GBP",
    }
    res2 = client.post("/api/v1/cases", json=inst_payload)
    assert res2.status_code == 404


def test_case_state_machine_valid_and_invalid_transitions(
    client: TestClient, session: Session
):
    """Verifies Spec 03 state machine transitions and blocks illegal jumps."""
    res = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-STATE-01",
            "amount_minor": 50000,
            "currency": "GBP",
        },
    )
    case_id = res.json()["case_id"]
    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()
    assert case is not None

    # Illegal direct jump from OPEN -> RESOLVED (must go through INVESTIGATING)
    with pytest.raises(InvalidCaseStateTransitionError):
        transition_case_state(
            session=session,
            case=case,
            target_state=CaseState.RESOLVED,
            actor="TestRunner",
            details="Illegal direct jump",
        )

    # Valid path: OPEN -> INVESTIGATING -> WAITING_PROVIDER -> INVESTIGATING -> RESOLVED
    transition_case_state(session, case, CaseState.INVESTIGATING, "Worker", "Start investigation")
    assert case.state == CaseState.INVESTIGATING

    transition_case_state(session, case, CaseState.WAITING_PROVIDER, "Worker", "S2 trace opened")
    assert case.state == CaseState.WAITING_PROVIDER

    transition_case_state(session, case, CaseState.INVESTIGATING, "Worker", "S6 late arrival")
    assert case.state == CaseState.INVESTIGATING

    transition_case_state(session, case, CaseState.RESOLVED, "PolicyEngine", "Allocated")
    assert case.state == CaseState.RESOLVED

    # Terminal state cannot reopen
    with pytest.raises(InvalidCaseStateTransitionError):
        transition_case_state(session, case, CaseState.OPEN, "Worker", "Reopen resolved")


def test_proactive_reconciliation_scanner_entry_b(
    client: TestClient, session: Session
):
    """Verifies POST /api/v1/reconciliation/run creates proactive cases only for uncovered aged payments."""
    # Ingest Payment 1 (covered by customer complaint)
    p1 = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-PRO-001",
        event_id="EVT-PRO-001",
        amount_minor=50000,
        raw_reference="MON-COVERED ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=p1.model_dump(mode="json"))

    # Ingest Payment 2 (uncovered unallocated receipt)
    p2 = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-PRO-002",
        event_id="EVT-PRO-002",
        amount_minor=80000,
        raw_reference="MON-UNCOVERED ACC-LEN-002",
    )
    client.post("/api/v1/provider-events", json=p2.model_dump(mode="json"))

    # Backdate both payments by 20 minutes so they qualify for the 15-minute threshold
    payments = session.exec(select(IncomingPayment)).all()
    for pay in payments:
        pay.received_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        session.add(pay)
    session.commit()

    # Customer files complaint for Payment 1
    client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-COVERED",
            "amount_minor": 50000,
            "currency": "GBP",
        },
    )

    # Trigger proactive scan
    scan_res = client.post("/api/v1/reconciliation/run?min_age_minutes=15")
    assert scan_res.status_code == 200
    scan_data = scan_res.json()
    assert scan_data["scanned_unallocated_count"] == 2
    assert scan_data["cases_created"] == 1
    proactive_case_id = scan_data["created_case_ids"][0]
    assert proactive_case_id.startswith("CASE-PRO-")

    # Re-running scan immediately creates 0 duplicate proactive cases
    scan_res_2 = client.post("/api/v1/reconciliation/run?min_age_minutes=15")
    assert scan_res_2.json()["cases_created"] == 0


def test_payment_trace_includes_complaint_opened_stage(client: TestClient):
    """Verifies GET /api/v1/payments/{id}/trace includes COMPLAINT_OPENED when a case matches."""
    p = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-TRACE-100",
        event_id="EVT-TRACE-100",
        amount_minor=50000,
        raw_reference="MON-TRACE-99 ACC-LEN-001",
    )
    client.post("/api/v1/provider-events", json=p.model_dump(mode="json"))

    client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-101",
            "beneficiary_account_id": "ACC-LEN-001",
            "sender_bank": "Monzo",
            "payment_reference": "MON-TRACE-99",
            "amount_minor": 50000,
            "currency": "GBP",
        },
    )

    trace_res = client.get("/api/v1/payments/PAY-FP-TRACE-100/trace")
    assert trace_res.status_code == 200
    stages = [item["stage"] for item in trace_res.json()["timeline"]]
    assert "COMPLAINT_OPENED" in stages

