# Spec 08: Conversational Intake, Redis Streams Worker & Persisted Real-Time Updates

## 1. Overview
This specification defines the backend-only conversational investigation architecture for **ResolveFlow AI**. It separates the **Customer Conversation Service** (FastAPI HTTP + SSE) from the **Asynchronous Investigation Worker** (a dedicated worker process powered by **Redis Streams** and **LangGraph**), while keeping **SQLite** as the single source of truth for all financial records, messages, cases, approvals, outbox events, and audit logs.

---

## 2. Core Architectural Principles & Constraints

1. **Backend-Only Stack**: FastAPI, SQLite, Redis Streams, LangGraph, Ollama, and Pytest. No frontend UI, WebSockets, Kafka, or extra microservices.
2. **No In-Memory Fire-and-Forget**: `asyncio.create_task()` MUST NOT be used as the durable execution mechanism for investigations. All investigation jobs are persisted via a **SQLite Transactional Outbox** and dispatched through **Redis Streams** (`XADD` / `XREADGROUP`) to an independent worker process.
3. **SQLite as Authoritative System of Record**: Redis is used strictly as the durable stream transport and consumer coordination layer. All financial states, chat messages, versioned evidence, case events, approvals, and ledger postings live in SQLite.
4. **Customer Claims Are Untrusted Evidence**: Information provided in chat (references, amounts, sender bank names) is stored as **versioned customer evidence** (`evidence_version`), never treated as authoritative settlement truth.
5. **Financial Authorization Outside the LLM**: The LLM may extract conversational slots and summarize verified evidence, but all financial decisions and double-entry postings execute strictly inside the **Deterministic Policy Engine** under atomic SQLite transactions.
6. **Strict Information & Privacy Boundaries**:
   - Customers can only access their own authenticated sessions and cases.
   - Internal risk flags, compliance holds (`FROZEN` status), internal operational notes, and other customers' data are never exposed in customer responses or SSE streams.
   - The system never promises fixed investigation or resolution timelines without measured evidence.

---

## 3. End-to-End Architecture

```mermaid
flowchart TD
    Client([Authenticated Customer HTTP / SSE]) -->|POST /chat/sessions/{id}/messages| ChatAPI[Customer Conversation Service\nFastAPI Process]
    Client <-->|GET /chat/sessions/{id}/events\nLast-Event-ID Replay + Live SSE| ChatAPI

    subgraph SQLite ["SQLite (Authoritative System of Record)"]
        ChatTables[(chat_sessions\nchat_messages\ncustomer_evidence_claims)]
        CaseTables[(investigation_cases\ncase_events\napproval_requests)]
        OutboxTable[(outbox_events)]
        LedgerTables[(incoming_payments\njournal_entries\nledger_entries\naudit_events)]
    end

    ChatAPI -->|1. Atomic Commit:\nMessage + Evidence vN + Outbox Row| SQLite
    OutboxRelay[Transactional Outbox Relay] -->|2. Poll PENDING outbox_events| OutboxTable
    OutboxRelay -->|3. XADD resolveflow:investigations| RedisStream[[Redis Stream:\nresolveflow:investigations]]

    subgraph WorkerProc ["Asynchronous Investigation Worker Process"]
        Consumer[Redis Consumer Group:\nXREADGROUP / XAUTOCLAIM]
        LangGraph[LangGraph Investigation Workflow:\nTrace -> Reconcile -> Explain -> Propose]
        PolicyEngine[Deterministic Policy Engine]
        DLQHandler[Retry / Backoff / DLQ Router]
    end

    RedisStream -->|4. Consume Job| Consumer
    Consumer -->|5. Acquire Case Lease & Run| LangGraph
    LangGraph -->|6. Read Evidence vN & Payment Records| SQLite
    LangGraph --> PolicyEngine
    PolicyEngine -->|7. Atomic Commit:\nLedger Journal + Post-Commit Case Event| SQLite
    Consumer -->|8. XACK after SQLite Commit| RedisStream
    Consumer -->|Exhausted Retries| DLQHandler
    DLQHandler -->|XADD resolveflow:investigations:dlq| DLQ[[Redis Dead-Letter Stream]]
```

---

## 4. Relational Schema Extensions (SQLite)

