from typing import Annotated, Any, Dict, List, Optional
from typing_extensions import TypedDict
from app.models.enums import ProposedAction


def merge_list(current: List[Any], new_items: List[Any]) -> List[Any]:
    """Reducer to append new audit entries onto the workflow state list."""
    return (current or []) + (new_items or [])


class InvestigationWorkflowState(TypedDict):
    """Shared state across the ResolveFlow AI 4-node LangGraph workflow (Spec 04)."""

    # Case Input Context
    case_id: str
    customer_id: str
    beneficiary_account_id: str
    claimed_reference: str
    claimed_amount_minor: int
    currency: str

    # Node 1: Payment Trace Outputs
    matched_payment_id: Optional[str]
    provider_status: Optional[str]
    allocation_status: Optional[str]
    raw_reference: Optional[str]
    sender_bank: Optional[str]
    receipt_amount_minor: Optional[int]
    funds_in_suspense: bool
    trace_events: List[Dict[str, Any]]

    # Node 2: Reconciliation Outputs
    exact_reference_match: bool
    amount_matches: bool
    candidate_account_ids: List[str]
    account_status: Optional[str]
    is_account_active: bool
    prior_credit_detected: bool
    reconciliation_flags: List[str]

    # Node 3: Explanation Outputs (Ollama)
    investigation_summary: str
    suspected_root_cause: str

    # Node 4: Proposal Outputs
    proposed_action: Optional[ProposedAction]
    evidence_ids: List[str]
    requires_human_approval: bool
    policy_approved: bool
    duplicate_noop: bool

    # Audit Trail Log Reducer
    audit_entries: Annotated[List[Dict[str, Any]], merge_list]

