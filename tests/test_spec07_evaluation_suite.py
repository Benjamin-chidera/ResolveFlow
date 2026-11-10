import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.database import get_session
from app.models.account import CustomerAccount
from app.models.ledger import JournalEntry, LedgerEntry
from app.services.evaluation_service import (
    run_evaluation_suite,
    seed_evaluation_fixtures,
    verify_all_journals_balanced,
)
from app.services.ingestion_service import ProviderEventPayload, ingest_provider_event
from app.services.ledger_service import get_account_balance_minor
from main import app


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        seed_evaluation_fixtures(session)
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_invariant1_zero_duplicate_credits_on_replay(
    client: TestClient, session: Session
):
    """
    Invariant 1 (Spec 07): Replay identical webhook and investigation 10 times;
    verify that at most 1 credit LedgerEntry exists for PAY-S1-001 on ACC-LEN-001.
    """
    for _ in range(10):
        ingest_provider_event(
            session=session,
            payload=ProviderEventPayload(
                event_id="EVT-S1-001",
                source="FASTER_PAYMENTS",
                event_type="FUNDS_SETTLED",
                provider_reference="FP-2026-X881",
                sender_bank="Monzo",
                raw_reference="MON-78291 ACC-LEN-001",
                amount_minor=50000,
                currency="GBP",
            ),
        )
        res = client.post("/api/v1/investigations/CASE-S1-001/run")
        assert res.status_code == 200

    credits = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.source_payment_id == "PAY-S1-001",
            LedgerEntry.account_id == "ACC-LEN-001",
            LedgerEntry.credit_minor > 0,
        )
    ).all()
    assert len(credits) == 1, "Duplicate credit detected!"
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_invariant2_all_ledger_journals_balance(client: TestClient, session: Session):
    """
    Invariant 2 (Spec 07): Every double-entry JournalEntry across all seeded and
    executed transactions must have sum(debit_minor) == sum(credit_minor).
    """
    client.post("/api/v1/investigations/CASE-S1-001/run")
    client.post("/api/v1/investigations/CASE-S3-003/run")
    client.post("/api/v1/investigations/CASE-S4-004/run")

    journals = session.exec(select(JournalEntry)).all()
    assert len(journals) >= 5

    for jrn in journals:
        total_debits = sum(leg.debit_minor for leg in jrn.lines)
        total_credits = sum(leg.credit_minor for leg in jrn.lines)
        assert total_debits == total_credits, (
            f"Journal {jrn.journal_id} is out of balance!"
        )

    balance_report = verify_all_journals_balanced(session)
    assert balance_report["passed"] is True
    assert balance_report["compliance_percentage"] == 100.0


def test_invariant3_scenario_s2_strictly_blocks_credit(
    client: TestClient, session: Session
):
    """
    Invariant 3 (Spec 07): Zero allocations when authoritative receipt is missing.
    """
    res = client.post("/api/v1/investigations/CASE-S2-002/run")
    assert res.status_code == 200
    body = res.json()
    assert body["proposed_action"] == "TRACE_WITH_PROVIDER"
    assert body["execution_status"] == "NOT_EXECUTED"
    assert get_account_balance_minor(session, "ACC-LEN-002") == 0


def test_invariant4_scenario_s5_strictly_blocks_frozen_account(
    client: TestClient, session: Session
):
    """
    Invariant 4 (Spec 07): Zero allocations on FROZEN accounts.
    """
    res = client.post("/api/v1/investigations/CASE-S5-005/run")
    assert res.status_code == 200
    body = res.json()
    assert body["proposed_action"] == "HUMAN_REVIEW"
    assert body["execution_status"] == "AWAITING_HUMAN_APPROVAL"

    acc = session.exec(
        select(CustomerAccount).where(CustomerAccount.account_id == "ACC-LEN-005")
    ).first()
    assert acc is not None
    assert acc.status.value == "FROZEN"
    assert get_account_balance_minor(session, "ACC-LEN-005") == 0


def test_full_resolveflow_evaluation_scorecard(session: Session):
    """
    Executes the complete 6-scenario ResolveFlow evaluation suite (S1-S6)
    and verifies a 6/6 (100%) pass rate and 100% ledger invariant compliance.
    """
    report = run_evaluation_suite(session)
    print("\n" + report["scorecard"])

    assert report["passed_scenarios"] == 6
    assert report["total_scenarios"] == 6
    assert report["duplicate_credits_detected"] == 0
    assert report["duplicate_credit_rate_pct"] == 0.0
    assert report["ledger_balanced"] is True
    assert report["ledger_compliance_pct"] == 100.0
    assert report["unauthorised_credits"] == 0
    assert report["p95_latency_seconds"] < 1.8
    assert report["human_review_interrupt_success_pct"] == 100.0
    assert "Scenario Pass Rate:              6/6 (100%)" in report["scorecard"]
