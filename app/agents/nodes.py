from datetime import datetime, timezone
from typing import Any, Callable, Dict, List
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_ollama import ChatOllama
from sqlmodel import Session
from app.agents.state import InvestigationWorkflowState
from app.core.config import settings
from app.models.enums import AccountStatus, ProposedAction
from app.tools.trace_tools import (
    tool_get_event_history,
    tool_get_internal_incoming_payments,
    tool_get_ledger_reconciliation_state,
    tool_resolve_candidate_accounts,
)


def get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_payment_trace_node(session: Session) -> Callable[[InvestigationWorkflowState], Dict[str, Any]]:
    """Creates Node 1: Payment Trace Node bound to a read-only database session scope."""

    def payment_trace_node(state: InvestigationWorkflowState) -> Dict[str, Any]:
        payment, is_exact_ref = tool_get_internal_incoming_payments(
            session=session,
            reference=state["claimed_reference"],
            amount_minor=state["claimed_amount_minor"],
            currency=state["currency"],
        )

        if payment is None:
            return {
                "matched_payment_id": None,
                "provider_status": None,
                "allocation_status": None,
                "raw_reference": None,
                "sender_bank": None,
                "receipt_amount_minor": None,
                "funds_in_suspense": False,
                "exact_reference_match": False,
                "prior_credit_detected": False,
                "trace_events": [],
                "audit_entries": [
                    {
                        "actor": "PaymentTraceNode",
                        "action": "TRACE_NO_RECEIPT_FOUND",
                        "timestamp": get_utc_now_iso(),
                        "details": (
                            f"No incoming payment receipt matched reference='{state['claimed_reference']}' "
                            f"or unallocated amount={state['claimed_amount_minor']} {state['currency']}"
                        ),
                    }
                ],
            }

        events = tool_get_event_history(session=session, payment_id=payment.payment_id)
        ledger_state = tool_get_ledger_reconciliation_state(
            session=session,
            payment_id=payment.payment_id,
        )

        return {
            "matched_payment_id": payment.payment_id,
            "provider_status": payment.provider_status.value,
            "allocation_status": payment.allocation_status.value,
            "raw_reference": payment.raw_reference,
            "sender_bank": payment.sender_bank,
            "receipt_amount_minor": payment.amount_minor,
            "funds_in_suspense": ledger_state["funds_in_suspense"],
            "exact_reference_match": is_exact_ref,
            "prior_credit_detected": ledger_state["prior_credit_detected"],
            "trace_events": events,
            "audit_entries": [
                {
                    "actor": "PaymentTraceNode",
                    "action": "TRACE_RECEIPT_LOCATED",
                    "timestamp": get_utc_now_iso(),
                    "details": (
                        f"Located payment {payment.payment_id} (status={payment.provider_status.value}, "
                        f"in_suspense={ledger_state['funds_in_suspense']}, exact_ref={is_exact_ref})"
                    ),
                }
            ],
        }

    return payment_trace_node


