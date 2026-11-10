import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from sqlmodel import Session, select

from app.core.redis_client import ensure_consumer_group
from app.models.chat import OutboxEvent
from app.models.enums import OutboxStatus


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def relay_pending_outbox_events(
    session: Session,
    redis_client: Any,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Transactional Outbox Relay (Spec 08 Section 6.2).
    Polls PENDING outbox_events in SQLite, publishes each job to Redis Streams
    via XADD, and marks the outbox row PUBLISHED with its redis_message_id.
    """
    ensure_consumer_group(redis_client)

    pending_rows = session.exec(
        select(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PENDING)
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
    ).all()

    published_results: List[Dict[str, Any]] = []

    for outbox in pending_rows:
        payload = json.loads(outbox.payload_json)
        fields = {
            "outbox_id": outbox.outbox_id,
            "idempotency_key": outbox.idempotency_key,
            "case_id": str(payload["case_id"]),
            "session_id": str(payload.get("session_id") or ""),
            "evidence_version": str(payload["evidence_version"]),
            "attempt": str(payload.get("attempt", 1)),
        }

        redis_msg_id = redis_client.xadd(outbox.stream_name, fields)

        outbox.status = OutboxStatus.PUBLISHED
        outbox.redis_message_id = str(redis_msg_id)
        outbox.published_at = get_utc_now()
        session.add(outbox)
        session.commit()

        published_results.append(
            {
                "outbox_id": outbox.outbox_id,
                "case_id": payload["case_id"],
                "evidence_version": payload["evidence_version"],
                "redis_message_id": str(redis_msg_id),
            }
        )

    return published_results

