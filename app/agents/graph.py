from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from sqlmodel import Session, select
from app.agents.nodes import (
    explanation_node,
    make_payment_trace_node,
    make_reconciliation_node,
    resolution_proposal_node,
)
from app.agents.state import InvestigationWorkflowState
from app.models.case import InvestigationCase
from app.services.policy_engine import (
    PolicyExecutionResult,
    evaluate_and_execute_policy,
)

# Shared checkpointer persisting node checkpoints keyed by thread_id = case_id
workflow_checkpointer = MemorySaver()


def build_investigation_workflow(session: Session):
    """Constructs and compiles the 4-node ResolveFlow LangGraph workflow (Spec 04)."""
    builder = StateGraph(InvestigationWorkflowState)

    builder.add_node("payment_trace", make_payment_trace_node(session))
    builder.add_node("reconciliation", make_reconciliation_node(session))
    builder.add_node("explanation", explanation_node)
    builder.add_node("resolution_proposal", resolution_proposal_node)

    builder.add_edge(START, "payment_trace")
    builder.add_edge("payment_trace", "reconciliation")
    builder.add_edge("reconciliation", "explanation")
    builder.add_edge("explanation", "resolution_proposal")
    builder.add_edge("resolution_proposal", END)

    return builder.compile(checkpointer=workflow_checkpointer)


def run_case_investigation(
    session: Session,
    case_id: str,
) -> PolicyExecutionResult:
    """Executes the LangGraph investigation pipeline for a case and hands the

    resulting proposal to the Deterministic Policy Engine for authorization/execution.
    """
    case = session.exec(
        select(InvestigationCase).where(InvestigationCase.case_id == case_id)
    ).first()

    if case is None:
        raise ValueError(f"Case '{case_id}' not found.")

    initial_state: InvestigationWorkflowState = {
        "case_id": case.case_id,
        "customer_id": case.customer_id,
        "beneficiary_account_id": case.beneficiary_account_id,
        "claimed_reference": case.reference,
        "claimed_amount_minor": case.amount_minor,
        "currency": case.currency,
        "matched_payment_id": None,
        "provider_status": None,
        "allocation_status": None,
        "raw_reference": None,
        "sender_bank": None,
        "receipt_amount_minor": None,
        "funds_in_suspense": False,
        "trace_events": [],
        "exact_reference_match": False,
        "amount_matches": False,
        "candidate_account_ids": [],
        "account_status": None,
        "is_account_active": False,
        "prior_credit_detected": False,
        "reconciliation_flags": [],
        "investigation_summary": "",
        "suspected_root_cause": "",
        "proposed_action": None,
        "evidence_ids": [],
        "requires_human_approval": False,
        "policy_approved": False,
        "duplicate_noop": False,
        "audit_entries": [],
    }

    graph = build_investigation_workflow(session=session)
    config = {"configurable": {"thread_id": case.case_id}}
    final_state: InvestigationWorkflowState = graph.invoke(initial_state, config=config)

    return evaluate_and_execute_policy(
        session=session,
        case=case,
        workflow_state=final_state,
    )

