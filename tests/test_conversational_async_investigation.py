import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.database import get_session
from app.core.redis_client import (
    DEAD_LETTER_STREAM,
    INVESTIGATION_CONSUMER_GROUP,
    PRIMARY_INVESTIGATION_STREAM,
    InMemoryRedisStreamAdapter,
    ensure_consumer_group,
)
from app.models.case import ApprovalRequest, AuditEvent, InvestigationCase
from app.models.chat import CaseEvent, CustomerEvidenceClaim, OutboxEvent
from app.models.enums import CaseState, EventVisibility, OutboxStatus
from app.models.ledger import JournalEntry, LedgerEntry
from app.services.chat_service import handle_customer_message
from app.services.evaluation_service import seed_evaluation_fixtures
from app.services.ledger_service import get_account_balance_minor
from app.services.outbox_relay import relay_pending_outbox_events
from app.worker import (
    SimulatedWorkerCrashError,
    consume_once,
    process_stream_job_entry,
    reclaim_stalled_jobs,
)
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
        # Clear pre-seeded S1 case so conversational intake can open its own case cleanly
        s1_case = session.exec(
            select(InvestigationCase).where(InvestigationCase.case_id == "CASE-S1-001")
        ).first()
        if s1_case:
            session.delete(s1_case)
            session.commit()
        yield session


@pytest.fixture(name="redis_stream")
def redis_stream_fixture():
    adapter = InMemoryRedisStreamAdapter()
    ensure_consumer_group(adapter)
    return adapter


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_session_override():
        return session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _parse_sse_body(raw_sse_text: str):
    """Parses raw Server-Sent Events stream text into a list of dicts."""
    events = []
    blocks = [b.strip() for b in raw_sse_text.strip().split("\n\n") if b.strip()]
    for block in blocks:
        item = {}
        for line in block.splitlines():
            if line.startswith("id:"):
                item["id"] = int(line.split("id:", 1)[1].strip())
            elif line.startswith("event:"):
                item["event"] = line.split("event:", 1)[1].strip()
            elif line.startswith("data:"):
                item["data"] = json.loads(line.split("data:", 1)[1].strip())
        events.append(item)
    return events


def test_01_targeted_conversational_intake_without_repetitive_questions(
    client: TestClient, session: Session
):
    """
    Acceptance Test 1: Agent uses existing account context (ACC-LEN-001) without asking
    repetitive questions and extracts amount_minor, sender_bank, and reference across turns.
    """
    sess_res = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
        json={},
    )
    assert sess_res.status_code == 201
    session_data = sess_res.json()
    session_id = session_data["session_id"]
    assert session_data["beneficiary_account_id"] == "ACC-LEN-001"
    assert session_data["currency"] == "GBP"

    # Turn 1: Customer supplies amount (£500.00) and sender bank (Monzo), but omits reference
    turn1 = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "Hi, I sent £500.00 from my Monzo account today but it hasn't arrived."},
    )
    assert turn1.status_code == 200
    t1_body = turn1.json()
    assert t1_body["active_case_id"] is None
    assert t1_body["missing_slots"] == ["payment_reference"]
    assert "reference" in t1_body["agent_reply"].lower()
    # Agent already knows £500.00 and Monzo, so it only asks for the payment reference
    assert "transfer amount" not in t1_body["agent_reply"].lower()

    # Turn 2: Customer supplies the missing reference MON-78291
    turn2 = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "Sure, the reference is MON-78291."},
    )
    assert turn2.status_code == 200
    t2_body = turn2.json()
    assert t2_body["active_case_id"] is not None
    assert t2_body["evidence_version"] == 1
    assert t2_body["outbox_id"] is not None
    assert t2_body["missing_slots"] == []

    # Verify CustomerEvidenceClaim v1 and PENDING OutboxEvent were atomically committed
    claim = session.exec(
        select(CustomerEvidenceClaim).where(
            CustomerEvidenceClaim.case_id == t2_body["active_case_id"],
            CustomerEvidenceClaim.evidence_version == 1,
        )
    ).first()
    assert claim is not None
    assert claim.claimed_amount_minor == 50000
    assert claim.claimed_sender_bank == "Monzo"
    assert claim.claimed_reference == "MON-78291"


