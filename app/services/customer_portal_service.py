import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
from pydantic import BaseModel
from sqlmodel import Session, select

from app.agents.graph import run_case_investigation
from app.models.account import CustomerAccount
from app.models.case import AuditEvent, InvestigationCase
from app.models.chat import ChatMessage, ChatSession, CustomerEvidenceClaim
from app.models.enums import (
    AllocationStatus,
    CaseState,
    MessageSenderRole,
    ProviderPaymentStatus,
)
from app.models.payment import IncomingPayment
from app.services.chat_service import (
    create_chat_session,
    extract_payment_clues_from_text,
    handle_customer_message,
)
from app.services.ledger_service import get_account_balance_minor

PRIMARY_DEMO_CUSTOMER_ID = "CUST-101"
PRIMARY_DEMO_ACCOUNT_ID = "ACC-LEN-001"
PBKDF2_ITERATIONS = 120_000


class PinValidationError(Exception):
    """Raised when a PIN does not meet 4-digit format or confirmation requirements."""


class InvalidPinError(Exception):
    """Raised when a submitted PIN does not match the customer's stored PIN hash."""


class CustomerIsolationError(Exception):
    """Raised when a customer attempts to access another customer's account, payment, case, or chat."""


class CustomerIncomingPaymentItem(BaseModel):
    """Customer-safe view of a single incoming payment or missing-payment claim."""

    item_id: str
    payment_id: Optional[str] = None
    case_id: Optional[str] = None
    amount_minor: int
    formatted_amount: str
    currency: str
    sender_bank: str
    reference: str
    date_display: str
    provider_status: str
    allocation_status: str
    rail_status_label: str
    account_status_label: str
    plain_explanation: str
    case_state: Optional[str] = None
    can_get_help: bool = True


class CustomerHomeSummary(BaseModel):
    """Customer-scoped home screen summary for the ResolveFlow receiving bank app."""

    customer_id: str
    customer_name: str
    account_id: str
    account_number: str
    sort_code: str
    currency: str
    available_balance_minor: int
    formatted_balance: str
    incoming_items: List[CustomerIncomingPaymentItem]


class CustomerHelpInvestigationOutcome(BaseModel):
    """Customer-safe result returned after investigating or chatting about an incoming payment."""

    case_id: str
    case_state: str
    payment_id: Optional[str] = None
    execution_status: str
    credited_to_customer: bool
    new_balance_minor: int
    formatted_new_balance: str
    customer_safe_title: str
    customer_safe_message: str


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_gbp_minor(amount_minor: int) -> str:
    """Formats integer minor units (pence) as a £X,XXX.XX string."""
    pounds = amount_minor / 100.0
    return f"£{pounds:,.2f}"


def validate_four_digit_pin(pin: str) -> str:
    cleaned = (pin or "").strip()
    if len(cleaned) != 4 or not cleaned.isdigit():
        raise PinValidationError("PIN must be exactly 4 numeric digits (0-9).")
    return cleaned


def hash_pin_with_salt(pin: str, salt: str) -> str:
    """Derives a PBKDF2-HMAC-SHA256 digest for a 4-digit PIN and hex salt."""
    valid_pin = validate_four_digit_pin(pin)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        valid_pin.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return derived.hex()


def setup_customer_profile_and_pin(
    session: Session,
    customer_id: str,
    customer_name: str,
    pin: str,
    confirm_pin: str,
) -> CustomerAccount:
    """
    Completes first-use onboarding for the customer:
    - Validates and hashes the 4-digit PIN with a cryptographic random salt.
    - Never stores the plain PIN.
    - Assigns/confirms the stable ResolveFlow account number and sort code.
    """
    valid_pin = validate_four_digit_pin(pin)
    valid_confirm = validate_four_digit_pin(confirm_pin)
    if not hmac.compare_digest(valid_pin, valid_confirm):
        raise PinValidationError("PIN and confirmation PIN do not match.")

    cleaned_name = (customer_name or "").strip() or "Benjamin Chidera"

    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    if account is None:
        raise CustomerIsolationError(f"Customer profile '{customer_id}' not found.")

    salt = secrets.token_hex(16)
    pin_digest = hash_pin_with_salt(valid_pin, salt)

    account.customer_name = cleaned_name
    account.pin_salt = salt
    account.pin_hash = pin_digest
    account.is_onboarded = True
    session.add(account)

    session.add(
        AuditEvent(
            actor=customer_id,
            action="CUSTOMER_ONBOARDED_PIN_SET",
            before_state="UNONBOARDED",
            after_state="ONBOARDED",
            details=(
                f"Customer '{cleaned_name}' ({account.account_id}) completed first-use "
                f"PIN setup (PBKDF2-SHA256 hash stored; plain PIN discarded)."
            ),
            timestamp=get_utc_now(),
        )
    )
    session.commit()
    session.refresh(account)
    return account