def make_reconciliation_node(session: Session) -> Callable[[InvestigationWorkflowState], Dict[str, Any]]:
    """Creates Node 2: Reconciliation Node (Pure Deterministic Rule Matrix)."""

    def reconciliation_node(state: InvestigationWorkflowState) -> Dict[str, Any]:
        flags: List[str] = []
        matched_payment_id = state.get("matched_payment_id")
        target_account_id = state["beneficiary_account_id"]

        if matched_payment_id is None:
            flags.append("RECEIPT_ABSENT")
            return {
                "amount_matches": False,
                "candidate_account_ids": [],
                "account_status": None,
                "is_account_active": False,
                "reconciliation_flags": flags,
                "audit_entries": [
                    {
                        "actor": "ReconciliationNode",
                        "action": "RECONCILE_COMPLETE",
                        "timestamp": get_utc_now_iso(),
                        "details": f"Flags={flags}",
                    }
                ],
            }

        # 1. Provider settlement status check
        provider_status = state.get("provider_status")
        if provider_status != "SETTLED":
            flags.append("PROVIDER_STATUS_NOT_SETTLED")
            if provider_status in ("REVERSED", "FAILED"):
                flags.append("PAYMENT_REVERSED_OR_FAILED")

        # 2. Amount consistency check
        receipt_amount = state.get("receipt_amount_minor") or 0
        amount_matches = receipt_amount == state["claimed_amount_minor"]
        if not amount_matches:
            flags.append("AMOUNT_MISMATCH")

        # 3. Prior credit / replay check
        if state.get("prior_credit_detected") or state.get("allocation_status") == "ALLOCATED":
            flags.append("DUPLICATE_CREDIT_DETECTED")

        # 4. Beneficiary ambiguity check
        raw_ref = state.get("raw_reference") or ""
        candidate_ids, target_account = tool_resolve_candidate_accounts(
            session=session,
            raw_reference=raw_ref,
            claimed_beneficiary_account_id=target_account_id,
        )

        if (
            not state.get("exact_reference_match")
            or len(candidate_ids) != 1
            or candidate_ids[0] != target_account_id
        ):
            flags.append("AMBIGUOUS_BENEFICIARY")

        # 5. Beneficiary account status check
        account_status_str = target_account.status.value if target_account else "MISSING"
        is_active = target_account is not None and target_account.status == AccountStatus.ACTIVE
        if not is_active:
            flags.append("ACCOUNT_RESTRICTED")

        # 6. High-value policy threshold check
        if receipt_amount >= settings.HIGH_VALUE_THRESHOLD_CENTS:
            flags.append("HIGH_VALUE_THRESHOLD_EXCEEDED")

        return {
            "amount_matches": amount_matches,
            "candidate_account_ids": candidate_ids,
            "account_status": account_status_str,
            "is_account_active": is_active,
            "reconciliation_flags": flags,
            "audit_entries": [
                {
                    "actor": "ReconciliationNode",
                    "action": "RECONCILE_COMPLETE",
                    "timestamp": get_utc_now_iso(),
                    "details": f"Payment={matched_payment_id}, Flags={flags}",
                }
            ],
        }

    return reconciliation_node


def explanation_node(state: InvestigationWorkflowState) -> Dict[str, Any]:
    """Node 3: Investigation / Explanation Node.

    Generates an evidence-grounded summary strictly conditioned on Node 1 & Node 2 outputs.
    Never invents provider statuses or facts.
    """
    flags = state.get("reconciliation_flags", [])
    matched_id = state.get("matched_payment_id")

    # Deterministic factual root cause classification
    if "RECEIPT_ABSENT" in flags:
        root_cause = "NO_AUTHORITATIVE_PROVIDER_RECEIPT"
        fallback_summary = (
            f"No authoritative provider receipt matching reference '{state['claimed_reference']}' "
            f"({state['claimed_amount_minor']} {state['currency']}) has been received by LenFi."
        )
    elif "PAYMENT_REVERSED_OR_FAILED" in flags:
        root_cause = f"PROVIDER_PAYMENT_{state.get('provider_status')}"
        fallback_summary = (
            f"Provider record {matched_id} has status '{state.get('provider_status')}'. "
            f"Funds are not settled with ResolveFlow and cannot be credited."
        )
    elif "PROVIDER_STATUS_NOT_SETTLED" in flags:
        root_cause = "PROVIDER_SETTLEMENT_PENDING"
        fallback_summary = (
            f"Provider record {matched_id} is currently '{state.get('provider_status')}'. "
            f"ResolveFlow cannot credit the account until settlement is confirmed."
        )
    elif "DUPLICATE_CREDIT_DETECTED" in flags:
        root_cause = "ALREADY_ALLOCATED_TO_CUSTOMER"
        fallback_summary = (
            f"Payment {matched_id} was already allocated and credited; duplicate request requires no posting."
        )
    elif "ACCOUNT_RESTRICTED" in flags:
        root_cause = "BENEFICIARY_ACCOUNT_RESTRICTED"
        fallback_summary = (
            f"Receipt {matched_id} is confirmed in suspense, but beneficiary account "
            f"{state['beneficiary_account_id']} has status '{state.get('account_status')}'."
        )
    elif "AMBIGUOUS_BENEFICIARY" in flags:
        root_cause = "AMBIGUOUS_BENEFICIARY_REFERENCE"
        fallback_summary = (
            f"Receipt {matched_id} is confirmed in suspense, but the wire reference does not "
            f"uniquely identify account {state['beneficiary_account_id']} "
            f"(candidates={state.get('candidate_account_ids')})."
        )
    elif "HIGH_VALUE_THRESHOLD_EXCEEDED" in flags:
        root_cause = "EXCEEDS_AUTO_ALLOCATION_LIMIT"
        fallback_summary = (
            f"Receipt {matched_id} matches {state['beneficiary_account_id']}, but amount "
            f"{state.get('receipt_amount_minor')} exceeds automatic allocation threshold."
        )
    else:
        root_cause = "UNALLOCATED_RECEIPT_VERIFIED_IN_SUSPENSE"
        fallback_summary = (
            f"Authoritative receipt {matched_id} ({state.get('receipt_amount_minor')} {state['currency']}) "
            f"is settled in unallocated suspense and uniquely matches active account {state['beneficiary_account_id']}."
        )

    summary = fallback_summary

    # Optional Ollama synthesis strictly grounded in verified facts
    if settings.APP_ENV == "production":
        try:
            llm = ChatOllama(
                base_url=settings.OLLAMA_BASE_URL,
                model=settings.OLLAMA_MODEL,
                temperature=0.0,
            )
            prompt = (
                f"Case Reference Claimed: {state['claimed_reference']}\n"
                f"Claimed Amount Minor: {state['claimed_amount_minor']} ({state['currency']})\n"
                f"Target Account: {state['beneficiary_account_id']} (Status: {state.get('account_status')})\n"
                f"Matched Rail Receipt: {matched_id} (Status: {state.get('provider_status')})\n"
                f"Reconciliation Flags: {flags}\n"
                f"Funds in Suspense: {state.get('funds_in_suspense')}\n"
                "State the factual status and root cause in two concise sentences without speculation."
            )
            res = llm.invoke(
                [
                    SystemMessage(
                        content=(
                            "You are a financial payment operations analyst. "
                            "Use ONLY the provided verified facts. Never invent statuses."
                        )
                    ),
                    HumanMessage(content=prompt),
                ]
            )
            if res.content:
                summary = str(res.content).strip()
        except Exception:
            summary = fallback_summary

    return {
        "investigation_summary": summary,
        "suspected_root_cause": root_cause,
        "audit_entries": [
            {
                "actor": "ExplanationNode",
                "action": "EXPLANATION_GENERATED",
                "timestamp": get_utc_now_iso(),
                "details": f"RootCause={root_cause}",
            }
        ],
    }