### 4.1 `chat_sessions`
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal UUID |
| `session_id` | TEXT | UNIQUE, NOT NULL, INDEX | Public session ID (e.g. `"CSESS-001"`) |
| `tenant_id` | TEXT | NOT NULL, INDEX | Tenant identifier (`"lenfi_uk"`) |
| `customer_id` | TEXT | NOT NULL, INDEX | Authenticated owner (`"CUST-101"`) |
| `beneficiary_account_id` | TEXT | NOT NULL, INDEX | Linked customer account (`"ACC-LEN-001"`) |
| `active_case_id` | TEXT | NULLABLE, INDEX | Linked `investigation_cases.case_id` once opened |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Session creation time |
| `updated_at` | TIMESTAMP | NOT NULL, UTC | Last activity timestamp |

### 4.2 `chat_messages`
| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal UUID |
| `message_id` | TEXT | UNIQUE, NOT NULL, INDEX | e.g. `"MSG-001"` |
| `session_id` | TEXT | NOT NULL, INDEX | Foreign key to `chat_sessions.session_id` |
| `sender_role` | TEXT | NOT NULL | `"CUSTOMER"` or `"AGENT"` |
| `content` | TEXT | NOT NULL | Sanitized message text |
| `evidence_version` | INTEGER | NULLABLE | Version number of evidence extracted from this message |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Timestamp |

### 4.3 `customer_evidence_claims` (Versioned Customer Clues)
Every time a customer supplies or updates payment details in chat, a new immutable versioned row is appended:

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Internal UUID |
| `case_id` | TEXT | NOT NULL, INDEX | Linked `investigation_cases.case_id` |
| `evidence_version` | INTEGER | NOT NULL | Monotonically increasing per case (`1, 2, 3...`) |
| `claimed_reference` | TEXT | NULLABLE | Reference provided by customer (`"MON-78291"`) |
| `claimed_amount_minor` | INTEGER | NULLABLE | Claimed amount in pence (`50000`) |
| `claimed_sender_bank` | TEXT | NULLABLE | Claimed sending bank (`"Monzo"`) |
| `source_message_id` | TEXT | NOT NULL | Message that produced this evidence version |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Timestamp |

*Unique Constraint*: `UNIQUE(case_id, evidence_version)`

### 4.4 `outbox_events` (Transactional Outbox for Redis Streams)
Guarantees zero lost investigation jobs between SQLite commits and Redis publication:

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `id` | UUID | PRIMARY KEY | Outbox row UUID |
| `outbox_id` | TEXT | UNIQUE, NOT NULL, INDEX | e.g. `"OUT-001"` |
| `stream_name` | TEXT | NOT NULL | `"resolveflow:investigations"` |
| `idempotency_key` | TEXT | UNIQUE, NOT NULL, INDEX | e.g. `"JOB-{case_id}-v{evidence_version}"` |
| `payload_json` | TEXT | NOT NULL | Serialized job payload |
| `status` | TEXT | NOT NULL, INDEX | `"PENDING"`, `"PUBLISHED"`, `"FAILED"` |
| `redis_message_id` | TEXT | NULLABLE | Stream entry ID returned by Redis `XADD` |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Created in same DB transaction as case/evidence |
| `published_at` | TIMESTAMP | NULLABLE, UTC | When relayed to Redis |

### 4.5 `case_events` (Persisted SSE Event Log)
Stores both customer-safe progress events and internal operational events:

