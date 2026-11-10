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

## Interactive Terminal Flow & Screen Walkthrough

Here is what each screen looks like when you run `python main.py`, including exactly what happens behind the scenes at every step:

### Screen 1: First-Time Setup & Onboarding
On initial launch, the user creates Benjamin's profile and sets a 4-digit PIN.

```text
========================================================================
  RESOLVEFLOW / LENFI UK — FIRST-TIME CUSTOMER SETUP (MVP DEMO)
========================================================================
  Welcome to ResolveFlow (LenFi UK). Let's set up your receiving bank
  profile and choose a 4-digit PIN. (Note: Demo authentication only;
  your PIN is salted and hashed with PBKDF2-SHA256 in SQLite.)

  Full Name [Press Enter for 'Benjamin Chidera']: Benjamin Chidera
  Choose a 4-digit numeric PIN (or '0' to exit): 2468
  Confirm your 4-digit PIN: 2468

------------------------------------------------------------------------
  ACCOUNT SETUP CONFIRMED
------------------------------------------------------------------------
  Account Holder:    Benjamin Chidera
  Receiving Bank:    ResolveFlow / LenFi UK
  Account Number:    84920101
  Sort Code:         04-00-75
  Internal Ref:      ACC-LEN-001
  PIN Security:      PBKDF2-HMAC-SHA256 hash stored (plain PIN discarded)
------------------------------------------------------------------------
```
* **What happens**: The PIN is salted (`secrets.token_hex(16)`) and hashed using `PBKDF2-HMAC-SHA256` with `120,000` iterations. The plain PIN is immediately discarded. A stable mock account number (`84920101`) and sort code (`04-00-75`) are provisioned. On future launches, Benjamin enters this PIN to sign in (or types `R` to reset).

---

### Screen 2: Customer Home Screen
After signing in, Benjamin sees his live account balance and recent incoming transfers.

```text
========================================================================
  RESOLVEFLOW (LENFI UK) — PERSONAL ACCOUNT HOME
========================================================================
  Customer:          Benjamin Chidera
  Available Balance: £150.00
  Account Details:   Account No: 84920101  |  Sort Code: 04-00-75
------------------------------------------------------------------------
  RECENT INCOMING-PAYMENT ACTIVITY:
    1. £150.00    | From: Monzo     | Ref: MON-11223  | Credited to your account
    2. £500.00    | From: Monzo     | Ref: MON-78291  | Awaiting allocation
    3. £250.00    | From: Monzo     | Ref: MON-99881  | Reported missing (Unverified)
    4. £180.00    | From: Barclays  | Ref: BARC-44910 | Awaiting rail settlement
    5. £95.00     | From: Revolut   | Ref: REV-66201  | Not credited (Failed on rail)
========================================================================
  MAIN NAVIGATION:
    1. Home
    2. Incoming payments
    3. Help
    4. Log out
    9. Reset demo data (Presenter option)
    0. Exit
------------------------------------------------------------------------
```
* **What happens**: The system queries SQLite and computes Benjamin's balance strictly from double-entry ledger legs (`sum(credits) - sum(debits)`). It starts at `£150.00` from his previously settled and allocated incoming transfer.

---

### Screen 3: Incoming Payments & Claims List
Pressing `2` displays all incoming transfers and claims scoped strictly to Benjamin.

```text
========================================================================
  INCOMING PAYMENTS & CLAIMS (BENJAMIN CHIDERA)
========================================================================
  [1] £150.00    | From: Monzo     | Ref: MON-11223  | Rail: Settled                      | Credited to your account
  [2] £500.00    | From: Monzo     | Ref: MON-78291  | Rail: Settled                      | Awaiting allocation
  [3] £250.00    | From: Monzo     | Ref: MON-99881  | Rail: No confirmed receipt         | Reported missing (Unverified)
  [4] £180.00    | From: Barclays  | Ref: BARC-44910 | Rail: Pending                      | Awaiting rail settlement
  [5] £95.00     | From: Revolut   | Ref: REV-66201  | Rail: Failed                       | Not credited (Failed on rail)
  [6] £210.00    | From: HSBC      | Ref: HSBC-88319 | Rail: Reversed / Returned          | Not credited (Reversed by sender bank)
  [7] £1,200.00  | From: Monzo     | Ref: MON-33112  | Rail: Settled (Ref verification)   | Awaiting verification
  [0] Back to Home
------------------------------------------------------------------------
```
* **What happens**: Resolves external mock rail statuses without touching external accounts. Notice payment `[2]`: the rail status is `Settled`, but account status is `Awaiting allocation` because the funds are held safely in `ACC-UNALLOCATED-SUSPENSE`.

---