def test_02_concurrent_chat_while_investigation_running_does_not_cancel_work(
    client: TestClient, session: Session
):
    """
    Acceptance Test 2: Customer sends follow-up messages while worker is actively running;
    active investigation is neither cancelled nor restarted from scratch.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]

    msg1 = client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I transferred £500 from Monzo with reference MON-78291"},
    ).json()
    case_id = msg1["active_case_id"]

    # Simulate worker actively holding lease in INVESTIGATING state
    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()
    case.state = CaseState.INVESTIGATING
    case.active_worker_lease_id = "worker-active-1"
    session.add(case)
    session.commit()

    # Customer messages the agent while investigation is running
    follow_up = client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "Are you still checking on this transfer?"},
    )
    assert follow_up.status_code == 200
    f_body = follow_up.json()
    assert f_body["active_case_id"] == case_id
    assert f_body["case_state"] == "INVESTIGATING"
    assert f_body["outbox_id"] is None  # No duplicate job queued

    session.refresh(case)
    assert case.state == CaseState.INVESTIGATING
    assert case.active_worker_lease_id == "worker-active-1"
    assert case.latest_evidence_version == 1


def test_03_versioned_evidence_mid_flight_incorporation(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 3: Customer updates reference (v1 -> v2) while investigation is running;
    worker safely reconciles v2 before final resolution and allocates the payment.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]

    # Customer initially gives typo reference MON-00000 (v1)
    msg_v1 = client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-00000"},
    ).json()
    case_id = msg_v1["active_case_id"]
    assert msg_v1["evidence_version"] == 1

    # Define a mid-flight hook where the customer sends corrected reference MON-78291 (v2)
    def customer_sends_v2_mid_flight(db_sess: Session, active_case: InvestigationCase):
        handle_customer_message(
            session=db_sess,
            session_id=sess_id,
            customer_id="CUST-101",
            content="Sorry, the reference is actually MON-78291!",
        )

    results = consume_once(
        session=session,
        redis_client=redis_stream,
        consumer_name="worker-midflight",
        mid_flight_hook=customer_sends_v2_mid_flight,
    )
    assert len(results) == 1
    assert results[0]["status"] == "COMPLETED"
    assert results[0]["reconciled_evidence_version"] == 2
    assert results[0]["execution_status"] == "EXECUTED"
    assert results[0]["case_state"] == "RESOLVED"
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_04_transactional_outbox_durability_across_redis_outage(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 4: Simulated failure between SQLite commit and Redis XADD preserves
    the PENDING outbox event and relays it cleanly on recovery.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]

    msg = client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-78291"},
    ).json()
    outbox_id = msg["outbox_id"]

    outbox_row = session.exec(
        select(OutboxEvent).where(OutboxEvent.outbox_id == outbox_id)
    ).first()
    assert outbox_row is not None
    assert outbox_row.status == OutboxStatus.PENDING

    # Simulate broken Redis during first relay attempt
    class BrokenRedis:
        def xgroup_create(self, *args, **kwargs):
            return True

        def xadd(self, *args, **kwargs):
            raise ConnectionError("Simulated Redis connection drop")

    with pytest.raises(ConnectionError):
        relay_pending_outbox_events(session, BrokenRedis())

    # Outbox event must still be PENDING in SQLite (not lost!)
    session.refresh(outbox_row)
    assert outbox_row.status == OutboxStatus.PENDING

    # Now Redis recovers -> relay succeeds and worker processes the job
    relayed = relay_pending_outbox_events(session, redis_stream)
    assert len(relayed) == 1
    session.refresh(outbox_row)
    assert outbox_row.status == OutboxStatus.PUBLISHED
    assert outbox_row.redis_message_id is not None

    worker_res = consume_once(session, redis_stream, consumer_name="worker-recovery")
    assert len(worker_res) == 1
    assert worker_res[0]["execution_status"] == "EXECUTED"
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_05_worker_crash_and_xautoclaim_recovery(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 5: Simulated worker crash mid-investigation leaves message in Redis PEL;
    secondary worker reclaims it via XAUTOCLAIM and completes the case.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]
    client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-78291"},
    )

    relay_pending_outbox_events(session, redis_stream)

    # Worker 1 reads from consumer group and crashes abruptly before XACK
    batches = redis_stream.xreadgroup(
        groupname=INVESTIGATION_CONSUMER_GROUP,
        consumername="worker-crashed-1",
        streams={PRIMARY_INVESTIGATION_STREAM: ">"},
        count=1,
    )
    entry_id, fields = batches[0][1][0]

    def simulate_sigkill():
        raise SimulatedWorkerCrashError("Worker process killed abruptly")

    with pytest.raises(SimulatedWorkerCrashError):
        process_stream_job_entry(
            session=session,
            redis_client=redis_stream,
            consumer_name="worker-crashed-1",
            entry_id=entry_id,
            fields=fields,
            transient_error_fn=simulate_sigkill,
        )

    # Message is still in Redis PEL (Pending Entries List)
    assert (
        redis_stream.xpending(PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP)[
            "pending"
        ]
        == 1
    )

    # Age the pending message past the 30s idle threshold so worker-2 can XAUTOCLAIM it
    redis_stream.force_age_pending_message(
        PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP, entry_id, age_ms=35000
    )

    reclaimed = reclaim_stalled_jobs(
        session=session,
        redis_client=redis_stream,
        consumer_name="worker-healthy-2",
        min_idle_ms=30000,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0]["status"] == "COMPLETED"
    assert reclaimed[0]["execution_status"] == "EXECUTED"

    # PEL is now empty (acknowledged) and customer is credited exactly once
    assert (
        redis_stream.xpending(PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP)[
            "pending"
        ]
        == 0
    )
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_06_duplicate_stream_delivery_idempotency(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 6: Delivering the exact same Redis Stream payload 5 times results
    in at most 1 investigation execution and zero duplicate ledger credits.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]
    msg = client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-78291"},
    ).json()
    case_id = msg["active_case_id"]

    # Push 5 identical job deliveries to Redis Stream
    for _ in range(5):
        redis_stream.xadd(
            PRIMARY_INVESTIGATION_STREAM,
            {
                "outbox_id": msg["outbox_id"],
                "case_id": case_id,
                "session_id": sess_id,
                "evidence_version": "1",
                "attempt": "1",
            },
        )

    results = consume_once(
        session=session,
        redis_client=redis_stream,
        consumer_name="worker-dedup",
        count=10,
    )
    completed = [r for r in results if r["status"] == "COMPLETED"]
    duplicates = [r for r in results if r["status"] == "DUPLICATE_NOOP"]

    assert len(completed) == 1
    assert len(duplicates) >= 4

    credits = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.account_id == "ACC-LEN-001",
            LedgerEntry.credit_minor > 0,
        )
    ).all()
    assert len(credits) == 1
    assert get_account_balance_minor(session, "ACC-LEN-001") == 50000


def test_07_exhausted_retries_and_dead_letter_stream_dlq(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 7: A job that repeatedly fails transient execution exhausts
    MAX_ATTEMPTS (5), moves to resolveflow:investigations:dlq, and is XACK-ed from main stream.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]
    msg = client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-78291"},
    ).json()
    case_id = msg["active_case_id"]

    def always_fail_transient():
        raise RuntimeError("Simulated transient downstream failure")

    statuses = []
    for _ in range(5):
        batch_res = consume_once(
            session=session,
            redis_client=redis_stream,
            consumer_name="worker-dlq-test",
            count=1,
            transient_error_fn=always_fail_transient,
        )
        assert len(batch_res) == 1
        statuses.append(batch_res[0]["status"])

    assert statuses == ["RETRIED", "RETRIED", "RETRIED", "RETRIED", "MOVED_TO_DLQ"]

    # Primary stream PEL is 0 (poison pill was XACK-ed)
    assert (
        redis_stream.xpending(PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP)[
            "pending"
        ]
        == 0
    )

    # Dead-Letter Stream has the exhausted message
    dlq_entries = redis_stream.xrange(DEAD_LETTER_STREAM)
    assert len(dlq_entries) == 1
    _, dlq_fields = dlq_entries[0]
    assert dlq_fields["case_id"] == case_id
    assert dlq_fields["exhausted_attempts"] == "5"

    # Audit log recorded JOB_MOVED_TO_DLQ
    dlq_audit = session.exec(
        select(AuditEvent).where(AuditEvent.action == "JOB_MOVED_TO_DLQ")
    ).first()
    assert dlq_audit is not None


def test_08_sse_disconnect_and_missed_event_replay(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 8: Client disconnects at sequence_id = 1, worker emits subsequent
    events, client reconnects with Last-Event-ID: 1 and receives only missed events in order.
    """
    sess_id = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]

    # Turn 1 opens case with typo reference (creates sequence_id 1: CASE_OPENED)
    client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-00000"},
    )

    initial_sse = client.get(
        f"/api/v1/chat/sessions/{sess_id}/events",
        headers={"X-Customer-ID": "CUST-101"},
    )
    assert initial_sse.status_code == 200
    initial_events = _parse_sse_body(initial_sse.text)
    assert len(initial_events) == 1
    first_seq_id = initial_events[0]["id"]
    assert initial_events[0]["event"] == "CASE_OPENED"

    # Client disconnects; customer sends corrected reference (EVIDENCE_UPDATED) and worker resolves (CASE_RESOLVED)
    client.post(
        f"/api/v1/chat/sessions/{sess_id}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "Correction: reference is MON-78291"},
    )
    consume_once(session, redis_stream, consumer_name="worker-sse")

    # Client reconnects passing Last-Event-ID: <first_seq_id>
    replay_sse = client.get(
        f"/api/v1/chat/sessions/{sess_id}/events",
        headers={
            "X-Customer-ID": "CUST-101",
            "Last-Event-ID": str(first_seq_id),
        },
    )
    assert replay_sse.status_code == 200
    replayed_events = _parse_sse_body(replay_sse.text)
    assert len(replayed_events) == 2
    assert replayed_events[0]["id"] > first_seq_id
    assert replayed_events[0]["event"] == "EVIDENCE_UPDATED"
    assert replayed_events[1]["event"] == "CASE_RESOLVED"
    assert replayed_events[1]["id"] > replayed_events[0]["id"]
    assert "£500.00" in replayed_events[1]["data"]["message"]


