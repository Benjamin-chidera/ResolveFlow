# Spec 04: Investigation & Reconciliation LangGraph Workflow

## 1. Overview
This specification details the LangGraph state machine orchestrating the investigation of missing incoming payments. Rather than disparate independent agents, the architecture employs a single persisted workflow with specialized sequential nodes and scoped read-only tools.

---

## 2. LangGraph Architecture & Node Pipeline

```mermaid
flowchart LR
    Start([START]) --> TraceNode[1. Payment Trace Node]
    TraceNode --> ReconNode[2. Reconciliation Node]
    ReconNode --> ExplainNode[3. Investigation / Explanation Node\n(Ollama LLM)]
    ExplainNode --> ProposalNode[4. Resolution Proposal Node]
    ProposalNode --> PolicyGate{5. Policy Evaluation}
    PolicyGate -- Safe Auto-Allocation --> ExecuteNode[Execute Ledger Allocation]
    PolicyGate -- Ambiguity / Freeze / >£10k --> InterruptNode[Interrupt: Persist Approval Request]
    ExecuteNode --> End([END])
    InterruptNode --> End([END])
```

---

## 3. Workflow State Schema (`InvestigationWorkflowState`)

```python
class InvestigationWorkflowState(TypedDict):
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
    raw_reference: Optional[str]
    sender_bank: Optional[str]
    funds_in_suspense: bool
    trace_events: List[Dict[str, Any]]

    # Node 2: Reconciliation Outputs
    exact_reference_match: bool
    amount_matches: bool
    candidate_account_ids: List[str]
    is_account_active: bool
    prior_credit_detected: bool
    reconciliation_flags: List[str]

    # Node 3: Explanation Outputs (Ollama)
    investigation_summary: str
    suspected_root_cause: str

    # Node 4: Proposal Outputs
    proposed_action: ProposedAction  # AUTO_ALLOCATE, WAIT_AND_RECHECK, TRACE_WITH_PROVIDER, HUMAN_REVIEW
    evidence_ids: List[str]
    requires_human_approval: bool
    policy_approved: bool

    # Audit Trail Log Reducer
    audit_entries: Annotated[List[Dict[str, Any]], merge_list]
```

---

## 4. Node Responsibilities & Allowed Scoped Tools

### Node 1: Payment Trace Node
- **Responsibility**: Query internal database tables to discover whether funds have actually arrived on the clearing rail.
- **Allowed Tools (Read-Only)**:
  1. `get_case(case_id)`: Fetches complaint details.
  2. `get_internal_incoming_payments(reference, amount_minor)`:
     - Exact match query on `provider_reference` or `raw_reference`.
     - Fallback query for unallocated receipts of matching amount and currency received within $\pm 24$ hours.
  3. `get_event_history(payment_id)`: Retrieves provider webhooks.
  4. `get_ledger_entries(account_id, payment_id)`: Checks suspense postings.
- **Output**: Populates `matched_payment_id`, `provider_status`, `funds_in_suspense`.

---

### Node 2: Reconciliation Node (Pure Deterministic Logic)
- **Responsibility**: Compares the customer's claim against authoritative bank records using strict mathematical and boolean invariants.
- **Rule Matrix**:
  1. **Receipt Existence**: If `matched_payment_id` is null $\rightarrow$ Flag: `RECEIPT_ABSENT`.
  2. **Amount Consistency**: Compare `claimed_amount_minor == payment.amount_minor`. If mismatch $\rightarrow$ Flag: `AMOUNT_MISMATCH`.
  3. **Beneficiary Ambiguity Check**:
     - Extract name/account indicators from `raw_reference`.
     - Query `customers_accounts` for matches.
     - If $> 1$ account matches or names conflict $\rightarrow$ Flag: `AMBIGUOUS_BENEFICIARY`.
  4. **Account State**:
     - Query target `customers_accounts.status`.
     - If not `ACTIVE` (e.g. `FROZEN`) $\rightarrow$ Flag: `ACCOUNT_RESTRICTED`.
  5. **Prior Credit (Replay Check)**:
     - Check `incoming_payments.allocation_status == 'ALLOCATED'`.
     - Check if customer account already received a credit leg linked to this `payment_id`.
     - If yes $\rightarrow$ Flag: `DUPLICATE_CREDIT_DETECTED`.

---

### Node 3: Investigation / Explanation Node (Ollama LLM)
- **Responsibility**: Synthesizes the forensic facts into human-readable explanations for operations staff and customer communications.
- **Guardrail**: The LLM **cannot invent facts or provider statuses**. It is strictly conditioned on the output of Node 1 and Node 2.

#### Ollama Prompt Specification
```text
SYSTEM:
You are an expert financial payment operations analyst investigating a missing incoming transfer for LenFi.
Analyze ONLY the provided verified facts. Never assume or invent provider statuses, transactions, or bank policies.

VERIFIED FACTS:
- Case Reference Claimed: {claimed_reference}
- Claimed Amount: £{claimed_amount} ({currency})
- Target Account: {beneficiary_account_id} (Status: {account_status})
- Matched Rail Receipt: {matched_payment_id} (Status: {provider_status})
- Reconciliation Flags: {reconciliation_flags}
- Funds currently in unallocated suspense: {funds_in_suspense}

TASK:
1. State the exact observed factual status of the payment.
2. If missing or held, explain the root cause concisely without speculation.
3. Recommend the operational resolution.
```

---

### Node 4: Resolution Proposal Node
- **Responsibility**: Translates reconciliation findings and LLM summary into a standardized, structured proposal:
  - `AUTO_ALLOCATE`: Exact receipt found, active account, no prior credit, under threshold.
  - `WAIT_AND_RECHECK`: Provider reports payment `PENDING` on rail.
  - `TRACE_WITH_PROVIDER`: No authoritative receipt found (Scenario S2).
  - `HUMAN_REVIEW`: Ambiguous beneficiary (S3), account frozen (S5), or high amount.

---

## 5. Persistence & Checkpointing
The LangGraph workflow utilizes SQLite checkpointer (`SqliteSaver`). Every node transition commits the complete state snapshot to SQLite under `thread_id = case_id`. This guarantees:
1. Resumability across server restarts.
2. Safe pausing for human operator review (LangGraph `interrupt()`).
3. Auditability of every intermediate reasoning step.

