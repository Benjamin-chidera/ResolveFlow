import json
from typing import Any, Dict, Optional
from sqlmodel import Session, select

from app.models.chat import CaseEvent, ChatSession
from app.models.enums import EventVisibility


def find_session_id_for_case(session: Session, case_id: str) -> Optional[str]:
    """Finds the chat session_id associated with an investigation case, if any."""
    chat_session = session.exec(
        select(ChatSession).where(ChatSession.active_case_id == case_id)
    ).first()
    return chat_session.session_id if chat_session else None


def append_case_event(
    session: Session,
    case_id: str,
    event_type: str,
    visibility: EventVisibility,
    payload: Dict[str, Any],
    session_id: Optional[str] = None,
) -> CaseEvent:
    """
    Stages a CaseEvent in the current SQLite transaction without committing,
    so callers can atomically commit ledger postings and SSE events together.
    """
    resolved_session_id = session_id or find_session_id_for_case(session, case_id)
    event = CaseEvent(
        case_id=case_id,
        session_id=resolved_session_id,
        visibility=visibility,
        event_type=event_type,
        payload_json=json.dumps(payload),
    )
    session.add(event)
    session.flush()
    return event