| Column | Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `sequence_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Monotonic cursor for SSE `id:` / `Last-Event-ID` |
| `case_id` | TEXT | NOT NULL, INDEX | Linked `investigation_cases.case_id` |
| `session_id` | TEXT | NOT NULL, INDEX | Linked `chat_sessions.session_id` |
| `visibility` | TEXT | NOT NULL, INDEX | `"CUSTOMER_SAFE"` or `"INTERNAL_ONLY"` |
| `event_type` | TEXT | NOT NULL | `"CASE_OPENED"`, `"TRACE_PROGRESS"`, `"EVIDENCE_UPDATED"`, `"AWAITING_REVIEW"`, `"CASE_RESOLVED"` |
| `payload_json` | TEXT | NOT NULL | Customer-safe or internal JSON payload |
| `created_at` | TIMESTAMP | NOT NULL, UTC | Committed timestamp |

### 4.6 `investigation_cases` Concurrency Columns
Extend `investigation_cases` with:
- `latest_evidence_version` (`INTEGER`, default `1`): Incremented whenever the customer provides new evidence.
- `processed_evidence_version` (`INTEGER`, default `0`): The version last reconciled by the worker.
- `active_worker_lease_id` (`TEXT`, nullable): Worker lock identifier preventing concurrent workers from executing the same case simultaneously.
- `lease_expires_at` (`TIMESTAMP`, nullable): Lease expiration for crash recovery.

---

## 5. Component 1: Customer Conversation Service

### 5.1 Targeted Context-Aware Intake
1. **Pre-Query Account & Case Context**: Before generating a reply, the service queries SQLite for the authenticated customer's `customers_accounts` record and any existing open `investigation_cases`.
2. **Avoid Repetitive Questions**:
   - The agent never asks for `beneficiary_account_id` or `currency` (already known from the authenticated account).
   - If the customer already supplied `sender_bank` or `amount_minor` in an earlier turn, those slots remain populated.
3. **Concurrent Messaging During Active Investigation**:
   - If a customer sends a message while `case.state == 'INVESTIGATING'`:
     - **Informational query** (e.g., *"Are you still checking on this?"*): The service reads the latest `CUSTOMER_SAFE` event from `case_events` and replies immediately without touching or cancelling the active worker job.
     - **New / corrected evidence** (e.g., *"Sorry, the reference is actually MON-78291"*): The service atomically inserts `customer_evidence_claims` with `evidence_version = latest_evidence_version + 1`, updates `investigation_cases.latest_evidence_version`, and writes a new `outbox_events` job (`JOB-{case_id}-v{new_version}`). The active worker is **not** cancelled; it either picks up the newer evidence version before finishing or runs a follow-up reconciliation pass.

### 5.2 Strict Customer Communication Guardrails
| Situation | What the Customer Is Told (`CUSTOMER_SAFE`) | What Is Strictly Hidden (`INTERNAL_ONLY`) |
| :--- | :--- | :--- |
| **Account is `FROZEN` (S5)** | *"We have located your incoming transfer and it is undergoing a standard review by our specialist team under case `#CASE-ID`."* | Account status `FROZEN`, compliance hold reasons, AML/risk indicators. |
| **Ambiguous Beneficiary (S3)** | *"We located a matching incoming transfer in our clearing queue, but the reference requires quick manual verification by our operations team."* | Other candidate customers' names or account IDs (`ACC-LEN-004`). |
| **No Receipt Yet (S2)** | *"We checked our clearing records for reference `MON-99881` and have not received settlement from Monzo yet. We have opened a provider trace."* | Internal provider adapter raw logs or unverified timelines. |
| **Auto-Allocated (S1)** | *"We verified your transfer (`MON-78291`) and credited £500.00 to your account (`ACC-LEN-001`)."* *(Sent ONLY after ledger commit)* | Internal policy thresholds or suspense account codes. |

---

## 6. Component 2: Asynchronous Investigation Worker & Redis Streams

### 6.1 Stream & Consumer Group Topology
- **Primary Stream**: `resolveflow:investigations`
- **Dead-Letter Stream**: `resolveflow:investigations:dlq`
- **Consumer Group**: `resolveflow_investigation_workers`
- **Consumer Name**: `worker-{hostname}-{pid}`

### 6.2 Transactional Outbox Relay
1. Whenever a case is ready for investigation (or receives a new `evidence_version`), the FastAPI request commits the case update and an `outbox_events` row (`status = 'PENDING'`) in **one SQLite transaction**.
2. The Outbox Relay reads `PENDING` outbox rows ordered by `created_at`, publishes each to Redis via:
   ```text
   XADD resolveflow:investigations * outbox_id <id> case_id <case_id> evidence_version <v> attempt 1
   ```
3. Upon receiving the Redis stream entry ID, the relay updates `outbox_events.status = 'PUBLISHED'` and `redis_message_id`.
4. If the relay publishes twice due to a crash before marking `PUBLISHED`, the worker's idempotency check safely deduplicates the job.

### 6.3 Worker Consumption, Crash Recovery & Lease Protocol
1. **Claiming Stalled Jobs (`XAUTOCLAIM`)**:
   On startup and periodically every `10s`, each worker calls:
   ```text
   XAUTOCLAIM resolveflow:investigations resolveflow_investigation_workers <consumer_name> 30000 0-0 COUNT 10
   ```
   Any job where a previous worker crashed mid-execution (idle $> 30\text{s}$) is reclaimed.
