from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlmodel import Session
from app.core.database import get_session
from app.services.approval_service import (
    ApprovalAuthorizationError,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalNotFoundError,
    ApprovalValidationError,
    PendingApprovalItem,
    StaleApprovalError,
    list_pending_approvals,
    process_approval_decision,
)

router = APIRouter(prefix="/api/v1/approvals", tags=["Human Review & Approvals"])


@router.get("/pending", response_model=List[PendingApprovalItem])
def get_pending_approvals(
    x_reviewer_role: Optional[str] = Header(default=None),
    reviewer_role: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> List[PendingApprovalItem]:
    """Lists all pending approval requests for authorized operations/compliance staff."""
    effective_role = x_reviewer_role or reviewer_role or ""
    try:
        return list_pending_approvals(session=session, reviewer_role=effective_role)
    except ApprovalAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc


@router.post("/{approval_id}/decision", response_model=ApprovalDecisionResponse)
def submit_approval_decision(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    session: Session = Depends(get_session),
) -> ApprovalDecisionResponse:
    """Executes an authorized operator approve/reject decision with stale-approval protection."""
    try:
        return process_approval_decision(
            session=session,
            approval_id=approval_id,
            payload=payload,
        )
    except ApprovalAuthorizationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except StaleApprovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ApprovalValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