def authenticate_customer_pin(
    session: Session,
    customer_id: str,
    pin: str,
) -> CustomerAccount:
    """Verifies a customer's 4-digit PIN against their stored PBKDF2 hash in constant time."""
    valid_pin = validate_four_digit_pin(pin)
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    if account is None or not account.is_onboarded or not account.pin_hash or not account.pin_salt:
        raise InvalidPinError("Customer profile has not completed PIN setup yet.")

    candidate_hash = hash_pin_with_salt(valid_pin, account.pin_salt)
    if not hmac.compare_digest(account.pin_hash, candidate_hash):
        raise InvalidPinError("Incorrect PIN entered.")

    return account


def _extract_display_reference(raw_reference: str) -> str:
    """Strips internal account suffixes like 'ACC-LEN-001' for clean customer display."""
    tokens = [
        tok
        for tok in (raw_reference or "").split()
        if not tok.upper().startswith("ACC-LEN-")
    ]
    if tokens:
        return tokens[0]
    return raw_reference or "N/A"


def _build_item_from_payment(
    payment: IncomingPayment,
    linked_case: Optional[InvestigationCase],
) -> CustomerIncomingPaymentItem:
    display_ref = _extract_display_reference(payment.raw_reference)
    case_state_str = linked_case.state.value if linked_case else None

    if payment.provider_status == ProviderPaymentStatus.SETTLED:
        rail_label = "Settled"
        if payment.allocation_status == AllocationStatus.ALLOCATED:
            account_label = "Credited to your account"
            explanation = (
                f"This {format_gbp_minor(payment.amount_minor)} transfer from {payment.sender_bank} "
                f"(ref: {display_ref}) has settled and been credited to your ResolveFlow balance."
            )
            can_help = False
        elif linked_case and linked_case.state == CaseState.AWAITING_REVIEW:
            account_label = "Under specialist review"
            explanation = (
                f"Your {format_gbp_minor(payment.amount_minor)} transfer from {payment.sender_bank} "
                f"(ref: {display_ref}) has arrived at ResolveFlow and is currently being verified "
                "by our payment specialist team."
            )
            can_help = True
        else:
            account_label = "Awaiting allocation"
            explanation = (
                f"ResolveFlow has received {format_gbp_minor(payment.amount_minor)} from "
                f"{payment.sender_bank} (ref: {display_ref}) into clearing suspense, but it has "
                f"not been allocated to your balance yet."
            )
            can_help = True
    elif payment.provider_status == ProviderPaymentStatus.PENDING:
        rail_label = "Pending"
        account_label = "Awaiting rail settlement"
        explanation = (
            f"{payment.sender_bank} reported a pending {format_gbp_minor(payment.amount_minor)} "
            f"transfer (ref: {display_ref}). ResolveFlow cannot credit your account before "
            f"the external payment rail confirms final settlement."
        )
        can_help = True
    elif payment.provider_status == ProviderPaymentStatus.FAILED:
        rail_label = "Failed"
        account_label = "Not credited (Failed on rail)"
        explanation = (
            f"This {format_gbp_minor(payment.amount_minor)} transfer from {payment.sender_bank} "
            f"(ref: {display_ref}) failed on the external rail before settling with ResolveFlow. "
            f"No funds arrived, so we cannot credit your account."
        )
        can_help = True
    else:
        # REVERSED
        rail_label = "Reversed / Returned"
        account_label = "Not credited (Reversed by sender bank)"
        explanation = (
            f"This {format_gbp_minor(payment.amount_minor)} transfer from {payment.sender_bank} "
            f"(ref: {display_ref}) was recalled or reversed by the sending bank. "
            f"Funds were returned and cannot be credited to your account."
        )
        can_help = True

    return CustomerIncomingPaymentItem(
        item_id=payment.payment_id,
        payment_id=payment.payment_id,
        case_id=linked_case.case_id if linked_case else None,
        amount_minor=payment.amount_minor,
        formatted_amount=format_gbp_minor(payment.amount_minor),
        currency=payment.currency,
        sender_bank=payment.sender_bank,
        reference=display_ref,
        date_display=payment.received_at.strftime("%Y-%m-%d %H:%M UTC"),
        provider_status=payment.provider_status.value,
        allocation_status=payment.allocation_status.value,
        rail_status_label=rail_label,
        account_status_label=account_label,
        plain_explanation=explanation,
        case_state=case_state_str,
        can_get_help=can_help,
    )


