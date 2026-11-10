# Spec 01: Data Model & Seed Fixtures Specification

## 1. Objective
Establish the relational schema and deterministic seed fixtures for **ResolveFlow AI**'s incoming payment investigation backend. The data model stores institutional bank accounts, raw payment rail events, double-entry financial journals, and investigation cases in SQLite.

---

## 2. Table-by-Table Data Specification

### 2.1 `customers_accounts`
Represents customer and internal institutional accounts in the receiving institution (LenFi).

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal primary key |
| `tenant_id` | TEXT | NOT NULL | Multi-tenant namespace (e.g. `"lenfi_uk"`) |
| `customer_id` | TEXT | NOT NULL, INDEX | Customer identifier (e.g. `"CUST-101"`) |
| `account_id` | TEXT | UNIQUE, NOT NULL, INDEX | Unique account code (e.g. `"ACC-LEN-001"`) |
| `account_type` | TEXT | NOT NULL | `LIABILITY` (customer deposits), `ASSET` (settlement cash), `LIABILITY` (unallocated suspense) |
| `currency` | TEXT | NOT NULL, DEFAULT 'GBP' | ISO 4217 3-letter currency code |
| `status` | TEXT | NOT NULL, DEFAULT 'ACTIVE' | `ACTIVE`, `FROZEN`, `CLOSED` |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Timestamp when account was opened |

#### Concrete Records Stored:
1. **Institutional Accounts**:
   - `account_id`: `"ACC-SETTLEMENT-CASH"` (Asset - represents real cash held at the central clearing bank)
   - `account_id`: `"ACC-UNALLOCATED-SUSPENSE"` (Liability - holds incoming money received but not yet attributed to a customer)
2. **Customer Accounts**:
   - `account_id`: `"ACC-LEN-001"`, `customer_id`: `"CUST-101"`, name: "Sarah Jenkins", `status`: `"ACTIVE"`
   - `account_id`: `"ACC-LEN-002"`, `customer_id`: `"CUST-102"`, name: "Michael Chang", `status`: `"ACTIVE"`
   - `account_id`: `"ACC-LEN-003"`, `customer_id`: `"CUST-103"`, name: "Benjamin Chidera", `status`: `"ACTIVE"`
   - `account_id`: `"ACC-LEN-004"`, `customer_id`: `"CUST-104"`, name: "Benjamin Chidera (Business)", `status`: `"ACTIVE"`
   - `account_id`: `"ACC-LEN-005"`, `customer_id`: `"CUST-105"`, name: "Arthur Pendelton", `status`: `"FROZEN"` (Compliance hold)

---

### 2.2 `incoming_payments`
Stores authoritative payment receipts delivered by the clearing rail / payment provider (e.g. Faster Payments, ClearBank, Modulr).

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal database UUID |
| `payment_id` | TEXT | UNIQUE, NOT NULL, INDEX | Internal payment reference (e.g. `"PAY-S1-001"`) |
| `provider_reference` | TEXT | NOT NULL, INDEX | External rail identifier (e.g. `"FP-2026-X881"`) |
| `sender_bank` | TEXT | NOT NULL | Name of origin bank (e.g. `"Monzo"`, `"Barclays"`) |
| `beneficiary_account_id` | TEXT | NULLABLE, INDEX | Beneficiary account parsed from reference (if identifiable) |
| `raw_reference` | TEXT | NOT NULL | Exact unparsed string passed in the wire (e.g. `"MON-78291 ACC-LEN-001"`) |
| `amount_minor` | INTEGER | NOT NULL, >= 0 | Amount in minor currency units (e.g. `50000` = £500.00) |
| `currency` | TEXT | NOT NULL, DEFAULT 'GBP' | Currency code |
| `provider_status` | TEXT | NOT NULL | `SETTLED`, `PENDING`, `REVERSED`, `FAILED` |
| `allocation_status` | TEXT | NOT NULL, INDEX | `UNALLOCATED`, `ALLOCATED`, `ON_HOLD` |
| `received_at` | TIMESTAMP | NOT NULL, UTC | Timestamp when funds cleared into our settlement account |

---