2. **Reading New Jobs (`XREADGROUP`)**:
   ```text
   XREADGROUP GROUP resolveflow_investigation_workers <consumer_name> COUNT 1 BLOCK 2000 STREAMS resolveflow:investigations >
   ```
3. **Idempotent Case Lease Acquisition**:
   Before running LangGraph, the worker runs an atomic SQLite update:
   - If `case.state == 'RESOLVED'` or `case.processed_evidence_version >= job.evidence_version` (and not awaiting re-run): the job is already complete $\rightarrow$ immediately `XACK` and skip.
   - If another live worker holds an unexpired `active_worker_lease_id`: leave pending or re-queue after backoff so only one worker executes a case at a time.
   - Otherwise, acquire `active_worker_lease_id` with a `30s` TTL and transition `state = 'INVESTIGATING'`.
4. **Handling Mid-Flight Evidence Updates**:
   After the LangGraph `Trace` and `Reconciliation` nodes complete, the worker checks `investigation_cases.latest_evidence_version` in SQLite:
   - If `latest_evidence_version > job.evidence_version`, the customer sent new clues while the worker was running!
   - The worker reloads the latest `customer_evidence_claims` row (`v_latest`) and re-runs `Trace -> Reconcile` before proceeding to the Policy Engine.
5. **Acknowledgment (`XACK`)**:
   `XACK` is called **only after** the SQLite transaction persisting the investigation outcome (and any ledger journal or approval request) commits.

### 6.4 Bounded Retries, Exponential Backoff & Dead-Letter Queue
- Each stream job carries an integer `attempt` counter (starting at `1`, `MAX_ATTEMPTS = 5`).
- If an unhandled or transient error occurs (e.g. Ollama connection failure, transient SQLite lock):
  - If `attempt < MAX_ATTEMPTS`:
    - Compute backoff delay: $\text{delay} = \min(30, 2^{\text{attempt}-1})$ seconds.
    - Re-enqueue retry message with `attempt = attempt + 1` and `XACK` the failed entry.
  - If `attempt >= MAX_ATTEMPTS`:
    - Publish job payload and failure reason to `resolveflow:investigations:dlq` via `XADD`.
    - Persist an internal `AuditEvent` (`action = "JOB_MOVED_TO_DLQ"`) and `INTERNAL_ONLY` case event.
    - `XACK` the exhausted message from the primary stream so the worker group is never blocked by a poison pill.

---

## 7. Component 3: Persisted Server-Sent Events (SSE)

### `GET /api/v1/chat/sessions/{session_id}/events`
- **Authentication**: Requires matching `X-Customer-ID` header owning `session_id` (`403 Forbidden` if mismatched).
- **Missed-Event Replay**:
  - Client may pass standard header `Last-Event-ID: <sequence_id>` or query param `after_sequence_id=<sequence_id>` (defaults to `0`).
  - On connection, the server queries SQLite:
    ```sql
    SELECT sequence_id, event_type, payload_json, created_at
    FROM case_events
    WHERE session_id = :session_id
      AND visibility = 'CUSTOMER_SAFE'
      AND sequence_id > :last_seen_id
    ORDER BY sequence_id ASC
    ```
  - Immediately flushes all historical/missed events to the client, then streams new `CUSTOMER_SAFE` events as they commit.
- **Post-Commit Guarantee**:
  - A `CASE_RESOLVED` event stating that funds have been credited is written to `case_events` **inside the same atomic SQLite transaction** as the `JournalEntry` and `LedgerEntry` allocation. It is impossible for a customer to receive a credit notification if the ledger transaction rolled back.

---

## 8. Component 4: Human Escalation & Stale Approval Protection

1. **Creating the Review Task**:
   When the Policy Engine routes a case to `HUMAN_REVIEW` (e.g. Scenario S3 Ambiguous Beneficiary or Scenario S5 Frozen Account), it:
   - Persists an `ApprovalRequest` bound to `(case_id, payment_id, proposed_action, evidence_version)`.
   - Checkpoints the LangGraph state under `thread_id = case_id`.
   - Emits a `CUSTOMER_SAFE` event (generic review notice) and an `INTERNAL_ONLY` event (detailed discrepancy & candidate accounts).
