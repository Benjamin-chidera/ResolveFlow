from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, select
from app.agents.graph import run_case_investigation
from app.core.database import get_session
from app.models.case import AuditEvent
from app.services.policy_engine import PolicyExecutionResult

router = APIRouter(prefix="/api/v1", tags=["Investigations & Audit"])


class AuditEventResponse(BaseModel):
    actor: str
    action: str
    policy_version: str
    before_state: Optional[str] = None
    after_state: Optional[str] = None
    details: Optional[str] = None
    timestamp: str


@router.post("/investigations/{case_id}/run", response_model=PolicyExecutionResult)
def trigger_case_investigation(
    case_id: str,
    session: Session = Depends(get_session),
) -> PolicyExecutionResult:
    """Runs the LangGraph investigation pipeline and Deterministic Policy Engine for a case."""
    try:
        return run_case_investigation(session=session, case_id=case_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get("/audit-events", response_model=List[AuditEventResponse])
def list_audit_events(
    actor: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> List[AuditEventResponse]:
    """Retrieves filtered immutable audit records ordered chronologically."""
    statement = select(AuditEvent).order_by(AuditEvent.timestamp)
    if actor:
        statement = statement.where(AuditEvent.actor == actor)
    if action:
        statement = statement.where(AuditEvent.action == action)

    events = session.exec(statement).all()
    return [
        AuditEventResponse(
            actor=evt.actor,
            action=evt.action,
            policy_version=evt.policy_version,
            before_state=evt.before_state,
            after_state=evt.after_state,
            details=evt.details,
            timestamp=evt.timestamp.isoformat(),
        )
        for evt in events
    ]

