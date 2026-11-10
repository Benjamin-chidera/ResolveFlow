---
name: resolution-policy
description: >-
  Evaluate financial control policies, enforce deterministic recovery rules,
  generate structured resolution proposals, and manage human approval workflows.
---

# Resolution Policy & Control Skill

This skill governs the final stage of transaction recovery: enforcing strictly authorized financial actions without bypassing policy.

## Financial Invariants & Safety Guardrails
1. **No Unilateral Refund for Settled Status**: A payment in `SETTLED` state cannot be automatically refunded merely because a recipient asserts non-receipt. Settled payments require a formal recall request or bilateral confirmation.
2. **Dual Authorization / Human-in-the-Loop**:
   - Any proposed recovery exceeding high-value thresholds (e.g. > $10,000 / 1,000,000 cents) requires human operator sign-off.
   - Any transaction flagged as `HIGH` or `CRITICAL` fraud risk cannot be executed automatically.
3. **Structured Proposal Only**:
   - The Resolution Agent ONLY generates structured proposals. It NEVER directly modifies the ledger or issues external banking transfers without passing through the deterministic policy engine.
4. **Audit Logging**:
   - Every proposal and execution must produce an `audit_event` capturing:
     - `actor`: System / Agent / Human Operator ID
     - `action`: PROPOSE_RECOVERY / APPROVE / REJECT / EXECUTE_REVERSAL
     - `policy_version`: Semantic version of policy ruleset applied
     - `before_state` & `after_state` snapshot
     - `timestamp`: UTC ISO 8601