### 2.3 `payment_events`
Audit log of raw messages and state updates received from the payment provider.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Event ID |
| `event_id` | TEXT | UNIQUE, NOT NULL, INDEX | e.g. `"EVT-FP-001"` |
| `source` | TEXT | NOT NULL | Provider name: `"FASTER_PAYMENTS"`, `"CLEARBANK"` |
| `payment_id` | TEXT | NOT NULL, INDEX | Linked payment identifier |
| `event_type` | TEXT | NOT NULL | `"FUNDS_SETTLED"`, `"STATUS_UPDATED"` |
| `event_time` | TIMESTAMP | NOT NULL, UTC | Timestamp from source rail |
| `ingested_at` | TIMESTAMP | NOT NULL, UTC | Timestamp when LenFi ingested the event |
| `payload_hash` | TEXT | NOT NULL | SHA-256 hash of payload for deduplication / replay detection |

---

### 2.4 `journal_entries` & `ledger_entries` (Double-Entry Engine)
Immutable records representing financial movements. Every journal must satisfy $\sum \text{debits} - \sum \text{credits} = 0$.

#### `journal_entries` (Container)
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Journal UUID |
| `journal_id` | TEXT | UNIQUE, NOT NULL, INDEX | e.g. `"JRN-S1-001"` |
| `idempotency_key` | TEXT | UNIQUE, NOT NULL, INDEX | Prevents duplicate journal execution |
| `reference` | TEXT | NOT NULL, INDEX | Linked payment or case ID |
| `description` | TEXT | NOT NULL | Narrative (e.g. `"Initial receipt in suspense for PAY-S1-001"`) |
| `posted_at` | TIMESTAMP | NOT NULL, UTC | When journal was posted |

#### `ledger_entries` (Individual Legs)
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Entry UUID |
| `journal_id` | UUID | FOREIGN KEY, INDEX | Reference to `journal_entries.id` |
| `account_id` | TEXT | NOT NULL, INDEX | Account code (e.g. `"ACC-SETTLEMENT-CASH"`) |
| `debit_minor` | INTEGER | NOT NULL, >= 0 | Debit amount in pence |
| `credit_minor` | INTEGER | NOT NULL, >= 0 | Credit amount in pence |
| `source_payment_id`| TEXT | NULLABLE, INDEX | Traceability back to `incoming_payments.payment_id` |
| `posted_at` | TIMESTAMP | NOT NULL, UTC | Post date |

---

### 2.5 `investigation_cases`
Represents customer complaints or proactive missing payment alerts under investigation.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal UUID |
| `case_id` | TEXT | UNIQUE, NOT NULL, INDEX | Human-readable ID (e.g. `"CASE-2026-001"`) |
| `customer_id` | TEXT | NOT NULL, INDEX | Customer who filed complaint |
| `beneficiary_account_id`| TEXT | NOT NULL, INDEX | Expected destination account (`"ACC-LEN-001"`) |
| `reference` | TEXT | NOT NULL, INDEX | Reference customer claims was used (e.g. `"MON-78291"`) |
| `amount_minor` | INTEGER | NOT NULL, >= 0 | Expected amount in minor units (`50000` = £500.00) |
| `currency` | TEXT | NOT NULL, DEFAULT 'GBP' | Currency |
| `state` | TEXT | NOT NULL, INDEX | `OPEN`, `INVESTIGATING`, `AWAITING_REVIEW`, `RESOLVED`, `CLOSED` |
| `findings_json` | TEXT | NULLABLE | JSON string with findings from Payment Trace and Reconciliation |
| `evidence_refs` | TEXT | NULLABLE | JSON list of authoritative evidence IDs (e.g. `["PAY-S1-001"]`) |
| `proposed_action` | TEXT | NULLABLE | `AUTO_ALLOCATE`, `WAIT_AND_RECHECK`, `TRACE_WITH_PROVIDER`, `HUMAN_REVIEW` |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Creation time |
| `updated_at` | TIMESTAMP | NOT NULL, UTC | Last update time |

---

### 2.6 `approval_requests`
Tracks human operator decisions when a case requires dual-authorization or manual resolution.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal UUID |
| `approval_id` | TEXT | UNIQUE, NOT NULL, INDEX | e.g. `"APP-001"` |
| `case_id` | TEXT | NOT NULL, INDEX | Linked `investigation_cases.case_id` |
| `reviewer_id` | TEXT | NULLABLE | Operator username e.g. `"ops_sarah"` |
| `decision` | TEXT | NOT NULL, DEFAULT 'PENDING' | `PENDING`, `APPROVED`, `REJECTED` |
| `reason` | TEXT | NULLABLE | Operator explanation or rationale |
| `decided_at` | TIMESTAMP | NULLABLE | When operator took action |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Creation time |