def test_09_tenant_and_customer_isolation_and_privacy_guardrails(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 9: Customer CUST-102 attempting to read or stream CUST-101's session
    receives 403 Forbidden; zero internal risk/freeze flags leak in customer SSE events.
    """
    # 1. Cross-customer access check
    sess_101 = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]

    forbidden_msg = client.post(
        f"/api/v1/chat/sessions/{sess_101}/messages",
        headers={"X-Customer-ID": "CUST-102"},
        json={"content": "Let me see CUST-101's transfer"},
    )
    assert forbidden_msg.status_code == 403

    forbidden_sse = client.get(
        f"/api/v1/chat/sessions/{sess_101}/events",
        headers={"X-Customer-ID": "CUST-102"},
    )
    assert forbidden_sse.status_code == 403

    # 2. Privacy guardrail on FROZEN account (CUST-105 / ACC-LEN-005 / Scenario S5)
    # Remove pre-seeded CASE-S5-005 so chat creates and links the S5 case
    s5_case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == "CASE-S5-005")
    ).first()
    if s5_case:
        session.delete(s5_case)
        session.commit()

    sess_105 = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-105"},
    ).json()["session_id"]

    client.post(
        f"/api/v1/chat/sessions/{sess_105}/messages",
        headers={"X-Customer-ID": "CUST-105"},
        json={"content": "I sent £750 from Monzo with reference MON-55441"},
    )
    consume_once(session, redis_stream, consumer_name="worker-privacy")

    sse_105 = client.get(
        f"/api/v1/chat/sessions/{sess_105}/events",
        headers={"X-Customer-ID": "CUST-105"},
    )
    assert sse_105.status_code == 200
    raw_stream = sse_105.text
    assert "FROZEN" not in raw_stream.upper()
    assert "ACCOUNT_RESTRICTED" not in raw_stream.upper()
    assert "INTERNAL_ESCALATION_CREATED" not in raw_stream
    assert "standard review" in raw_stream.lower()


def test_10_stale_approval_rejected_when_chat_evidence_advances(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 10: If new conflicting evidence arrives in chat while an ApprovalRequest
    is pending, POST /api/v1/approvals/{id}/decision rejects the stale approval (409 Conflict).
    """
    # Remove pre-seeded CASE-S3-003 so chat links the S3 case
    s3_case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == "CASE-S3-003")
    ).first()
    if s3_case:
        session.delete(s3_case)
        session.commit()

    sess_103 = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-103"},
    ).json()["session_id"]

    msg = client.post(
        f"/api/v1/chat/sessions/{sess_103}/messages",
        headers={"X-Customer-ID": "CUST-103"},
        json={"content": "I sent £1200 from Monzo with reference MON-33112"},
    ).json()
    case_id = msg["active_case_id"]

    # Worker runs and escalates ambiguous S3 case to AWAITING_REVIEW (evidence_version=1)
    consume_once(session, redis_stream, consumer_name="worker-s3")

    approval = session.exec(
        select(ApprovalRequest).where(ApprovalRequest.case_id == case_id)
    ).first()
    assert approval is not None
    assert approval.evidence_version == 1

    # Customer sends new conflicting reference in chat -> advances case evidence_version to 2
    client.post(
        f"/api/v1/chat/sessions/{sess_103}/messages",
        headers={"X-Customer-ID": "CUST-103"},
        json={"content": "Wait, the reference was actually MON-99999 for £1250"},
    )

    # Operator attempts to approve the stale v1 approval request -> 409 Conflict
    decision_res = client.post(
        f"/api/v1/approvals/{approval.approval_id}/decision",
        json={
            "reviewer_id": "ops_sarah",
            "reviewer_role": "PAYMENT_OPERATIONS",
            "decision": "APPROVED",
            "selected_account_id": "ACC-LEN-003",
            "reason": "Attempting to approve v1 after v2 evidence arrived",
        },
    )
    assert decision_res.status_code == 409
    assert "evidence version advanced" in decision_res.json()["detail"].lower()
    assert get_account_balance_minor(session, "ACC-LEN-003") == 0


