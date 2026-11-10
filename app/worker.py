import os
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from sqlmodel import Session, select

from app.agents.graph import build_investigation_workflow
from app.agents.state import InvestigationWorkflowState
from app.core.database import engine
from app.core.redis_client import (
    DEAD_LETTER_STREAM,
    INVESTIGATION_CONSUMER_GROUP,
    PRIMARY_INVESTIGATION_STREAM,
    ensure_consumer_group,
    get_redis_client,
)
from app.models.case import AuditEvent, InvestigationCase
from app.models.chat import CustomerEvidenceClaim
from app.models.enums import CaseState, EventVisibility
from app.services.case_event_helper import append_case_event
from app.services.case_service import transition_case_state
from app.services.outbox_relay import relay_pending_outbox_events
from app.services.policy_engine import evaluate_and_execute_policy

MAX_ATTEMPTS = 5
WORKER_LEASE_TTL_SECONDS = 30


class SimulatedWorkerCrashError(BaseException):
    """
    BaseException (not Exception) used in tests to simulate an abrupt process kill (SIGKILL)
    mid-investigation where the worker dies before calling XACK or retry handling.
    """


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_consumer_name() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


def _build_initial_workflow_state(case: InvestigationCase) -> InvestigationWorkflowState:
    return {
        "case_id": case.case_id,
        "customer_id": case.customer_id,
        "beneficiary_account_id": case.beneficiary_account_id,
        "claimed_reference": case.reference,
        "claimed_amount_minor": case.amount_minor,
        "currency": case.currency,
        "matched_payment_id": None,
        "provider_status": None,
        "allocation_status": None,
        "raw_reference": None,
        "sender_bank": case.sender_bank,
        "receipt_amount_minor": None,
        "funds_in_suspense": False,
        "trace_events": [],
        "exact_reference_match": False,
        "amount_matches": False,
        "candidate_account_ids": [],
        "account_status": None,
        "is_account_active": False,
        "prior_credit_detected": False,
        "reconciliation_flags": [],
        "investigation_summary": "",
        "suspected_root_cause": "",
        "proposed_action": None,
        "evidence_ids": [],
        "requires_human_approval": False,
        "policy_approved": False,
        "duplicate_noop": False,
        "audit_entries": [],
    }