def _build_item_from_unmatched_case(
    case: InvestigationCase,
    ambiguous_payment: Optional[IncomingPayment],
) -> CustomerIncomingPaymentItem:
    if ambiguous_payment is not None:
        rail_label = "Settled (Reference needs verification)"
        if case.state == CaseState.AWAITING_REVIEW:
            account_label = "Under specialist review"
            explanation = (
                f"We located an incoming {format_gbp_minor(case.amount_minor)} transfer from "
                f"{ambiguous_payment.sender_bank} matching '{case.reference}', but the sender's "
                f"reference did not uniquely identify your account number. Our payment review team "
                f"is verifying case #{case.case_id}."
            )
        elif case.state == CaseState.RESOLVED:
            account_label = "Credited to your account"
            explanation = (
                f"Our payment review team verified your {format_gbp_minor(case.amount_minor)} "
                f"transfer (ref: {case.reference}) and credited your ResolveFlow account."
            )
        else:
            account_label = "Awaiting verification"
            explanation = (
                f"A {format_gbp_minor(case.amount_minor)} transfer from {ambiguous_payment.sender_bank} "
                f"with reference '{case.reference}' arrived in clearing suspense, but requires "
                f"verification before it can be allocated."
            )
        return CustomerIncomingPaymentItem(
            item_id=case.case_id,
            payment_id=ambiguous_payment.payment_id,
            case_id=case.case_id,
            amount_minor=case.amount_minor,
            formatted_amount=format_gbp_minor(case.amount_minor),
            currency=case.currency,
            sender_bank=case.sender_bank or ambiguous_payment.sender_bank,
            reference=case.reference,
            date_display=case.created_at.strftime("%Y-%m-%d %H:%M UTC"),
            provider_status=ambiguous_payment.provider_status.value,
            allocation_status=ambiguous_payment.allocation_status.value,
            rail_status_label=rail_label,
            account_status_label=account_label,
            plain_explanation=explanation,
            case_state=case.state.value,
            can_get_help=case.state != CaseState.RESOLVED,
        )

    # No receipt found at all (Scenario 3 / S2)
    rail_label = "No confirmed receipt"
    if case.state == CaseState.WAITING_PROVIDER:
        account_label = "Tracing with sending bank"
        explanation = (
            f"You reported a {format_gbp_minor(case.amount_minor)} transfer from "
            f"{case.sender_bank or 'your sending bank'} (ref: {case.reference}). "
            f"ResolveFlow has found no authoritative receipt on the payment rail yet. "
            "We are awaiting provider confirmation."
        )
    else:
        account_label = "Reported missing (Unverified)"
        explanation = (
            f"You reported expecting {format_gbp_minor(case.amount_minor)} from "
            f"{case.sender_bank or 'an external bank'} (ref: {case.reference}). "
            f"ResolveFlow has not received a matching settlement event yet."
        )

    return CustomerIncomingPaymentItem(
        item_id=case.case_id,
        payment_id=None,
        case_id=case.case_id,
        amount_minor=case.amount_minor,
        formatted_amount=format_gbp_minor(case.amount_minor),
        currency=case.currency,
        sender_bank=case.sender_bank or "External Bank",
        reference=case.reference,
        date_display=case.created_at.strftime("%Y-%m-%d %H:%M UTC"),
        provider_status="NO_RECEIPT",
        allocation_status="UNALLOCATED",
        rail_status_label=rail_label,
        account_status_label=account_label,
        plain_explanation=explanation,
        case_state=case.state.value,
        can_get_help=True,
    )