### Screen 4: Payment Details & One-Click Safe Investigation
Selecting payment `2` opens the detail view with plain-language explanations and actions.

```text
========================================================================
  £500.00 incoming payment
------------------------------------------------------------------------
  From:           Monzo
  Reference:      MON-78291
  Date:           2026-11-10 11:28 UTC
  Rail status:    Settled
  Account status: Awaiting allocation
  Linked Case:    #CASE-S1-001 (OPEN)
------------------------------------------------------------------------
  ResolveFlow has received £500.00 from Monzo (ref: MON-78291) into clearing suspense,
  but it has not been allocated to your balance yet.
========================================================================
  [1] Get help with this payment (Run ResolveFlow Investigation)
  [2] Chat with us
  [0] Back

  Select action > 1

------------------------------------------------------------------------
  INVESTIGATION UPDATE: Payment Verified & Credited
------------------------------------------------------------------------
  We verified your incoming £500.00 transfer from Monzo (ref: MON-78291)
  and credited £500.00 to your ResolveFlow account (84920101).
  Case ID: #CASE-S1-001  |  Case Status: RESOLVED  |  Available Balance: £650.00
------------------------------------------------------------------------
```
* **What happens**:
  1. The 4-node LangGraph pipeline traces the payment and verifies that `PAY-S1-001` is settled in suspense.
  2. The **Deterministic Policy Engine** confirms all 5 financial invariants pass.
  3. A double-entry journal entry is posted:
     - `Debit ACC-UNALLOCATED-SUSPENSE: £500.00` (-Liability)
     - `Credit ACC-LEN-001: £500.00` (+Customer Liability)
     - `idempotency_key = "ALLOC-PAY-S1-001"` (physically preventing double-credit on replay)
  4. Benjamin's live available balance increases from **£150.00** to **£650.00**.

---

### Screen 5: Contextual AI Agent Chat
Selecting `[2] Chat with us` or navigating through `3. Help` opens the conversational assistant with the payment context already loaded.

```text
========================================================================
  RESOLVEFLOW PAYMENT INVESTIGATION ASSISTANT
========================================================================
  Agent: Hi Benjamin, I have your selected payment in front of me: £500.00
  from Monzo (Reference: MON-78291, Rail status: Settled, Account status: Credited).
  Would you like me to investigate this payment now, or do you have additional details?
------------------------------------------------------------------------
  Type your message below (or type 'back' / '0' to return):

  You > I also sent £300 from Monzo with reference MON-44331 yesterday

  Agent: I recorded your details as evidence v1 and searched our incoming
  settlement records for £300.00 with reference MON-44331 from Monzo.
  ResolveFlow has not received a confirmed settlement receipt for this payment yet,
  so no credit can be posted. I have opened a provider trace under Case #CASE-4FA802
  (status: WAITING_PROVIDER).
  [Case #CASE-4FA802 | Status: WAITING_PROVIDER | Evidence v1]
```
* **What happens**:
  1. The agent extracts slot values (`£300.00`, `Monzo`, `MON-44331`).
  2. The claim is recorded in SQLite as an **untrusted evidence clue** (`CustomerEvidenceClaim v1`), never as proof of settlement.
  3. The agent searches clearing records and finds no matching receipt.
  4. The system strictly refuses to credit the account and marks the case `WAITING_PROVIDER`.

---

### Possible Investigation Outcomes Supported by the Engine

| Outcome | Trigger Condition | Engine Decision | Balance Effect |
| :--- | :--- | :--- | :--- |
| **Settled & Uniquely Matched** | Wire settled in suspense; reference uniquely matches active account | `AUTO_ALLOCATE` via policy engine | Reclassified from Suspense to Customer (`+£500.00`) |
| **Pending Rail Settlement** | Rail provider event marked `PENDING` | `WAIT_AND_RECHECK` (`WAITING_PROVIDER`) | No credit; waits for final settlement |
| **No Receipt Found** | Customer claims payment, but no wire received | `TRACE_WITH_PROVIDER` (`WAITING_PROVIDER`) | No credit; trace opened with provider |
| **Failed / Reversed** | Rail provider event marked `FAILED` or `REVERSED` | `HUMAN_REVIEW` (Support queue) | No credit; honest status explanation |
| **Ambiguous Beneficiary** | Wire settled in suspense, but reference matches multiple customers | `HUMAN_REVIEW` (`AWAITING_REVIEW`) | Funds held in suspense; operator disambiguates |
| **Restricted / Frozen Account** | Target account has compliance hold | `HUMAN_REVIEW` (`AWAITING_REVIEW`) | Blocked; internal hold reasons never leaked |
| **Duplicate Replay** | Same webhook or repeat investigation request | `IDEMPOTENT_NOOP` | No second credit; returns existing journal |

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

