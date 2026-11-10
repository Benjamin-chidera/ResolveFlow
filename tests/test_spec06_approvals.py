import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from main import app
from app.core.database import get_session
from app.models.account import CustomerAccount
from app.models.case import InvestigationCase
from app.models.enums import AccountStatus, AccountType
from app.services.ledger_service import (
    SETTLEMENT_CASH_ACCOUNT,
    UNALLOCATED_SUSPENSE_ACCOUNT,
    get_account_balance_minor,
)
from app.services.rail_simulator import PaymentRailSimulator


@pytest.fixture(name="session")
def session_fixture():
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


def _setup_ambiguous_s3_case(client: TestClient) -> tuple[str, str]:
    """Helper that creates an S3 ambiguous case and runs investigation to AWAITING_REVIEW."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-S3-900",
        event_id="EVT-S3-900",
        amount_minor=120000,
        raw_reference="MON-33112 BEN CHIDERA",
    )
    client.post("/api/v1/provider-events", json=wire.model_dump(mode="json"))

    case_id = client.post(
        "/api/v1/cases",
        json={
            "customer_id": "CUST-103",
            "beneficiary_account_id": "ACC-LEN-003",
            "sender_bank": "Monzo",
            "payment_reference": "MON-33112",
            "amount_minor": 120000,
            "currency": "GBP",
        },
    ).json()["case_id"]

    inv_res = client.post(f"/api/v1/investigations/{case_id}/run").json()
    return case_id, inv_res["approval_id"]


def test_s3_ambiguous_case_approved_with_disambiguated_account(
    client: TestClient, session: Session
):
    """Verifies operator approval disambiguates ACC-LEN-003, allocates funds, and resolves case."""
    case_id, approval_id = _setup_ambiguous_s3_case(client)

    # List pending approvals as PAYMENT_OPERATIONS
    pending_res = client.get(
        "/api/v1/approvals/pending",
        headers={"X-Reviewer-Role": "PAYMENT_OPERATIONS"},
    )
    assert pending_res.status_code == 200
    assert len(pending_res.json()) == 1
    assert pending_res.json()[0]["approval_id"] == approval_id

    # Approve and disambiguate to ACC-LEN-003
    dec_res = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={
            "reviewer_id": "ops_sarah",
            "reviewer_role": "PAYMENT_OPERATIONS",
            "decision": "APPROVED",
            "selected_account_id": "ACC-LEN-003",
            "reason": "Verified residential address matches Monzo statement.",
        },
    )
    assert dec_res.status_code == 200
    dec_data = dec_res.json()
    assert dec_data["decision"] == "APPROVED"
    assert dec_data["execution_status"] == "EXECUTED"
    assert dec_data["journal_id"] == "JRN-ALLOC-PAY-FP-S3-900"

    # Suspense cleared (0), ACC-LEN-003 credited (+120000), ACC-LEN-004 untouched (0)
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 0
    assert get_account_balance_minor(session, "ACC-LEN-003") == 120000
    assert get_account_balance_minor(session, "ACC-LEN-004") == 0

    case_detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert case_detail["state"] == "RESOLVED"


def test_rejection_flow_closes_case_and_keeps_funds_in_suspense(
    client: TestClient, session: Session
):
    """Verifies operator rejection closes case and leaves money safely in suspense."""
    case_id, approval_id = _setup_ambiguous_s3_case(client)

    dec_res = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={
            "reviewer_id": "ops_mark",
            "reviewer_role": "PAYMENT_OPERATIONS",
            "decision": "REJECTED",
            "reason": "Claimant failed identity verification for wire reference.",
        },
    )
    assert dec_res.status_code == 200
    assert dec_res.json()["execution_status"] == "REJECTED_NOT_EXECUTED"

    # Money remains in suspense (120000); customer balance is 0
    assert get_account_balance_minor(session, UNALLOCATED_SUSPENSE_ACCOUNT) == 120000
    assert get_account_balance_minor(session, "ACC-LEN-003") == 0

    case_detail = client.get(f"/api/v1/cases/{case_id}").json()
    assert case_detail["state"] == "CLOSED"


def test_unauthorized_role_blocked_with_403(client: TestClient):
    """Verifies non-operations roles cannot list or decide approvals."""
    _, approval_id = _setup_ambiguous_s3_case(client)

    res_list = client.get(
        "/api/v1/approvals/pending",
        headers={"X-Reviewer-Role": "CUSTOMER"},
    )
    assert res_list.status_code == 403

    res_dec = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={
            "reviewer_id": "cust_hacker",
            "reviewer_role": "CUSTOMER",
            "decision": "APPROVED",
            "selected_account_id": "ACC-LEN-003",
            "reason": "Self-approving my own transfer",
        },
    )
    assert res_dec.status_code == 403


def test_repeated_approval_submission_blocked_with_409_conflict(
    client: TestClient, session: Session
):
    """Verifies clicking Approve twice returns 409 Conflict and never double-credits."""
    _, approval_id = _setup_ambiguous_s3_case(client)

    payload = {
        "reviewer_id": "ops_sarah",
        "reviewer_role": "PAYMENT_OPERATIONS",
        "decision": "APPROVED",
        "selected_account_id": "ACC-LEN-003",
        "reason": "Verified customer identity.",
    }
    res1 = client.post(f"/api/v1/approvals/{approval_id}/decision", json=payload)
    assert res1.status_code == 200

    res2 = client.post(f"/api/v1/approvals/{approval_id}/decision", json=payload)
    assert res2.status_code == 409
    assert get_account_balance_minor(session, "ACC-LEN-003") == 120000


def test_stale_approval_rejected_when_evidence_version_changes(
    client: TestClient, session: Session
):
    """Verifies stale approval is rejected with 409 if case evidence_version advanced."""
    case_id, approval_id = _setup_ambiguous_s3_case(client)

    # Simulate new customer evidence arriving while approval was pending (v1 -> v2)
    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()
    assert case is not None
    case.latest_evidence_version = 2
    session.add(case)
    session.commit()

    res = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={
            "reviewer_id": "ops_sarah",
            "reviewer_role": "PAYMENT_OPERATIONS",
            "decision": "APPROVED",
            "selected_account_id": "ACC-LEN-003",
            "reason": "Approving based on outdated v1 evidence",
        },
    )
    assert res.status_code == 409
    assert "evidence version advanced" in res.json()["detail"]
    assert get_account_balance_minor(session, "ACC-LEN-003") == 0


def test_approval_cannot_allocate_to_frozen_account(
    client: TestClient, session: Session
):
    """Verifies even an operator approval cannot credit a FROZEN account until unfrozen."""
    wire = PaymentRailSimulator.emit_settled_payment(
        provider_ref="FP-S5-905",
        event_id="EVT-S5-905",
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

    inv_res = client.post(f"/api/v1/investigations/{case_id}/run").json()
    approval_id = inv_res["approval_id"]

    # Attempting to approve while ACC-LEN-005 is still FROZEN -> 409 Conflict
    res = client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={
            "reviewer_id": "ops_compliance",
            "reviewer_role": "COMPLIANCE_OFFICER",
            "decision": "APPROVED",
            "selected_account_id": "ACC-LEN-005",
            "reason": "Trying to release while account is still frozen",
        },
    )
    assert res.status_code == 409
    assert "FROZEN" in res.json()["detail"]
    assert get_account_balance_minor(session, "ACC-LEN-005") == 0