def _normalize_dt_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def process_stream_job_entry(
    session: Session,
    redis_client: Any,
    consumer_name: str,
    entry_id: str,
    fields: Dict[str, str],
    mid_flight_hook: Optional[Callable[[Session, InvestigationCase], None]] = None,
    transient_error_fn: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """
    Executes a single Redis Stream investigation job idempotently with SQLite lease locking,
    mid-flight evidence version reconciliation, post-commit XACK, and bounded retry / DLQ routing.
    """
    case_id = fields.get("case_id", "")
    job_evidence_version = int(fields.get("evidence_version", "1"))
    attempt = int(fields.get("attempt", "1"))

    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()

    if case is None:
        redis_client.xack(
            PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP, entry_id
        )
        return {
            "status": "MISSING_CASE_ACKED",
            "case_id": case_id,
            "entry_id": entry_id,
        }

    # 1. Idempotency Guard: If already resolved or evidence_version already processed
    if case.state in (CaseState.RESOLVED, CaseState.CLOSED) or (
        case.processed_evidence_version >= job_evidence_version
        and case.latest_evidence_version <= case.processed_evidence_version
        and case.state
        in (
            CaseState.RESOLVED,
            CaseState.AWAITING_REVIEW,
            CaseState.WAITING_PROVIDER,
        )
    ):
        redis_client.xack(
            PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP, entry_id
        )
        return {
            "status": "DUPLICATE_NOOP",
            "case_id": case_id,
            "entry_id": entry_id,
            "processed_evidence_version": case.processed_evidence_version,
        }

    # 2. Check active worker lease to prevent concurrent execution on the same case
    now = get_utc_now()
    lease_exp = _normalize_dt_utc(case.lease_expires_at)
    if (
        case.active_worker_lease_id is not None
        and case.active_worker_lease_id != consumer_name
        and lease_exp is not None
        and lease_exp > now
    ):
        return {
            "status": "LEASE_LOCKED_BY_OTHER_WORKER",
            "case_id": case_id,
            "entry_id": entry_id,
            "holder": case.active_worker_lease_id,
        }

    # 3. Acquire case lease & transition state to INVESTIGATING
    case.active_worker_lease_id = consumer_name
    case.lease_expires_at = now + timedelta(seconds=WORKER_LEASE_TTL_SECONDS)
    if case.state in (
        CaseState.OPEN,
        CaseState.WAITING_PROVIDER,
        CaseState.AWAITING_REVIEW,
    ):
        transition_case_state(
            session=session,
            case=case,
            target_state=CaseState.INVESTIGATING,
            actor=consumer_name,
            details=f"Worker {consumer_name} acquired lease for evidence v{job_evidence_version}",
        )
    session.add(case)
    session.commit()

    try:
        if transient_error_fn is not None:
            transient_error_fn()

        reconciled_version = case.latest_evidence_version
        graph = build_investigation_workflow(session=session)
        config = {"configurable": {"thread_id": case.case_id}}
        final_state: InvestigationWorkflowState = graph.invoke(
            _build_initial_workflow_state(case), config=config
        )

        # Optional mid-flight hook (e.g. customer sends updated reference while worker runs)
        if mid_flight_hook is not None:
            mid_flight_hook(session, case)

        # Check if customer submitted newer evidence mid-flight
        session.refresh(case)
        if case.latest_evidence_version > reconciled_version:
            latest_claim = session.exec(
                select(CustomerEvidenceClaim).where(
                    CustomerEvidenceClaim.case_id == case.case_id,
                    CustomerEvidenceClaim.evidence_version
                    == case.latest_evidence_version,
                )
            ).first()
            if latest_claim is not None:
                if latest_claim.claimed_reference:
                    case.reference = latest_claim.claimed_reference
                if latest_claim.claimed_amount_minor is not None:
                    case.amount_minor = latest_claim.claimed_amount_minor
                if latest_claim.claimed_sender_bank:
                    case.sender_bank = latest_claim.claimed_sender_bank
                session.add(case)
                session.flush()

            reconciled_version = case.latest_evidence_version
            final_state = graph.invoke(
                _build_initial_workflow_state(case), config=config
            )

        policy_result = evaluate_and_execute_policy(
            session=session,
            case=case,
            workflow_state=final_state,
        )

        # 4. Acknowledge stream message ONLY after SQLite transaction commits
        redis_client.xack(
            PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP, entry_id
        )

        return {
            "status": "COMPLETED",
            "case_id": case.case_id,
            "entry_id": entry_id,
            "reconciled_evidence_version": reconciled_version,
            "execution_status": policy_result.execution_status,
            "proposed_action": policy_result.proposed_action,
            "case_state": policy_result.case_state,
        }

    except Exception as exc:
        session.rollback()
        refreshed_case = session.exec(
            select(InvestigationCase).where(InvestigationCase.case_id == case_id)
        ).first()
        if refreshed_case is not None:
            refreshed_case.active_worker_lease_id = None
            refreshed_case.lease_expires_at = None
            session.add(refreshed_case)
            session.commit()

        backoff_seconds = min(30, 2 ** (attempt - 1))

        if attempt < MAX_ATTEMPTS:
            retry_fields = dict(fields)
            retry_fields["attempt"] = str(attempt + 1)
            retry_fields["backoff_seconds"] = str(backoff_seconds)
            retry_fields["last_error"] = str(exc)
            retry_entry_id = redis_client.xadd(
                PRIMARY_INVESTIGATION_STREAM, retry_fields
            )
            redis_client.xack(
                PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP, entry_id
            )
            return {
                "status": "RETRIED",
                "case_id": case_id,
                "failed_entry_id": entry_id,
                "retry_entry_id": retry_entry_id,
                "attempt": attempt + 1,
                "backoff_seconds": backoff_seconds,
            }

        # Exhausted MAX_ATTEMPTS -> Route to Dead-Letter Stream (resolveflow:investigations:dlq)
        dlq_fields = dict(fields)
        dlq_fields["exhausted_attempts"] = str(attempt)
        dlq_fields["failure_reason"] = str(exc)
        dlq_fields["failed_at"] = get_utc_now().isoformat()
        dlq_entry_id = redis_client.xadd(DEAD_LETTER_STREAM, dlq_fields)

        append_case_event(
            session=session,
            case_id=case_id,
            event_type="WORKER_JOB_DLQ",
            visibility=EventVisibility.INTERNAL_ONLY,
            payload={
                "case_id": case_id,
                "dlq_entry_id": str(dlq_entry_id),
                "attempts": attempt,
                "failure_reason": str(exc),
            },
        )
        session.add(
            AuditEvent(
                actor=consumer_name,
                action="JOB_MOVED_TO_DLQ",
                before_state="INVESTIGATING",
                after_state="DLQ_EXHAUSTED",
                details=(
                    f"[{case_id}] Investigation job exhausted {attempt} attempts "
                    f"and moved to {DEAD_LETTER_STREAM} ({dlq_entry_id}): {exc}"
                ),
                timestamp=get_utc_now(),
            )
        )
        session.commit()

        redis_client.xack(
            PRIMARY_INVESTIGATION_STREAM, INVESTIGATION_CONSUMER_GROUP, entry_id
        )
        return {
            "status": "MOVED_TO_DLQ",
            "case_id": case_id,
            "failed_entry_id": entry_id,
            "dlq_entry_id": str(dlq_entry_id),
            "attempts": attempt,
        }


def reclaim_stalled_jobs(
    session: Session,
    redis_client: Any,
    consumer_name: str,
    min_idle_ms: int = 30000,
    count: int = 10,
) -> List[Dict[str, Any]]:
    """
    Uses Redis XAUTOCLAIM to recover pending jobs from workers that crashed mid-execution
    and safely completes them.
    """
    ensure_consumer_group(redis_client)
    _, claimed_entries, _ = redis_client.xautoclaim(
        name=PRIMARY_INVESTIGATION_STREAM,
        groupname=INVESTIGATION_CONSUMER_GROUP,
        consumername=consumer_name,
        min_idle_time=min_idle_ms,
        start_id="0-0",
        count=count,
    )

    results: List[Dict[str, Any]] = []
    for entry_id, fields in claimed_entries:
        case_id = fields.get("case_id", "")
        case = session.exec(
            select(InvestigationCase).where(InvestigationCase.case_id == case_id)
        ).first()
        if case is not None:
            # Force-release the crashed worker's stale lease before re-running
            case.active_worker_lease_id = None
            case.lease_expires_at = None
            session.add(case)
            session.commit()

        res = process_stream_job_entry(
            session=session,
            redis_client=redis_client,
            consumer_name=consumer_name,
            entry_id=entry_id,
            fields=fields,
        )
        results.append(res)

    return results


def consume_once(
    session: Session,
    redis_client: Any,
    consumer_name: Optional[str] = None,
    count: int = 10,
    block_ms: Optional[int] = 10,
    mid_flight_hook: Optional[Callable[[Session, InvestigationCase], None]] = None,
    transient_error_fn: Optional[Callable[[], None]] = None,
) -> List[Dict[str, Any]]:
    """
    Relays any PENDING outbox rows to Redis Streams, then reads and processes up to
    `count` messages via XREADGROUP.
    """
    active_consumer = consumer_name or default_consumer_name()
    ensure_consumer_group(redis_client)
    relay_pending_outbox_events(session=session, redis_client=redis_client)

    stream_batches = redis_client.xreadgroup(
        groupname=INVESTIGATION_CONSUMER_GROUP,
        consumername=active_consumer,
        streams={PRIMARY_INVESTIGATION_STREAM: ">"},
        count=count,
        block=block_ms,
    )

    processed: List[Dict[str, Any]] = []
    for _, entries in stream_batches:
        for entry_id, fields in entries:
            res = process_stream_job_entry(
                session=session,
                redis_client=redis_client,
                consumer_name=active_consumer,
                entry_id=entry_id,
                fields=fields,
                mid_flight_hook=mid_flight_hook,
                transient_error_fn=transient_error_fn,
            )
            processed.append(res)

    return processed


def run_worker_loop() -> None:
    """Entrypoint for the standalone `worker` service (`uv run python -m app.worker`)."""
    redis_client = get_redis_client()
    ensure_consumer_group(redis_client)
    consumer_name = default_consumer_name()
    last_autoclaim = 0.0

    print(
        f"[ResolveFlow Worker] Started {consumer_name} listening on {PRIMARY_INVESTIGATION_STREAM}..."
    )
    while True:
        with Session(engine) as session:
            now_ts = time.time()
            if now_ts - last_autoclaim >= 10.0:
                reclaim_stalled_jobs(
                    session=session,
                    redis_client=redis_client,
                    consumer_name=consumer_name,
                    min_idle_ms=30000,
                )
                last_autoclaim = now_ts

            consume_once(
                session=session,
                redis_client=redis_client,
                consumer_name=consumer_name,
                count=5,
                block_ms=1000,
            )


if __name__ == "__main__":
    run_worker_loop()