def test_11_post_commit_resolution_notification_atomic_with_ledger(
    client: TestClient, session: Session, redis_stream: InMemoryRedisStreamAdapter
):
    """
    Acceptance Test 11: Verified credit notifications (CASE_RESOLVED) appear in case_events
    if and only if the underlying double-entry JournalEntry (ALLOC-{payment_id}) committed.
    """
    # 1. Unresolved case (S2 - missing receipt) must have ZERO CASE_RESOLVED events
    sess_102 = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-102"},
    ).json()["session_id"]
    client.post(
        f"/api/v1/chat/sessions/{sess_102}/messages",
        headers={"X-Customer-ID": "CUST-102"},
        json={"content": "Where is my £250 transfer from Monzo with reference MON-99881?"},
    )
    consume_once(session, redis_stream, consumer_name="worker-s2")

    s2_resolved_events = session.exec(
        select(CaseEvent).where(
            CaseEvent.session_id == sess_102,
            CaseEvent.event_type == "CASE_RESOLVED",
        )
    ).all()
    assert len(s2_resolved_events) == 0

    # 2. Resolved case (S1 - £500 settled in suspense) commits JournalEntry and CASE_RESOLVED together
    sess_101 = client.post(
        "/api/v1/chat/sessions",
        headers={"X-Customer-ID": "CUST-101"},
    ).json()["session_id"]
    msg_s1 = client.post(
        f"/api/v1/chat/sessions/{sess_101}/messages",
        headers={"X-Customer-ID": "CUST-101"},
        json={"content": "I sent £500 from Monzo with reference MON-78291"},
    ).json()
    s1_case_id = msg_s1["active_case_id"]

    consume_once(session, redis_stream, consumer_name="worker-s1")

    s1_resolved_event = session.exec(
        select(CaseEvent).where(
            CaseEvent.case_id == s1_case_id,
            CaseEvent.event_type == "CASE_RESOLVED",
            CaseEvent.visibility == EventVisibility.CUSTOMER_SAFE,
        )
    ).first()
    assert s1_resolved_event is not None

    event_payload = json.loads(s1_resolved_event.payload_json)
    committed_journal = session.exec(
        select(JournalEntry).where(
            JournalEntry.journal_id == event_payload["journal_id"]
        )
    ).first()
    assert committed_journal is not None
    assert committed_journal.idempotency_key == "ALLOC-PAY-S1-001"