def list_customer_incoming_items(
    session: Session,
    customer_id: str,
) -> List[CustomerIncomingPaymentItem]:
    """
    Returns all incoming payments and reported claims scoped strictly to `customer_id`.
    Never exposes records belonging to other customers.
    """
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    if account is None or account.customer_id == "INSTITUTION":
        raise CustomerIsolationError(f"Unauthorized or unknown customer '{customer_id}'.")

    # 1. Load customer's own InvestigationCases
    customer_cases = session.exec(
        select(InvestigationCase)
        .where(
            InvestigationCase.customer_id == customer_id,
            InvestigationCase.beneficiary_account_id == account.account_id,
        )
        .order_by(InvestigationCase.created_at.asc())
    ).all()

    cases_by_ref: Dict[str, InvestigationCase] = {}
    for c in customer_cases:
        clean_ref = c.reference.strip().upper()
        if clean_ref not in cases_by_ref:
            cases_by_ref[clean_ref] = c

    # 2. Load IncomingPayments explicitly addressed to this customer's beneficiary_account_id
    direct_payments = session.exec(
        select(IncomingPayment)
        .where(IncomingPayment.beneficiary_account_id == account.account_id)
        .order_by(IncomingPayment.received_at.asc())
    ).all()

    items: List[CustomerIncomingPaymentItem] = []
    matched_case_ids = set()

    for pay in direct_payments:
        display_ref = _extract_display_reference(pay.raw_reference).upper()
        linked_case = cases_by_ref.get(display_ref)
        if linked_case:
            matched_case_ids.add(linked_case.case_id)
        items.append(_build_item_from_payment(pay, linked_case))

    # 3. Include customer's own reported cases that aren't directly linked to a direct payment
    #    (e.g., Scenario 3: No receipt found, or Scenario 6: Ambiguous wire with beneficiary_account_id=None)
    for c in customer_cases:
        if c.case_id in matched_case_ids:
            continue

        # Check if there is an unallocated wire with beneficiary_account_id=None matching the case ref
        ambiguous_wire = session.exec(
            select(IncomingPayment).where(
                IncomingPayment.beneficiary_account_id.is_(None),
                IncomingPayment.raw_reference.contains(c.reference),
                IncomingPayment.amount_minor == c.amount_minor,
            )
        ).first()
        items.append(_build_item_from_unmatched_case(c, ambiguous_wire))

    return items


def get_customer_home_summary(
    session: Session,
    customer_id: str,
) -> CustomerHomeSummary:
    """Returns the customer-scoped home screen summary for the logged-in customer."""
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    if account is None or account.customer_id == "INSTITUTION":
        raise CustomerIsolationError(f"Unauthorized or unknown customer '{customer_id}'.")

    balance_minor = get_account_balance_minor(session, account.account_id)
    items = list_customer_incoming_items(session, customer_id)

    return CustomerHomeSummary(
        customer_id=account.customer_id,
        customer_name=account.customer_name,
        account_id=account.account_id,
        account_number=account.account_number,
        sort_code=account.sort_code,
        currency=account.currency,
        available_balance_minor=balance_minor,
        formatted_balance=format_gbp_minor(balance_minor),
        incoming_items=items,
    )


def get_customer_incoming_item_by_id(
    session: Session,
    customer_id: str,
    item_id: str,
) -> CustomerIncomingPaymentItem:
    """Fetches a specific incoming payment or claim item, enforcing strict customer isolation."""
    items = list_customer_incoming_items(session, customer_id)
    for item in items:
        if item.item_id == item_id or item.payment_id == item_id or item.case_id == item_id:
            return item
    raise CustomerIsolationError(
        f"Item '{item_id}' does not exist or does not belong to customer '{customer_id}'."
    )


