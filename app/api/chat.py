from typing import Iterator, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session

from app.core.database import get_session
from app.services.chat_service import (
    ChatAccessDeniedError,
    ChatMessageResponse,
    ChatResourceNotFoundError,
    ChatSessionResponse,
    CreateChatSessionRequest,
    SendChatMessageRequest,
    create_chat_session,
    get_customer_safe_case_events,
    handle_customer_message,
)

router = APIRouter(prefix="/api/v1/chat", tags=["Customer Conversation & SSE"])


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_session_endpoint(
    payload: Optional[CreateChatSessionRequest] = None,
    x_customer_id: str = Header(..., alias="X-Customer-ID"),
    session: Session = Depends(get_session),
) -> ChatSessionResponse:
    """Creates an authenticated customer chat session."""
    try:
        beneficiary_id = payload.beneficiary_account_id if payload else None
        return create_chat_session(
            session=session,
            customer_id=x_customer_id,
            beneficiary_account_id=beneficiary_id,
        )
    except ChatAccessDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ChatResourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatMessageResponse,
)
def send_message_endpoint(
    session_id: str,
    payload: SendChatMessageRequest,
    x_customer_id: str = Header(..., alias="X-Customer-ID"),
    session: Session = Depends(get_session),
) -> ChatMessageResponse:
    """
    Sends a customer message inside an authenticated session.
    Supports context-aware slot extraction, concurrent messages during active
    investigations, versioned evidence updates, and transactional outbox queueing.
    """
    try:
        return handle_customer_message(
            session=session,
            session_id=session_id,
            customer_id=x_customer_id,
            content=payload.content,
        )
    except ChatAccessDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ChatResourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get("/sessions/{session_id}/events")
def stream_session_events_endpoint(
    session_id: str,
    x_customer_id: str = Header(..., alias="X-Customer-ID"),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    after_sequence_id: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """
    Persisted Server-Sent Events (SSE) endpoint for customer-safe case updates (Spec 08).
    Supports Last-Event-ID header and after_sequence_id query parameter for missed-event replay.
    """
    effective_cursor = after_sequence_id
    if last_event_id is not None and last_event_id.strip().isdigit():
        effective_cursor = max(effective_cursor, int(last_event_id.strip()))

    try:
        events = get_customer_safe_case_events(
            session=session,
            session_id=session_id,
            customer_id=x_customer_id,
            after_sequence_id=effective_cursor,
        )
    except ChatAccessDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ChatResourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    def sse_generator() -> Iterator[str]:
        for evt in events:
            yield (
                f"id: {evt.sequence_id}\n"
                f"event: {evt.event_type}\n"
                f"data: {evt.payload_json}\n\n"
            )

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )

