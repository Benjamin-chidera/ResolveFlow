from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlmodel import Session, select
from app.core.database import get_session
from app.models.case import InvestigationCase
from app.services.case_service import (
    CaseDetailResponse,
    CaseSummaryResponse,
    CaseValidationError,
    CreateCaseRequest,
    ProactiveReconciliationResponse,
    create_complaint_case,
    format_case_detail,
    run_proactive_reconciliation_scan,
)

router = APIRouter(prefix="/api/v1", tags=["Cases & Reconciliation"])


@router.post(
    "/cases",
    response_model=CaseSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_case(
    payload: CreateCaseRequest,
    response: Response,
    session: Session = Depends(get_session),
) -> CaseSummaryResponse:
    """Creates a missing incoming-payment complaint case (Entry A)."""
    try:
        case_record, created_new = create_complaint_case(session=session, payload=payload)
    except CaseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    if not created_new:
        response.status_code = status.HTTP_409_CONFLICT
    else:
        response.status_code = status.HTTP_201_CREATED

    return CaseSummaryResponse(
        case_id=case_record.case_id,
        customer_id=case_record.customer_id,
        beneficiary_account_id=case_record.beneficiary_account_id,
        sender_bank=case_record.sender_bank,
        reference=case_record.reference,
        amount_minor=case_record.amount_minor,
        currency=case_record.currency,
        state=case_record.state.value,
        created_at=case_record.created_at.isoformat(),
    )


@router.get("/cases/{case_id}", response_model=CaseDetailResponse)
def get_case(
    case_id: str,
    session: Session = Depends(get_session),
) -> CaseDetailResponse:
    """Retrieves full case state, investigation findings, evidence references, and proposed action."""
    case_record = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()

    if case_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Case '{case_id}' not found.",
        )

    return format_case_detail(case_record)


@router.post("/reconciliation/run", response_model=ProactiveReconciliationResponse)
def trigger_proactive_reconciliation(
    min_age_minutes: int = Query(default=15, ge=0),
    session: Session = Depends(get_session),
) -> ProactiveReconciliationResponse:
    """Triggers a proactive scan for settled unallocated receipts (Entry B)."""
    return run_proactive_reconciliation_scan(
        session=session,
        min_age_minutes=min_age_minutes,
    )

