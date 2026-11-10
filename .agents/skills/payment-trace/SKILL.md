---
name: payment-trace
description: >-
  Reconstruct transaction lifecycles, trace payments across synthetic bank rails,
  ISO 20022 messages, and Stripe webhooks, and diagnose stalled, returned, or unconfirmed transfers.
---

# Payment Trace Skill

This skill guides the agent in reconstructing payment states, resolving inconsistencies across heterogeneous rails, and tracing transactions throughout their lifecycle.

## Domain Concepts
- **Transaction Identifiers**: Trace by internal `transaction_id`, payment processor IDs (e.g. Stripe `pi_...`), or SWIFT UETR (`123e4567-e89b-12d3-a456-426614174000`). Note: a reference alone does not grant interbank settlement access.
- **State Machine States**:
  - `INITIATED`: Transfer requested and validated.
  - `PROCESSING`: Transmitted to processor / clearing rail.
  - `SETTLED`: Funds confirmed at destination / settlement account.
  - `REJECTED`: Declined prior to settlement (e.g. insufficient funds, compliance hold).
  - `RETURNED`: Reversal after settlement (e.g. ACH return, chargeback, Recall).
  - `SUSPENDED`: Held for fraud review or manual operational triage.

## Procedure: State Reconstruction
1. **Query Payment Events**: Fetch chronologically ordered events for the target `payment_id`.
2. **Reconcile Against Ledger**:
   - Check if a corresponding journal entry exists in `ledger_entries`.
   - Verify whether the debit and credit legs posted and settled.
3. **Verify Rail Confirmations**:
   - For Stripe: inspect webhook payload events (`payment_intent.succeeded`, `charge.failed`).
   - For ISO 20022: parse `pacs.008` (credit transfer), `pacs.002` (payment status report), and `camt.054` (credit notification).
4. **Identify Inconsistencies**:
   - Account debited but rail status shows `failed` -> Stuck debit requiring automated correction.
   - Rail status shows `succeeded` but ledger entry missing -> Missing reconciliation ingestion.
   - Duplicate webhook delivery -> Verify idempotency key matching.