---

### 2.7 `audit_events`
Append-only log of every state transition, policy check, and agent decision.

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Audit ID |
| `actor` | TEXT | NOT NULL | e.g. `"PaymentTraceNode"`, `"DeterministicPolicyEngine"`, `"ops_sarah"` |
| `action` | TEXT | NOT NULL | `"CASE_INTAKE"`, `"RECONCILE"`, `"AUTO_ALLOCATE"`, `"HOLD_FROZEN_ACCOUNT"` |
| `policy_version` | TEXT | NOT NULL, DEFAULT 'v1.0.0' | Semantic policy version applied |
| `before_state` | TEXT | NULLABLE | Case or payment state prior to action |
| `after_state` | TEXT | NULLABLE | State resulting from action |
| `details` | TEXT | NULLABLE | Contextual details or rationale |
| `timestamp` | TIMESTAMP | NOT NULL, UTC | Event timestamp |

---

## 3. Seed Scenarios Matrix (What `seed.py` Will Generate)

When `seed.py` runs, it clears and seeds the database with representative data for all core scenarios:

### Scenario S1: Received, Not Allocated (Happy Path)
- **Payment Record**: `PAY-S1-001`, Amount: £500.00 (`50000`), Ref: `"MON-78291 ACC-LEN-001"`, Sender: Monzo, Status: `SETTLED`, Allocation: `UNALLOCATED`.
- **Initial Ledger**:
  - Debit: `ACC-SETTLEMENT-CASH` £500.00
  - Credit: `ACC-UNALLOCATED-SUSPENSE` £500.00
- **Complaint Case**: `CASE-S1-001`, Customer: `CUST-101`, Expected Acc: `ACC-LEN-001`, Ref: `"MON-78291"`, Amount: £500.00.
- **Expected Outcome**: Exact reference match $\rightarrow$ unallocated receipt in suspense $\rightarrow$ safe `AUTO_ALLOCATE`.

### Scenario S2: Reference Given, No Receipt (The Ghost Payment)
- **Payment Record**: *None exists* in `incoming_payments`.
- **Complaint Case**: `CASE-S2-002`, Customer: `CUST-102`, Expected Acc: `ACC-LEN-002`, Ref: `"MON-99881"`, Amount: £250.00.
- **Expected Outcome**: Zero authoritative receipts found $\rightarrow$ `DO_NOT_CREDIT` $\rightarrow$ `TRACE_WITH_PROVIDER`.

### Scenario S3: Ambiguous Beneficiary (Multiple Account Matches)
- **Payment Record**: `PAY-S3-003`, Amount: £1,200.00 (`120000`), Ref: `"MON-33112 BEN CHIDERA"`, Sender: Monzo, Status: `SETTLED`, Allocation: `UNALLOCATED`.
- **Initial Ledger**: In suspense pool.
- **Accounts in DB**: Two accounts for Benjamin Chidera: `ACC-LEN-003` (Personal) and `ACC-LEN-004` (Business).
- **Complaint Case**: `CASE-S3-003`, Customer: `CUST-103`, Expected Acc: `ACC-LEN-003`, Ref: `"MON-33112"`.
- **Expected Outcome**: Beneficiary ambiguity detected $\rightarrow$ `HUMAN_REVIEW` required (LangGraph interrupt).

### Scenario S4: Duplicate Event / Replay
- **Payment Record**: `PAY-S4-004`, Amount: £150.00, Status: `SETTLED`, Allocation: `ALLOCATED`.
- **Ledger**: Already allocated to `ACC-LEN-001` with prior journal `JRN-PREV-004`.
- **Replay Event**: Same webhook delivered again.
- **Expected Outcome**: Idempotency check catches duplicate; no duplicate credit.

### Scenario S5: Restricted / Frozen Account
- **Payment Record**: `PAY-S5-005`, Amount: £750.00, Ref: `"MON-55441 ACC-LEN-005"`, Status: `SETTLED`, Allocation: `UNALLOCATED`.
- **Customer Account**: `ACC-LEN-005` has `status = 'FROZEN'`.
- **Complaint Case**: `CASE-S5-005`.
- **Expected Outcome**: Policy check detects `FROZEN` status $\rightarrow$ blocks auto-allocation $\rightarrow$ routes to Compliance / Ops review.

