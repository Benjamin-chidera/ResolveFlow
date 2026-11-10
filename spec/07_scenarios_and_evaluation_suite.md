# Spec 07: Scenarios & Automated Evaluation Suite

## 1. Overview
This specification defines the acceptance criteria, test assertions, and evaluation benchmark for ResolveFlow AI. The test suite exercises all six core synthetic scenarios (S1–S6) and asserts hard banking invariants.

---

## 2. The 6 Core Test Scenarios Matrix

| Scenario | Input & Initial Conditions | Expected Node Path | Expected Decision | Final Ledger State |
| :--- | :--- | :--- | :--- | :--- |
| **S1. Received, Not Allocated** | Wire £500 arrived from Monzo, sitting in Suspense. Customer reports missing £500 (`CASE-S1-001`). | Trace $\rightarrow$ Reconcile $\rightarrow$ Explain $\rightarrow$ Propose $\rightarrow$ Policy | `AUTO_ALLOCATE` | Suspense debited £500, `ACC-LEN-001` credited £500. Balance net change = 0. |
| **S2. Reference Given, No Receipt** | Customer reports £250 wire (`MON-99881`). Zero matching receipts exist on rail. | Trace $\rightarrow$ Reconcile $\rightarrow$ Explain $\rightarrow$ Propose $\rightarrow$ Policy | `TRACE_WITH_PROVIDER` | No money moved. Zero ledger change. Case set to `WAITING_PROVIDER`. |
| **S3. Ambiguous Beneficiary** | Wire £1,200 arrived with ref `"BEN CHIDERA"`. Matches 2 customer accounts (`ACC-LEN-003`, `ACC-LEN-004`). | Trace $\rightarrow$ Reconcile $\rightarrow$ Explain $\rightarrow$ Propose $\rightarrow$ Interrupt | `HUMAN_REVIEW` | Money stays in Suspense. Graph halts with `AWAITING_REVIEW`. Operator resolves. |
| **S4. Duplicate Event / Replay** | Same provider webhook `EVT-S4-001` or customer allocation executed twice. | Ingestion / Trace $\rightarrow$ Idempotency Check | `IDEMPOTENT_NOOP` | No second credit. Exactly 1 allocation journal exists. |
| **S5. Restricted / Frozen Account** | Wire £750 arrived for account `ACC-LEN-005` (`FROZEN`). | Trace $\rightarrow$ Reconcile $\rightarrow$ Policy Gate | `HUMAN_REVIEW` | Auto-allocation BLOCKED. Routed to Compliance review. Money remains in Suspense. |
| **S6. Late Arrival** | Initial S2 search found no receipt. Provider event arrives 3 hours later. | Ingestion triggers case resumption $\rightarrow$ Re-evaluate | `AUTO_ALLOCATE` | Case transitions from `WAITING_PROVIDER` $\rightarrow$ `RESOLVED`. Suspense debited. |

---

## 3. Hard Financial Invariant Assertions

The test suite enforces the following non-negotiable assertions across every run:

### Invariant 1: Zero Duplicate Credits
```python
def test_zero_duplicate_credits_on_replay(session):
    # Replay identical event 10 times concurrently
    ...
    credits = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.source_payment_id == "PAY-S1-001",
            LedgerEntry.credit_minor > 0
        )
    ).all()
    assert len(credits) == 1, "Duplicate credit detected!"
```

### Invariant 2: 100% Balanced Double-Entry Journals
```python
def test_all_ledger_journals_balance(session):
    journals = session.exec(select(JournalEntry)).all()
    for jrn in journals:
        total_debits = sum(leg.debit_minor for leg in jrn.lines)
        total_credits = sum(leg.credit_minor for leg in jrn.lines)
        assert total_debits == total_credits, f"Journal {jrn.journal_id} is out of balance!"
```

### Invariant 3: Zero Allocations When Authoritative Receipt is Missing
```python
def test_scenario_s2_strictly_blocks_credit(client):
    # Run S2 case
    res = client.post("/api/v1/investigations/CASE-S2-002/run")
    assert res.json()["proposed_action"] == "TRACE_WITH_PROVIDER"
    assert res.json()["execution_status"] == "NOT_EXECUTED"
```

### Invariant 4: Zero Allocations on Frozen Accounts
```python
def test_scenario_s5_strictly_blocks_frozen_account(client):
    res = client.post("/api/v1/investigations/CASE-S5-005/run")
    assert res.json()["proposed_action"] == "HUMAN_REVIEW"
    assert res.json()["account_status"] == "FROZEN"
```

---

## 4. Evaluation Metrics & Reporting
When `pytest` or evaluation runs execute, the system outputs a structured scorecard:

```text
======================= RESOLVEFLOW EVALUATION SCORECARD =======================
Scenario Pass Rate:              6/6 (100%)
Duplicate Credit Rate:           0.00% across replay tests
Ledger Invariant Compliance:     100.0% (Zero unbalanced journals)
Unauthorised Credits:            0
p95 Case Investigation Latency:  < 1.8s (Local SQLite + Ollama)
Human Review Interrupt Success:  100% (State safely persisted and resumed)
================================================================================
```

