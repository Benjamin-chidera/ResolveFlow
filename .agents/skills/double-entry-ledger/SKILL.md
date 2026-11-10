---
name: double-entry-ledger
description: >-
  Design, audit, and manipulate double-entry financial ledger journals and accounts with
  integer minor-unit arithmetic, strict balance invariant checks, and idempotency.
---

# Double-Entry Ledger Skill

This skill enforces financial accounting standards, balanced journals, and strict integer-based arithmetic across all ledger operations.

## Fundamental Invariants
1. **Integer Minor Units**: All monetary amounts MUST be represented as 64-bit integers in the minor unit of the currency (e.g. cents for USD/EUR, pence for GBP). Never use floating point numbers.
2. **Balanced Journal Entries**: Every journal transaction must satisfy:
   $$\sum \text{debits} - \sum \text{credits} = 0$$
3. **Immutability**: Posted journal entries are strictly immutable. Corrections must be executed via offsetting reversing journals, never SQL `UPDATE` or `DELETE` on posted entries.

## Standard Chart of Accounts
- `1000 - Assets`: Cash at Bank, Settlement In-Flight, Clearing Receivables.
- `2000 - Liabilities`: Customer Deposits / Available Balances, Inward Pending Payables.
- `3000 - Equity`: Retained Earnings, Operational Reserves.
- `4000 - Revenue`: Processing Fees, Interchange Income.
- `5000 - Expense`: Payment Rail Fees, Chargeback Penalties.

## Transactional Workflow
1. Generate unique `journal_id` and associate the idempotency key.
2. Formulate balanced legs:
   - Debit: Account increasing (Asset/Expense) or decreasing (Liability/Equity).
   - Credit: Account decreasing (Asset/Expense) or increasing (Liability/Equity).
3. Verify that total debits equal total credits before persistence.
4. Commit atomically within a database transaction alongside the transactional outbox record.

