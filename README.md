# ResolveFlow AI — Receiving-Bank Incoming Payment Investigation MVP

**ResolveFlow AI** is a bank-neutral receiving-bank demonstration for investigating missing incoming customer transfers. It can be presented to any bank or financial institution: customers log in to check whether payments sent from external banks have arrived, inspect payment statuses, and get help from an evidence-grounded investigation agent.

External banks and payment rails are modelled strictly as interchangeable mock evidence sources (`SETTLED`, `PENDING`, `FAILED`, `REVERSED`, or no confirmed receipt). The seeded bank names are example data only, not product integrations or endorsements.

---

## Quickstart: Run the Interactive Terminal MVP

### 1. Install Dependencies & Seed the SQLite Database
```bash
uv add -r requirements.txt
uv run python seed.py
```

### 2. Launch the Interactive Terminal App
```bash
uv run python main.py
# Or inside your activated .venv:
python main.py
```

---

## Walkthrough of the Terminal Demo Flow

1. **First-Time Setup & PIN Sign-In**:
   - On first launch, confirm the primary customer profile (**Benjamin Chidera**) and choose a 4-digit numeric PIN.
   - Your PIN is salted and hashed with **PBKDF2-HMAC-SHA256** (`120,000` iterations) in SQLite; plain-text PINs are never stored or logged.
   - You receive your stable ResolveFlow account details:
     - **Account Number**: `84920101`
     - **Sort Code**: `04-00-75`
     - **Internal Account ID**: `ACC-LEN-001`
   - On subsequent visits, enter your 4-digit PIN to sign in (or type `R` to reset the demo data).

2. **Customer Home Screen**:
   - Displays **Benjamin Chidera**'s available balance (initially `£150.00` from his already-allocated incoming transfer), his ResolveFlow account number and sort code, and his recent incoming-payment activity.
   - Navigation options:
     - `1. Home`
     - `2. Incoming payments`
     - `3. Help`
     - `4. Log out`
     - `9. Reset demo data (Presenter option)`
     - `0. Exit`

3. **Incoming Payments & Seeded Presentation Scenarios**:
   Selecting `2. Incoming payments` shows Benjamin's own incoming payments and claims:
   1. **Already Allocated (`£150.00` from Monzo, `MON-11223`)**: Settled and credited to Benjamin's balance.
   2. **Received in Suspense, Not Yet Allocated (`£500.00` from Monzo, `MON-78291`)**: Rail status is `Settled`, account status is `Awaiting allocation`. Selecting `[1] Get help with this payment` or `[2] Chat with us` runs the 4-node LangGraph trace & reconciliation workflow and the Deterministic Policy Engine, reclassifying `£500.00` from `ACC-UNALLOCATED-SUSPENSE` to `ACC-LEN-001` and updating Benjamin's balance to `£650.00`.
   3. **Pending Incoming Payment (`£180.00` from Barclays, `BARC-44910`)**: Rail status is `Pending`. ResolveFlow explains that it cannot credit the account before final rail settlement.
   4. **Failed Incoming Payment (`£95.00` from Revolut, `REV-66201`)**: Rail status is `Failed`. No funds settled with ResolveFlow; zero credit posted.
   5. **Reversed Incoming Payment (`£210.00` from HSBC, `HSBC-88319`)**: Rail status is `Reversed / Returned`. Recalled by the sending bank; zero credit posted.
   6. **No Incoming Receipt Found (`£250.00` from Monzo, `MON-99881`)**: Benjamin claims he sent `£250.00`, but ResolveFlow has no matching settlement receipt. Opens a provider trace (`WAITING_PROVIDER`) with zero credit.
   7. **Ambiguous Beneficiary (`£1,200.00` from Monzo, `MON-33112`)**: Arrived in Suspense with raw reference `"MON-33112 BEN CHIDERA"`, matching multiple profiles. Funds stay safely in Suspense and route to the human payment-review team (`AWAITING_REVIEW`).
   8. **Restricted / Frozen Account (`PAY-S5-005`, `ACC-LEN-005`)**: Auto-allocation is strictly blocked by the Policy Engine and routed to human review without leaking internal compliance flags.

4. **Help Menu & Contextual Agent Chat**:
   - Selecting `3. Help` $\rightarrow$ `1. Help with an incoming bank transfer` lets Benjamin pick any payment or report a missing payment (`[M]`).
   - Contextual chat starts with the selected payment already loaded, records customer statements as versioned untrusted evidence (`CustomerEvidenceClaim`), and never converts an unverified claim into a balance credit.

---

## Visual Terminal Flow Preview

```text
WELCOME TO RESOLVEFLOW / LENFI UK
─────────────────────────────────
First-time setup
Name: Benjamin Chidera
Choose PIN: ••••
Confirm PIN: ••••

Account created
Account number: 84920101
Sort code:      04-00-75

HOME
─────────────────────────────────
Benjamin Chidera
Available balance: £150.00

Recent incoming payments
• £150.00 from Monzo     Credited
• £500.00 from Monzo     Awaiting allocation
• £180.00 from Barclays  Pending

1. Home
2. Incoming payments
3. Help
4. Log out
9. Reset demo data
0. Exit

INCOMING PAYMENT → £500 from Monzo
─────────────────────────────────
Rail status: Settled
Account status: Awaiting allocation

ResolveFlow has received the payment,
but it has not yet been added to your balance.

1. Get help with this payment
2. Chat with us
0. Back

HELP → Incoming bank transfer
─────────────────────────────────
Choose a payment, or report one missing.

Agent: I can help check your £250 transfer from Monzo.
       I cannot yet find a settled receipt in our records.

You: I sent it yesterday.

Agent: I’ve opened a provider trace. Your balance will not
       change unless we receive verified settlement evidence.
```

### Possible Investigation Outcomes:
- **Settled + uniquely matched** $\rightarrow$ Safely credited to Benjamin's balance
- **Pending** $\rightarrow$ Wait for final rail settlement
- **No receipt** $\rightarrow$ Provider trace opened (`WAITING_PROVIDER`)
- **Failed / reversed** $\rightarrow$ No credit posted
- **Ambiguous / restricted** $\rightarrow$ Routed to human payment-review team

---

## Running the Automated Test Suite

```bash
# Run all automated tests
uv run pytest -v

# Run only the Receiving-Bank Terminal MVP test suite
uv run pytest -v tests/test_receiving_bank_terminal_mvp.py

# Run the Spec 07 Evaluation Scorecard with console output
uv run pytest -v -s tests/test_spec07_evaluation_suite.py
```