def investigate_customer_incoming_item(
    session: Session,
    customer_id: str,
    item_id: str,
) -> CustomerHelpInvestigationOutcome:
    """
    Runs the ResolveFlow LangGraph investigation + Deterministic Policy Engine for a
    customer's selected incoming payment or claim, returning only customer-safe output.
    """
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    if account is None or account.customer_id == "INSTITUTION":
        raise CustomerIsolationError(f"Unauthorized customer '{customer_id}'.")

    item = get_customer_incoming_item_by_id(session, customer_id, item_id)

    # Ensure an InvestigationCase exists for this customer item
    case: Optional[InvestigationCase] = None
    if item.case_id:
        case = session.exec(
            select(InvestigationCase).where(InvestigationCase.case_id == item.case_id)
        ).first()

    if case is None:
        # Look for an existing case matching customer + reference + amount
        case = session.exec(
            select(InvestigationCase).where(
                InvestigationCase.customer_id == customer_id,
                InvestigationCase.beneficiary_account_id == account.account_id,
                InvestigationCase.reference == item.reference,
                InvestigationCase.amount_minor == item.amount_minor,
            )
        ).first()

    if case is None:
        short_id = uuid4().hex[:6].upper()
        case = InvestigationCase(
            case_id=f"CASE-{short_id}",
            customer_id=customer_id,
            beneficiary_account_id=account.account_id,
            sender_bank=item.sender_bank,
            reference=item.reference,
            amount_minor=item.amount_minor,
            currency=item.currency,
            state=CaseState.OPEN,
            latest_evidence_version=1,
            processed_evidence_version=0,
        )
        session.add(case)
        session.commit()
        session.refresh(case)

    policy_result = run_case_investigation(session=session, case_id=case.case_id)
    new_balance_minor = get_account_balance_minor(session, account.account_id)

    # Translate internal policy result into strictly customer-safe messaging
    if policy_result.execution_status == "EXECUTED":
        title = "Payment Verified & Credited"
        msg = (
            f"We verified your incoming {format_gbp_minor(item.amount_minor)} transfer from "
            f"{item.sender_bank} (ref: {item.reference}) and credited {format_gbp_minor(item.amount_minor)} "
            f"to your ResolveFlow account ({account.account_number})."
        )
        credited = True
    elif policy_result.execution_status == "IDEMPOTENT_NOOP":
        title = "Payment Already Credited"
        msg = (
            f"We confirmed that this {format_gbp_minor(item.amount_minor)} transfer "
            f"(ref: {item.reference}) was already credited to your ResolveFlow balance. "
            f"No duplicate posting was made."
        )
        credited = False
    elif policy_result.proposed_action == "WAIT_AND_RECHECK":
        title = "Payment Pending Settlement"
        msg = (
            f"Our check confirmed that {item.sender_bank} transfer {item.reference} "
            f"({format_gbp_minor(item.amount_minor)}) is still marked as PENDING on the payment rail. "
            f"ResolveFlow cannot credit your account before settlement completes. "
            "We are monitoring for settlement."
        )
        credited = False
    elif policy_result.proposed_action == "TRACE_WITH_PROVIDER":
        title = "No Receipt Found — Provider Trace Opened"
        msg = (
            f"We searched our clearing records for reference {item.reference} "
            f"({format_gbp_minor(item.amount_minor)} from {item.sender_bank}) and found no "
            f"confirmed settlement receipt at ResolveFlow. No funds have arrived yet. "
            "We have opened a trace with the payment rail."
        )
        credited = False
    else:
        # AWAITING_HUMAN_APPROVAL (Ambiguous beneficiary, restricted account, high-value, or failed/reversed)
        if item.provider_status in ("FAILED", "REVERSED"):
            title = f"Payment {item.provider_status.title()} on Rail — Escalated for Support Review"
            msg = (
                f"Our records show that transfer {item.reference} ({format_gbp_minor(item.amount_minor)} "
                f"from {item.sender_bank}) was marked as {item.provider_status} by the payment rail. "
                f"Because no funds settled with ResolveFlow, we cannot credit your account. "
                "Our specialist team has been notified."
            )
        else:
            title = "Routed to Payment Specialist Team"
            msg = (
                f"We located an incoming {format_gbp_minor(item.amount_minor)} transfer record "
                f"for reference {item.reference}, and it requires manual verification by our "
                f"payment specialist team under case #{case.case_id}. We will update your case "
                f"as soon as our team completes their review."
            )
        credited = False

    return CustomerHelpInvestigationOutcome(
        case_id=case.case_id,
        case_state=policy_result.case_state,
        payment_id=policy_result.matched_payment_id,
        execution_status=policy_result.execution_status,
        credited_to_customer=credited,
        new_balance_minor=new_balance_minor,
        formatted_new_balance=format_gbp_minor(new_balance_minor),
        customer_safe_title=title,
        customer_safe_message=msg,
    )


