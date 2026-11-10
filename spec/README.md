# ResolveFlow AI: Backend Specifications Directory

This directory contains the engineering specifications for the **ResolveFlow AI** incoming payment investigation backend (inspired by the Monzo-to-LenFi missing payment case study).

## Specifications Index

| Spec File | Title | Core Focus |
| :--- | :--- | :--- |
| **[`01_data_model_and_seed_fixtures.md`](./01_data_model_and_seed_fixtures.md)** | Data Model & Seed Fixtures | SQLite schema, double-entry chart of accounts, integer minor-unit arithmetic, and `seed.py` data matrix for Scenarios S1–S5. |
| **[`02_payment_ingestion_and_simulation.md`](./02_payment_ingestion_and_simulation.md)** | Payment Ingestion & Rail Simulator | Ingestion endpoint (`POST /provider-events`), SHA-256 idempotency, initial booking into suspense, and Faster Payments rail simulator. |
| **[`03_case_intake_and_lifecycle.md`](./03_case_intake_and_lifecycle.md)** | Case Intake & Lifecycle Management | Customer complaint API (`POST /cases`), proactive reconciliation scanner (`POST /reconciliation/run`), case state machine, and timeline tracing. |
| **[`04_investigation_and_reconciliation_workflow.md`](./04_investigation_and_reconciliation_workflow.md)** | LangGraph Multi-Node Workflow | Sequential LangGraph pipeline (Payment Trace $\rightarrow$ Reconciliation $\rightarrow$ Ollama Explanation $\rightarrow$ Proposal), scoped read-only tools, and SQLite checkpointer. |
| **[`05_deterministic_policy_and_execution.md`](./05_deterministic_policy_and_execution.md)** | Deterministic Policy Engine & Execution | Authorizer vs Proposer separation, 5 non-negotiable policy checks (DB lock, active status, unallocated check, duplicate check, £10k limit), and double-entry reclassification. |
| **[`06_human_review_and_approval_workflow.md`](./06_human_review_and_approval_workflow.md)** | Human Review & Approval Flow | LangGraph `interrupt()` mechanism, approval decision API (`POST /approvals/{id}/decision`), stale approval protection, and thread resumption. |
| **[`07_scenarios_and_evaluation_suite.md`](./07_scenarios_and_evaluation_suite.md)** | Scenarios & Evaluation Suite | S1–S6 test matrix, automated hard invariant assertions (0 duplicate credits, 100% balanced journals, zero unauthorized credits), and evaluation scorecard. |
| **[`08_conversational_intake_and_async_investigation.md`](./08_conversational_intake_and_async_investigation.md)** | Conversational Intake & Async Redis Streams Worker | Customer conversation service, SQLite transactional outbox, Redis Streams consumer groups (`XREADGROUP`, `XACK`, `XAUTOCLAIM`), DLQ, versioned evidence, persisted SSE replay, and stale approval protection. |
| **[`08_conversational_intake_and_async_investigation.md`](./08_conversational_intake_and_async_investigation.md)** | Conversational Intake & Async Redis Streams Worker | Customer conversation service, SQLite transactional outbox, Redis Streams consumer groups (`XREADGROUP`, `XACK`, `XAUTOCLAIM`), DLQ, versioned evidence, persisted SSE replay, and stale approval protection. |

