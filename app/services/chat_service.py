import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.models.account import CustomerAccount
from app.models.case import AuditEvent, InvestigationCase
from app.models.chat import (
    CaseEvent,
    ChatMessage,
    ChatSession,
    CustomerEvidenceClaim,
    OutboxEvent,
)
from app.models.enums import (
    CaseState,
    EventVisibility,
    MessageSenderRole,
    OutboxStatus,
)
from app.services.case_event_helper import append_case_event

KNOWN_SENDER_BANKS = (
    "Monzo",
    "Revolut",
    "Barclays",
    "HSBC",
    "NatWest",
    "Starling",
    "Santander",
    "Lloyds",
    "Halifax",
    "Chase",
)

KNOWN_REFERENCE_PREFIXES = (
    "MON-",
    "INV-",
    "FP-",
    "REF-",
    "STRP-",
    "PAY-",
    "TXN-",
)


class ChatAccessDeniedError(Exception):
    """Raised when a customer attempts to access another customer's account, session, or case."""


class ChatResourceNotFoundError(Exception):
    """Raised when a requested chat session or account does not exist."""


class CreateChatSessionRequest(BaseModel):
    beneficiary_account_id: Optional[str] = Field(
        default=None,
        description="Optional customer account ID; defaults to the authenticated customer's primary account.",
    )


class ChatSessionResponse(BaseModel):
    session_id: str
    customer_id: str
    beneficiary_account_id: str
    currency: str
    active_case_id: Optional[str] = None
    created_at: str


class SendChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ChatMessageResponse(BaseModel):
    session_id: str
    customer_message_id: str
    agent_message_id: str
    agent_reply: str
    active_case_id: Optional[str] = None
    case_state: Optional[str] = None
    evidence_version: Optional[int] = None
    outbox_id: Optional[str] = None
    missing_slots: List[str] = []


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def extract_payment_clues_from_text(
    text: str,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """
    Extracts (sender_bank, amount_minor, payment_reference) from a customer chat message
    using simple, deterministic token scanning (no complex regex).
    """
    cleaned_text = text.strip()
    lower_text = cleaned_text.lower()

    # 1. Detect sender bank from allowlist
    detected_bank: Optional[str] = None
    for bank in KNOWN_SENDER_BANKS:
        if bank.lower() in lower_text:
            detected_bank = bank
            break

    # 2. Tokenize by whitespace and strip punctuation
    raw_tokens = cleaned_text.replace(",", " ").split()
    detected_amount_minor: Optional[int] = None
    detected_reference: Optional[str] = None

    for idx, raw_token in enumerate(raw_tokens):
        token = raw_token.strip(".,!?;:\"'()[]{}")
        if not token:
            continue

        upper_token = token.upper()

        # Check for payment reference prefix (e.g. MON-78291, INV-88210)
        if any(upper_token.startswith(prefix) for prefix in KNOWN_REFERENCE_PREFIXES):
            if not upper_token.startswith("ACC-LEN-"):
                detected_reference = upper_token
                continue

        # Check for currency amount (e.g. £500, £500.00, 500gbp, or number before 'pounds'/'gbp')
        has_pound_prefix = token.startswith("£") or token.upper().startswith("GBP")
        has_currency_suffix = token.upper().endswith("GBP")
        next_word_is_currency = False
        if idx + 1 < len(raw_tokens):
            next_clean = raw_tokens[idx + 1].strip(".,!?;:\"'()[]{}").lower()
            if next_clean in ("gbp", "pound", "pounds", "sterling"):
                next_word_is_currency = True

        if has_pound_prefix or has_currency_suffix or next_word_is_currency:
            numeric_str = (
                token.lstrip("£GgBbPp").rstrip("GgBbPp").strip()
            )
            amount_minor = _parse_decimal_to_minor_units(numeric_str)
            if amount_minor is not None and amount_minor > 0:
                detected_amount_minor = amount_minor

    return detected_bank, detected_amount_minor, detected_reference


def _parse_decimal_to_minor_units(numeric_str: str) -> Optional[int]:
    """Converts a positive decimal string like '500' or '500.25' into integer minor units (pence)."""
    if not numeric_str:
        return None
    if "." in numeric_str:
        parts = numeric_str.split(".")
        if len(parts) != 2:
            return None
        whole_part, frac_part = parts[0], parts[1]
        if not whole_part.isdigit() or not frac_part.isdigit():
            return None
        frac_padded = (frac_part + "00")[:2]
        return int(whole_part) * 100 + int(frac_padded)
    if numeric_str.isdigit():
        return int(numeric_str) * 100
    return None


def _create_outbox_job(
    session: Session,
    case_id: str,
    session_id: str,
    evidence_version: int,
) -> OutboxEvent:
    """Stages an idempotent OutboxEvent row inside the caller's current SQLite transaction."""
    idempotency_key = f"JOB-{case_id}-v{evidence_version}"
    existing = session.exec(
        select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
    ).first()
    if existing is not None:
        return existing

    outbox_id = f"OUT-{uuid4().hex[:8].upper()}"
    payload = {
        "outbox_id": outbox_id,
        "case_id": case_id,
        "session_id": session_id,
        "evidence_version": evidence_version,
        "attempt": 1,
    }
    outbox = OutboxEvent(
        outbox_id=outbox_id,
        stream_name="resolveflow:investigations",
        idempotency_key=idempotency_key,
        payload_json=json.dumps(payload),
        status=OutboxStatus.PENDING,
        created_at=get_utc_now(),
    )
    session.add(outbox)
    session.flush()
    return outbox


def create_chat_session(
    session: Session,
    customer_id: str,
    beneficiary_account_id: Optional[str] = None,
) -> ChatSessionResponse:
    """
    Creates an authenticated customer chat session bound to their verified account
    and automatically attaches any existing open investigation case.
    """
    normalized_customer = (customer_id or "").strip()
    if not normalized_customer or normalized_customer == "INSTITUTION":
        raise ChatAccessDeniedError("A valid authenticated customer_id is required.")

    if beneficiary_account_id:
        account = session.exec(
            select(CustomerAccount).where(
                CustomerAccount.account_id == beneficiary_account_id.strip()
            )
        ).first()
        if account is None:
            raise ChatResourceNotFoundError(
                f"Account '{beneficiary_account_id}' not found."
            )
        if account.customer_id != normalized_customer:
            raise ChatAccessDeniedError(
                f"Customer '{normalized_customer}' is not authorized to access account '{beneficiary_account_id}'."
            )
    else:
        account = session.exec(
            select(CustomerAccount).where(
                CustomerAccount.customer_id == normalized_customer
            )
        ).first()
        if account is None:
            raise ChatResourceNotFoundError(
                f"No beneficiary account found for customer '{normalized_customer}'."
            )

    # Check if customer already has an open/active case on this account
    existing_case = session.exec(
        select(InvestigationCase)
        .where(
            InvestigationCase.customer_id == normalized_customer,
            InvestigationCase.beneficiary_account_id == account.account_id,
            InvestigationCase.state.in_(
                [
                    CaseState.OPEN,
                    CaseState.INVESTIGATING,
                    CaseState.AWAITING_REVIEW,
                    CaseState.WAITING_PROVIDER,
                ]
            ),
        )
        .order_by(InvestigationCase.created_at.desc())
    ).first()

    session_id = f"CSESS-{uuid4().hex[:8].upper()}"
    now = get_utc_now()
    chat_session = ChatSession(
        session_id=session_id,
        tenant_id=account.tenant_id,
        customer_id=normalized_customer,
        beneficiary_account_id=account.account_id,
        active_case_id=existing_case.case_id if existing_case else None,
        draft_sender_bank=existing_case.sender_bank if existing_case else None,
        draft_amount_minor=existing_case.amount_minor if existing_case else None,
        draft_reference=existing_case.reference if existing_case else None,
        created_at=now,
        updated_at=now,
    )
    session.add(chat_session)
    session.commit()

    return ChatSessionResponse(
        session_id=chat_session.session_id,
        customer_id=chat_session.customer_id,
        beneficiary_account_id=chat_session.beneficiary_account_id,
        currency=account.currency,
        active_case_id=chat_session.active_case_id,
        created_at=chat_session.created_at.isoformat(),
    )


def handle_customer_message(
    session: Session,
    session_id: str,
    customer_id: str,
    content: str,
) -> ChatMessageResponse:
    """
    Processes a customer chat message with context-aware slot extraction,
    concurrent active-investigation safety, versioned evidence updates,
    and atomic SQLite Transactional Outbox queueing (Spec 08).
    """
    normalized_customer = (customer_id or "").strip()
    chat_session = session.exec(
        select(ChatSession).where(ChatSession.session_id == session_id)
    ).first()
    if chat_session is None:
        raise ChatResourceNotFoundError(f"Chat session '{session_id}' not found.")

    if chat_session.customer_id != normalized_customer:
        raise ChatAccessDeniedError(
            f"Customer '{normalized_customer}' is not authorized to access session '{session_id}'."
        )

    account = session.exec(
        select(CustomerAccount).where(
            CustomerAccount.account_id == chat_session.beneficiary_account_id
        )
    ).first()
    currency = account.currency if account else "GBP"

    extracted_bank, extracted_amount, extracted_ref = extract_payment_clues_from_text(
        content
    )

    # Persist the customer's incoming chat message
    customer_msg_id = f"MSG-{uuid4().hex[:8].upper()}"
    customer_msg = ChatMessage(
        message_id=customer_msg_id,
        session_id=chat_session.session_id,
        sender_role=MessageSenderRole.CUSTOMER,
        content=content.strip(),
        created_at=get_utc_now(),
    )
    session.add(customer_msg)

    active_case: Optional[InvestigationCase] = None
    if chat_session.active_case_id:
        active_case = session.exec(
            select(InvestigationCase).where(
                InvestigationCase.case_id == chat_session.active_case_id
            )
        ).first()

    # -------------------------------------------------------------------------
    # Branch A: An active case already exists on this session
    # -------------------------------------------------------------------------
    if active_case is not None:
        has_new_reference = (
            extracted_ref is not None and extracted_ref != active_case.reference
        )
        has_new_amount = (
            extracted_amount is not None and extracted_amount != active_case.amount_minor
        )
        has_new_bank = (
            extracted_bank is not None
            and extracted_bank != active_case.sender_bank
        )

        # Sub-branch A1: Customer supplied new or updated evidence while case exists/runs!
        if has_new_reference or has_new_amount or has_new_bank:
            new_version = active_case.latest_evidence_version + 1
            active_case.latest_evidence_version = new_version
            if extracted_ref is not None:
                active_case.reference = extracted_ref
                chat_session.draft_reference = extracted_ref
            if extracted_amount is not None:
                active_case.amount_minor = extracted_amount
                chat_session.draft_amount_minor = extracted_amount
            if extracted_bank is not None:
                active_case.sender_bank = extracted_bank
                chat_session.draft_sender_bank = extracted_bank
            active_case.updated_at = get_utc_now()
            chat_session.updated_at = get_utc_now()

            customer_msg.evidence_version = new_version
            session.add(customer_msg)
            session.add(active_case)
            session.add(chat_session)

            claim = CustomerEvidenceClaim(
                case_id=active_case.case_id,
                evidence_version=new_version,
                claimed_reference=active_case.reference,
                claimed_amount_minor=active_case.amount_minor,
                claimed_sender_bank=active_case.sender_bank,
                source_message_id=customer_msg.message_id,
                created_at=get_utc_now(),
            )
            session.add(claim)

            append_case_event(
                session=session,
                case_id=active_case.case_id,
                session_id=chat_session.session_id,
                event_type="EVIDENCE_UPDATED",
                visibility=EventVisibility.CUSTOMER_SAFE,
                payload={
                    "case_id": active_case.case_id,
                    "evidence_version": new_version,
                    "reference": active_case.reference,
                    "amount_minor": active_case.amount_minor,
                    "sender_bank": active_case.sender_bank,
                    "message": (
                        f"Updated payment details recorded (v{new_version}: reference "
                        f"{active_case.reference}, £{active_case.amount_minor / 100:.2f}). "
                        f"Incorporating into active investigation #{active_case.case_id}."
                    ),
                },
            )

            outbox = _create_outbox_job(
                session=session,
                case_id=active_case.case_id,
                session_id=chat_session.session_id,
                evidence_version=new_version,
            )

            agent_reply = (
                f"Thank you — I have updated case #{active_case.case_id} with your new details "
                f"(reference {active_case.reference}, £{active_case.amount_minor / 100:.2f} "
                f"from {active_case.sender_bank or 'your sending bank'}, evidence v{new_version}) "
                f"without interrupting the active investigation."
            )
            agent_msg_id = f"MSG-{uuid4().hex[:8].upper()}"
            session.add(
                ChatMessage(
                    message_id=agent_msg_id,
                    session_id=chat_session.session_id,
                    sender_role=MessageSenderRole.AGENT,
                    content=agent_reply,
                    evidence_version=new_version,
                    created_at=get_utc_now(),
                )
            )
            session.commit()

            return ChatMessageResponse(
                session_id=chat_session.session_id,
                customer_message_id=customer_msg_id,
                agent_message_id=agent_msg_id,
                agent_reply=agent_reply,
                active_case_id=active_case.case_id,
                case_state=active_case.state.value,
                evidence_version=new_version,
                outbox_id=outbox.outbox_id,
                missing_slots=[],
            )

        # Sub-branch A2: Informational follow-up while investigation is running or resolved
        latest_safe_event = session.exec(
            select(CaseEvent)
            .where(
                CaseEvent.case_id == active_case.case_id,
                CaseEvent.visibility == EventVisibility.CUSTOMER_SAFE,
            )
            .order_by(CaseEvent.sequence_id.desc())
        ).first()

        if latest_safe_event is not None:
            event_data = json.loads(latest_safe_event.payload_json)
            status_message = event_data.get("message", "")
        else:
            status_message = (
                f"Your investigation #{active_case.case_id} for reference "
                f"{active_case.reference} (£{active_case.amount_minor / 100:.2f}) "
                f"is currently in status {active_case.state.value}."
            )

        agent_reply = (
            f"Case #{active_case.case_id} status ({active_case.state.value}): {status_message} "
            f"You can share any additional transfer details here at any time."
        )
        agent_msg_id = f"MSG-{uuid4().hex[:8].upper()}"
        session.add(
            ChatMessage(
                message_id=agent_msg_id,
                session_id=chat_session.session_id,
                sender_role=MessageSenderRole.AGENT,
                content=agent_reply,
                created_at=get_utc_now(),
            )
        )
        chat_session.updated_at = get_utc_now()
        session.add(chat_session)
        session.commit()

        return ChatMessageResponse(
            session_id=chat_session.session_id,
            customer_message_id=customer_msg_id,
            agent_message_id=agent_msg_id,
            agent_reply=agent_reply,
            active_case_id=active_case.case_id,
            case_state=active_case.state.value,
            evidence_version=active_case.latest_evidence_version,
            outbox_id=None,
            missing_slots=[],
        )

    # -------------------------------------------------------------------------
    # Branch B: No active case yet — Merge slots with session draft context
    # -------------------------------------------------------------------------
    if extracted_bank is not None:
        chat_session.draft_sender_bank = extracted_bank
    if extracted_amount is not None:
        chat_session.draft_amount_minor = extracted_amount
    if extracted_ref is not None:
        chat_session.draft_reference = extracted_ref

    missing_slots: List[str] = []
    if chat_session.draft_amount_minor is None:
        missing_slots.append("amount_minor")
    if chat_session.draft_reference is None:
        missing_slots.append("payment_reference")
    if chat_session.draft_sender_bank is None:
        missing_slots.append("sender_bank")

    # We require at least amount_minor and payment_reference to open an investigation case
    required_missing = [
        slot for slot in missing_slots if slot in ("amount_minor", "payment_reference")
    ]

    if required_missing:
        known_parts: List[str] = [
            f"account {chat_session.beneficiary_account_id} ({currency})"
        ]
        if chat_session.draft_sender_bank:
            known_parts.append(f"from {chat_session.draft_sender_bank}")
        if chat_session.draft_amount_minor:
            known_parts.append(f"for £{chat_session.draft_amount_minor / 100:.2f}")
        if chat_session.draft_reference:
            known_parts.append(f"with reference {chat_session.draft_reference}")

        needed_prompts: List[str] = []
        if "amount_minor" in required_missing:
            needed_prompts.append("the transfer amount (e.g. £500.00)")
        if "payment_reference" in required_missing:
            needed_prompts.append("the payment reference number (e.g. MON-78291)")
        if "sender_bank" in missing_slots:
            needed_prompts.append("the sending bank name")

        agent_reply = (
            f"I have your LenFi {', '.join(known_parts)}. "
            f"To trace this transfer immediately, could you please share {' and '.join(needed_prompts)}?"
        )
        agent_msg_id = f"MSG-{uuid4().hex[:8].upper()}"
        chat_session.updated_at = get_utc_now()
        session.add(chat_session)
        session.add(
            ChatMessage(
                message_id=agent_msg_id,
                session_id=chat_session.session_id,
                sender_role=MessageSenderRole.AGENT,
                content=agent_reply,
                created_at=get_utc_now(),
            )
        )
        session.commit()

        return ChatMessageResponse(
            session_id=chat_session.session_id,
            customer_message_id=customer_msg_id,
            agent_message_id=agent_msg_id,
            agent_reply=agent_reply,
            active_case_id=None,
            case_state=None,
            evidence_version=None,
            outbox_id=None,
            missing_slots=required_missing,
        )

    # All required details collected! Create InvestigationCase + Evidence v1 + Outbox Job atomically
    case_id = f"CASE-{uuid4().hex[:8].upper()}"
    new_case = InvestigationCase(
        case_id=case_id,
        customer_id=chat_session.customer_id,
        beneficiary_account_id=chat_session.beneficiary_account_id,
        sender_bank=chat_session.draft_sender_bank,
        reference=chat_session.draft_reference or "",
        amount_minor=chat_session.draft_amount_minor or 0,
        currency=currency,
        state=CaseState.OPEN,
        latest_evidence_version=1,
        processed_evidence_version=0,
        created_at=get_utc_now(),
        updated_at=get_utc_now(),
    )
    session.add(new_case)

    chat_session.active_case_id = case_id
    chat_session.updated_at = get_utc_now()
    session.add(chat_session)

    customer_msg.evidence_version = 1
    session.add(customer_msg)

    claim = CustomerEvidenceClaim(
        case_id=case_id,
        evidence_version=1,
        claimed_reference=new_case.reference,
        claimed_amount_minor=new_case.amount_minor,
        claimed_sender_bank=new_case.sender_bank,
        source_message_id=customer_msg.message_id,
        created_at=get_utc_now(),
    )
    session.add(claim)

    append_case_event(
        session=session,
        case_id=case_id,
        session_id=chat_session.session_id,
        event_type="CASE_OPENED",
        visibility=EventVisibility.CUSTOMER_SAFE,
        payload={
            "case_id": case_id,
            "status": CaseState.OPEN.value,
            "evidence_version": 1,
            "reference": new_case.reference,
            "amount_minor": new_case.amount_minor,
            "currency": new_case.currency,
            "beneficiary_account_id": new_case.beneficiary_account_id,
            "message": (
                f"Opened investigation case #{case_id} for reference {new_case.reference} "
                f"(£{new_case.amount_minor / 100:.2f}) to account {new_case.beneficiary_account_id}."
            ),
        },
    )

    outbox = _create_outbox_job(
        session=session,
        case_id=case_id,
        session_id=chat_session.session_id,
        evidence_version=1,
    )

    session.add(
        AuditEvent(
            actor="CustomerConversationService",
            action="CHAT_CASE_OPENED_AND_QUEUED",
            before_state="NONE",
            after_state=CaseState.OPEN.value,
            details=(
                f"[{case_id}] Opened from chat session {chat_session.session_id} "
                f"(evidence v1) and staged outbox job {outbox.outbox_id}"
            ),
            timestamp=get_utc_now(),
        )
    )

    agent_reply = (
        f"Thank you. I have opened investigation case #{case_id} for your "
        f"£{new_case.amount_minor / 100:.2f} transfer (reference {new_case.reference}) "
        f"to account {new_case.beneficiary_account_id} and started checking our clearing records "
        f"in the background. You can continue messaging me here at any time."
    )
    agent_msg_id = f"MSG-{uuid4().hex[:8].upper()}"
    session.add(
        ChatMessage(
            message_id=agent_msg_id,
            session_id=chat_session.session_id,
            sender_role=MessageSenderRole.AGENT,
            content=agent_reply,
            evidence_version=1,
            created_at=get_utc_now(),
        )
    )
    session.commit()

    return ChatMessageResponse(
        session_id=chat_session.session_id,
        customer_message_id=customer_msg_id,
        agent_message_id=agent_msg_id,
        agent_reply=agent_reply,
        active_case_id=case_id,
        case_state=new_case.state.value,
        evidence_version=1,
        outbox_id=outbox.outbox_id,
        missing_slots=[],
    )


def get_customer_safe_case_events(
    session: Session,
    session_id: str,
    customer_id: str,
    after_sequence_id: int = 0,
) -> List[CaseEvent]:
    """
    Returns all persisted CUSTOMER_SAFE CaseEvents for a session after a given sequence_id,
    enforcing strict customer ownership and filtering out all INTERNAL_ONLY events.
    """
    normalized_customer = (customer_id or "").strip()
    chat_session = session.exec(
        select(ChatSession).where(ChatSession.session_id == session_id)
    ).first()
    if chat_session is None:
        raise ChatResourceNotFoundError(f"Chat session '{session_id}' not found.")

    if chat_session.customer_id != normalized_customer:
        raise ChatAccessDeniedError(
            f"Customer '{normalized_customer}' is not authorized to access events for '{session_id}'."
        )

    if not chat_session.active_case_id:
        return []

    events = session.exec(
        select(CaseEvent)
        .where(
            CaseEvent.case_id == chat_session.active_case_id,
            CaseEvent.visibility == EventVisibility.CUSTOMER_SAFE,
            CaseEvent.sequence_id > after_sequence_id,
        )
        .order_by(CaseEvent.sequence_id.asc())
    ).all()
    return list(events)