def start_contextual_payment_chat(
    session: Session,
    customer_id: str,
    selected_item: Optional[CustomerIncomingPaymentItem] = None,
) -> Dict[str, Any]:
    """
    Creates or resumes a customer chat session pre-loaded with the selected incoming
    payment's context (amount, sender bank, reference, and current status).
    """
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    if account is None or account.customer_id == "INSTITUTION":
        raise CustomerIsolationError(f"Unauthorized customer '{customer_id}'.")

    chat_sess_resp = create_chat_session(
        session=session,
        customer_id=customer_id,
        beneficiary_account_id=account.account_id,
    )
    chat_session = session.exec(
        select(ChatSession).where(ChatSession.session_id == chat_sess_resp.session_id)
    ).first()

    if selected_item is not None and chat_session is not None:
        chat_session.draft_sender_bank = selected_item.sender_bank
        chat_session.draft_amount_minor = selected_item.amount_minor
        chat_session.draft_reference = selected_item.reference
        if selected_item.case_id:
            chat_session.active_case_id = selected_item.case_id
        else:
            # Avoid binding to a different payment's pre-existing open case
            matching_case = session.exec(
                select(InvestigationCase).where(
                    InvestigationCase.customer_id == customer_id,
                    InvestigationCase.beneficiary_account_id == account.account_id,
                    InvestigationCase.reference == selected_item.reference,
                    InvestigationCase.amount_minor == selected_item.amount_minor,
                )
            ).first()
            chat_session.active_case_id = (
                matching_case.case_id if matching_case else None
            )
        session.add(chat_session)
        session.commit()

        opening_greeting = (
            f"Hi {account.customer_name.split()[0]}, I have your selected payment in front of me: "
            f"{selected_item.formatted_amount} from {selected_item.sender_bank} "
            f"(Reference: {selected_item.reference}, Rail status: {selected_item.rail_status_label}, "
            f"Account status: {selected_item.account_status_label}). "
            f"Would you like me to investigate this payment now, or do you have additional details to share?"
        )
    else:
        if chat_session is not None:
            chat_session.active_case_id = None
            chat_session.draft_sender_bank = None
            chat_session.draft_amount_minor = None
            chat_session.draft_reference = None
            session.add(chat_session)
            session.commit()
        opening_greeting = (
            f"Hi {account.customer_name.split()[0]}, I can help check on a missing incoming bank transfer "
            f"to your ResolveFlow account ({account.account_number}). "
            f"Please tell me the amount, the sending bank (e.g. Monzo, Barclays), and the payment reference."
        )

    return {
        "session_id": chat_sess_resp.session_id,
        "opening_greeting": opening_greeting,
        "selected_item": selected_item,
    }