2. **Operations Endpoints**:
   - `GET /api/v1/approvals/pending`: Returns pending `ApprovalRequest` records for authenticated operations/compliance staff (`X-Reviewer-Role: PAYMENT_OPERATIONS` or `COMPLIANCE_OFFICER`).
   - `POST /api/v1/approvals/{approval_id}/decision`:
     - Validates operator role.
     - **Immediate Pre-Execution Revalidation (Stale Approval Check)**:
       Inside an exclusive SQLite transaction, re-reads:
       1. `investigation_cases.latest_evidence_version == approval.evidence_version` (Ensures new contradictory evidence didn't arrive after the approval was requested).
       2. `incoming_payments.allocation_status == 'UNALLOCATED'` and `provider_status == 'SETTLED'`.
       3. Target `customers_accounts.status == 'ACTIVE'` (unless compliance override is explicitly permitted by policy, which by default is blocked for `FROZEN` accounts).
       4. No existing `JournalEntry` with `idempotency_key = f"ALLOC-{payment_id}"`.
     - If any check fails, aborts execution and returns `409 Conflict` (`STALE_APPROVAL_OR_STATE_CHANGED`).
     - If all checks pass, resumes the LangGraph workflow, executes the double-entry reclassification journal, commits the `CUSTOMER_SAFE` resolution event, and marks the case `RESOLVED`.

---

## 9. Docker Compose Topology

`docker-compose.yml` runs four services:
1. **`app`**: FastAPI backend serving Customer Chat, SSE streams, Case APIs, Provider Webhooks, and Approval APIs.
2. **`worker`**: Asynchronous Investigation Worker (`uv run python -m app.worker`) consuming from Redis Streams (`resolveflow:investigations`) and running the Outbox Relay + LangGraph pipeline.
3. **`redis`**: `redis:7-alpine` backing Redis Streams and Consumer Groups (`XREADGROUP`, `XACK`, `XAUTOCLAIM`).
4. **`ollama`**: Local LLM server for slot extraction and evidence-grounded investigation summaries.

---

## 10. Acceptance Test Suite (`tests/test_conversational_async_investigation.py`)

The implementation must pass all 11 automated acceptance tests:

1. **Targeted Conversational Intake**: Agent uses existing account context (`ACC-LEN-001`) without asking repetitive questions and extracts `amount_minor`, `sender_bank`, and `reference`.
2. **Concurrent Chat Without Cancelling Investigation**: Customer sends follow-up messages while worker is actively running; active investigation is neither cancelled nor restarted from scratch.
3. **Versioned Evidence Mid-Flight Incorporation**: Customer updates reference (`v1` $\rightarrow$ `v2`) while investigation is running; worker safely reconciles `v2` before final resolution.
4. **Transactional Outbox Durability**: Simulated failure between SQLite commit and Redis `XADD` preserves the `PENDING` outbox event and relays it on recovery.
5. **Worker Crash & `XAUTOCLAIM` Recovery**: Simulated worker crash mid-investigation leaves message in Redis PEL (Pending Entries List); secondary worker reclaims it via `XAUTOCLAIM` and completes the case.
6. **Duplicate Stream Delivery Idempotency**: Delivering the exact same Redis Stream payload 5 times results in at most 1 investigation execution and **zero duplicate ledger credits**.
7. **Exhausted Retries & Dead-Letter Stream (`DLQ`)**: A job that repeatedly fails transient execution exhausts `MAX_ATTEMPTS`, moves to `resolveflow:investigations:dlq`, and is `XACK`-ed from the main stream.
8. **SSE Disconnect & Missed-Event Replay**: Client disconnects at `sequence_id = 1`, worker emits events `2` and `3`, client reconnects with `Last-Event-ID: 1` and receives events `2` and `3` in order.
9. **Tenant & Customer Isolation**: Customer `CUST-102` attempting to read or stream `CUST-101`'s session or case receives `403 Forbidden`; zero internal risk/freeze flags leak in customer SSE events.
10. **Stale Approval Rejection**: If a payment is already allocated or new conflicting evidence arrives while an `ApprovalRequest` is pending, `POST /api/v1/approvals/{id}/decision` rejects the stale approval (`409 Conflict`).
11. **Post-Commit Resolution Notification**: Verified credit notifications appear in `case_events` if and only if the underlying double-entry `JournalEntry` (`ALLOC-{payment_id}`) committed to SQLite.