def resolution_proposal_node(state: InvestigationWorkflowState) -> Dict[str, Any]:
    """Node 4: Resolution Proposal Node.

    Translates reconciliation flags into a structured ProposedAction.
    Note: This node ONLY proposes; the Deterministic Policy Engine owns authorization.
    """
    flags = state.get("reconciliation_flags", [])
    matched_id = state.get("matched_payment_id")
    evidence_ids: List[str] = []
    if matched_id:
        evidence_ids.append(matched_id)
    for evt in state.get("trace_events", []):
        if evt.get("event_id"):
            evidence_ids.append(evt["event_id"])

    duplicate_noop = False

    if "RECEIPT_ABSENT" in flags:
        proposed = ProposedAction.TRACE_WITH_PROVIDER
        requires_human = False
        policy_approved = False
    elif "PAYMENT_REVERSED_OR_FAILED" in flags:
        proposed = ProposedAction.HUMAN_REVIEW
        requires_human = True
        policy_approved = False
    elif "PROVIDER_STATUS_NOT_SETTLED" in flags:
        proposed = ProposedAction.WAIT_AND_RECHECK
        requires_human = False
        policy_approved = False
    elif "DUPLICATE_CREDIT_DETECTED" in flags:
        proposed = ProposedAction.AUTO_ALLOCATE
        requires_human = False
        policy_approved = True
        duplicate_noop = True
    elif any(
        f in flags
        for f in (
            "AMBIGUOUS_BENEFICIARY",
            "ACCOUNT_RESTRICTED",
            "AMOUNT_MISMATCH",
            "HIGH_VALUE_THRESHOLD_EXCEEDED",
        )
    ):
        proposed = ProposedAction.HUMAN_REVIEW
        requires_human = True
        policy_approved = False
    else:
        proposed = ProposedAction.AUTO_ALLOCATE
        requires_human = False
        policy_approved = True

    return {
        "proposed_action": proposed,
        "evidence_ids": evidence_ids,
        "requires_human_approval": requires_human,
        "policy_approved": policy_approved,
        "duplicate_noop": duplicate_noop,
        "audit_entries": [
            {
                "actor": "ResolutionProposalNode",
                "action": "PROPOSAL_CREATED",
                "timestamp": get_utc_now_iso(),
                "details": (
                    f"Proposed={proposed.value}, RequiresHuman={requires_human}, "
                    f"Evidence={evidence_ids}"
                ),
            }
        ],
    }