def send_contextual_chat_turn(
    session: Session,
    customer_id: str,
    session_id: str,
    user_message: str,
) -> Dict[str, Any]:
    """
    Processes a customer chat message within the terminal help flow:
    - Stores customer statements as versioned, untrusted evidence (`CustomerEvidenceClaim`).
    - Never treats a customer claim as proof that funds settled.
    - When all required clues (amount + reference) are present, runs the deterministic
      investigation & policy engine and returns a grounded, customer-safe response.
    """
    chat_session = session.exec(
        select(ChatSession).where(ChatSession.session_id == session_id)
    ).first()
    if chat_session is None or chat_session.customer_id != customer_id:
        raise CustomerIsolationError("Unauthorized access to chat session.")

    # Use the spec-08 conversation service to record message, extract clues, and version evidence
    msg_resp = handle_customer_message(
        session=session,
        session_id=session_id,
        customer_id=customer_id,
        content=user_message,
    )

    # If required slots are still missing, return the targeted follow-up question
    if msg_resp.missing_slots or not msg_resp.active_case_id:
        return {
            "agent_reply": msg_resp.agent_reply,
            "case_id": None,
            "case_state": None,
            "evidence_version": None,
            "investigation_ran": False,
        }

    # We have an active case! Run the LangGraph trace + Deterministic Policy Engine
    case_id = msg_resp.active_case_id
    policy_res = run_case_investigation(session=session, case_id=case_id)
    account = session.exec(
        select(CustomerAccount).where(CustomerAccount.customer_id == customer_id)
    ).first()
    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()

    balance_str = format_gbp_minor(
        get_account_balance_minor(session, account.account_id) if account else 0
    )
    amt_str = format_gbp_minor(case.amount_minor if case else 0)
    ref_str = case.reference if case else ""
    bank_str = (case.sender_bank if case and case.sender_bank else "the sending bank")

    if policy_res.execution_status == "EXECUTED":
        grounded_reply = (
            f"Good news — I checked our clearing records for reference {ref_str} "
            f"and confirmed that {amt_str} from "
            f"{bank_str} had settled in our suspense queue and matched your active ResolveFlow "
            f"account. Our policy engine has now credited {amt_str} to your account. "
            f"Your updated available balance is {balance_str}."
        )
    elif policy_res.execution_status == "IDEMPOTENT_NOOP":
        grounded_reply = (
            f"I checked reference {ref_str} and confirmed that {amt_str} was already credited "
            f"to your ResolveFlow account. Your available balance is {balance_str}."
        )
    elif policy_res.proposed_action == "WAIT_AND_RECHECK":
        grounded_reply = (
            f"I checked our payment rail records for reference {ref_str}. "
            f"{bank_str} has initiated this {amt_str} transfer, but its rail status is still PENDING. "
            f"ResolveFlow cannot credit your account before final settlement is confirmed. "
            "We are monitoring the transfer and will update you when the provider confirms settlement."
        )
    elif policy_res.proposed_action == "TRACE_WITH_PROVIDER":
        grounded_reply = (
            f"I recorded your details and searched our "
            f"incoming settlement records for {amt_str} with reference {ref_str} from {bank_str}. "
            f"ResolveFlow has not received a confirmed settlement receipt for this payment yet, "
            "so no credit can be posted. I have opened a provider trace."
        )
    else:
        # HUMAN_REVIEW (Ambiguous, Restricted, High-Value, or Reversed/Failed)
        if policy_res.provider_status in ("FAILED", "REVERSED"):
            grounded_reply = (
                f"I checked our rail records for reference {ref_str} ({amt_str} from {bank_str}). "
                f"The payment rail shows this transfer as {policy_res.provider_status}, meaning "
                f"no settled funds remain with ResolveFlow to credit your account. "
                "Our specialist team can help if you need a formal trace letter."
            )
        else:
            grounded_reply = (
                f"I recorded your details and located a "
                f"matching incoming transfer record for {amt_str} (reference {ref_str}) in our "
                f"clearing queue. Because this payment requires manual verification under our "
                "safety policy, I have routed this to our human payment-review team."
            )

    # Persist the grounded agent update into ChatMessage history
    session.add(
        ChatMessage(
            message_id=f"MSG-{uuid4().hex[:8].upper()}",
            session_id=session_id,
            sender_role=MessageSenderRole.AGENT,
            content=grounded_reply,
            evidence_version=msg_resp.evidence_version,
            created_at=get_utc_now(),
        )
    )
    session.commit()

    return {
        "agent_reply": grounded_reply,
        "case_id": case_id,
        "case_state": policy_res.case_state,
        "evidence_version": msg_resp.evidence_version,
        "execution_status": policy_res.execution_status,
        "investigation_ran": True,
    }
