import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from sqlmodel import Session, select

from app.agents.graph import run_case_investigation
from app.models.account import CustomerAccount
from app.models.case import InvestigationCase
from app.models.enums import (
    AccountStatus,
    AccountType,
    AllocationStatus,
    CaseState,
    ProviderPaymentStatus,
)
from app.models.ledger import JournalEntry, LedgerEntry
from app.models.payment import IncomingPayment, PaymentEvent
from app.services.approval_service import (
    ApprovalDecisionRequest,
    process_approval_decision,
)
from app.services.ingestion_service import (
    ProviderEventPayload,
    compute_canonical_fingerprint,
    ingest_provider_event,
)
from app.services.ledger_service import (
    SETTLEMENT_CASH_ACCOUNT,
    UNALLOCATED_SUSPENSE_ACCOUNT,
    get_account_balance_minor,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def seed_evaluation_fixtures(session: Session) -> None:
    """Seeds an isolated SQLite session with the 6 core ResolveFlow AI scenarios (S1-S5 + S6 base)."""
    accounts = [
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="INSTITUTION",
            account_id=SETTLEMENT_CASH_ACCOUNT,
            account_type=AccountType.ASSET,
            currency="GBP",
            status=AccountStatus.ACTIVE,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="INSTITUTION",
            account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-101",
            account_id="ACC-LEN-001",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-102",
            account_id="ACC-LEN-002",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-103",
            account_id="ACC-LEN-003",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-104",
            account_id="ACC-LEN-004",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.ACTIVE,
        ),
        CustomerAccount(
            tenant_id="lenfi_uk",
            customer_id="CUST-105",
            account_id="ACC-LEN-005",
            account_type=AccountType.LIABILITY,
            currency="GBP",
            status=AccountStatus.FROZEN,
        ),
    ]
    session.add_all(accounts)
    session.commit()

    # Scenario S1: Received, Not Allocated (£500.00 in Suspense)
    pay_s1 = IncomingPayment(
        payment_id="PAY-S1-001",
        provider_reference="FP-2026-X881",
        sender_bank="Monzo",
        beneficiary_account_id="ACC-LEN-001",
        raw_reference="MON-78291 ACC-LEN-001",
        amount_minor=50000,
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s1)
    jrn_s1 = JournalEntry(
        journal_id="JRN-S1-001",
        idempotency_key="RECEIPT-PAY-S1-001",
        reference="PAY-S1-001",
        description="Initial receipt parked in suspense for PAY-S1-001",
        posted_at=now_utc(),
    )
    session.add(jrn_s1)
    session.flush()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s1.id,
                account_id=SETTLEMENT_CASH_ACCOUNT,
                debit_minor=50000,
                credit_minor=0,
                source_payment_id="PAY-S1-001",
            ),
            LedgerEntry(
                journal_id=jrn_s1.id,
                account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
                debit_minor=0,
                credit_minor=50000,
                source_payment_id="PAY-S1-001",
            ),
        ]
    )
    session.add(
        PaymentEvent(
            event_id="EVT-S1-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-S1-001",
            event_type="FUNDS_SETTLED",
            event_time=now_utc(),
            payload_hash=hashlib.sha256(b"PAY-S1-001").hexdigest(),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_reference="FP-2026-X881",
                sender_bank="Monzo",
                amount_minor=50000,
                currency="GBP",
                raw_reference="MON-78291 ACC-LEN-001",
            ),
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S1-001",
            customer_id="CUST-101",
            beneficiary_account_id="ACC-LEN-001",
            reference="MON-78291",
            amount_minor=50000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )

    # Scenario S2: Reference Given, No Receipt (£250.00)
    session.add(
        InvestigationCase(
            case_id="CASE-S2-002",
            customer_id="CUST-102",
            beneficiary_account_id="ACC-LEN-002",
            reference="MON-99881",
            amount_minor=25000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )

    # Scenario S3: Ambiguous Beneficiary (£1,200.00 in Suspense, no ACC-LEN code on wire)
    pay_s3 = IncomingPayment(
        payment_id="PAY-S3-003",
        provider_reference="FP-2026-X883",
        sender_bank="Monzo",
        beneficiary_account_id=None,
        raw_reference="MON-33112 BEN CHIDERA",
        amount_minor=120000,
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s3)
    jrn_s3 = JournalEntry(
        journal_id="JRN-S3-001",
        idempotency_key="RECEIPT-PAY-S3-003",
        reference="PAY-S3-003",
        description="Ambiguous receipt in suspense for PAY-S3-003",
        posted_at=now_utc(),
    )
    session.add(jrn_s3)
    session.flush()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s3.id,
                account_id=SETTLEMENT_CASH_ACCOUNT,
                debit_minor=120000,
                credit_minor=0,
                source_payment_id="PAY-S3-003",
            ),
            LedgerEntry(
                journal_id=jrn_s3.id,
                account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
                debit_minor=0,
                credit_minor=120000,
                source_payment_id="PAY-S3-003",
            ),
        ]
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S3-003",
            customer_id="CUST-103",
            beneficiary_account_id="ACC-LEN-003",
            reference="MON-33112",
            amount_minor=120000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )

    # Scenario S4: Already Allocated Payment (£320.00 on ACC-LEN-004)
    pay_s4 = IncomingPayment(
        payment_id="PAY-S4-004",
        provider_reference="FP-2026-X884",
        sender_bank="Monzo",
        beneficiary_account_id="ACC-LEN-004",
        raw_reference="MON-11223 ACC-LEN-004",
        amount_minor=32000,
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.ALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s4)
    jrn_s4_init = JournalEntry(
        journal_id="JRN-S4-INIT",
        idempotency_key="RECEIPT-PAY-S4-004",
        reference="PAY-S4-004",
        description="Initial receipt for PAY-S4-004",
        posted_at=now_utc(),
    )
    session.add(jrn_s4_init)
    session.flush()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s4_init.id,
                account_id=SETTLEMENT_CASH_ACCOUNT,
                debit_minor=32000,
                credit_minor=0,
                source_payment_id="PAY-S4-004",
            ),
            LedgerEntry(
                journal_id=jrn_s4_init.id,
                account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
                debit_minor=0,
                credit_minor=32000,
                source_payment_id="PAY-S4-004",
            ),
        ]
    )
    jrn_s4_alloc = JournalEntry(
        journal_id="JRN-S4-ALLOC",
        idempotency_key="ALLOC-PAY-S4-004",
        reference="PAY-S4-004",
        description="Prior allocation for PAY-S4-004",
        posted_at=now_utc(),
    )
    session.add(jrn_s4_alloc)
    session.flush()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s4_alloc.id,
                account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
                debit_minor=32000,
                credit_minor=0,
                source_payment_id="PAY-S4-004",
            ),
            LedgerEntry(
                journal_id=jrn_s4_alloc.id,
                account_id="ACC-LEN-004",
                debit_minor=0,
                credit_minor=32000,
                source_payment_id="PAY-S4-004",
            ),
        ]
    )
    session.add(
        PaymentEvent(
            event_id="EVT-S4-001",
            source="FASTER_PAYMENTS",
            payment_id="PAY-S4-004",
            event_type="FUNDS_SETTLED",
            event_time=now_utc(),
            payload_hash=hashlib.sha256(b"PAY-S4-004").hexdigest(),
            canonical_fingerprint=compute_canonical_fingerprint(
                source="FASTER_PAYMENTS",
                provider_reference="FP-2026-X884",
                sender_bank="Monzo",
                amount_minor=32000,
                currency="GBP",
                raw_reference="MON-11223 ACC-LEN-004",
            ),
        )
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S4-004",
            customer_id="CUST-104",
            beneficiary_account_id="ACC-LEN-004",
            reference="MON-11223",
            amount_minor=32000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )

    # Scenario S5: Restricted / Frozen Account (£750.00 in Suspense for ACC-LEN-005)
    pay_s5 = IncomingPayment(
        payment_id="PAY-S5-005",
        provider_reference="FP-2026-X885",
        sender_bank="Monzo",
        beneficiary_account_id="ACC-LEN-005",
        raw_reference="MON-55441 ACC-LEN-005",
        amount_minor=75000,
        currency="GBP",
        provider_status=ProviderPaymentStatus.SETTLED,
        allocation_status=AllocationStatus.UNALLOCATED,
        received_at=now_utc(),
    )
    session.add(pay_s5)
    jrn_s5 = JournalEntry(
        journal_id="JRN-S5-001",
        idempotency_key="RECEIPT-PAY-S5-005",
        reference="PAY-S5-005",
        description="Receipt parked in suspense for frozen account PAY-S5-005",
        posted_at=now_utc(),
    )
    session.add(jrn_s5)
    session.flush()
    session.add_all(
        [
            LedgerEntry(
                journal_id=jrn_s5.id,
                account_id=SETTLEMENT_CASH_ACCOUNT,
                debit_minor=75000,
                credit_minor=0,
                source_payment_id="PAY-S5-005",
            ),
            LedgerEntry(
                journal_id=jrn_s5.id,
                account_id=UNALLOCATED_SUSPENSE_ACCOUNT,
                debit_minor=0,
                credit_minor=75000,
                source_payment_id="PAY-S5-005",
            ),
        ]
    )
    session.add(
        InvestigationCase(
            case_id="CASE-S5-005",
            customer_id="CUST-105",
            beneficiary_account_id="ACC-LEN-005",
            reference="MON-55441",
            amount_minor=75000,
            currency="GBP",
            state=CaseState.OPEN,
        )
    )
    session.commit()


def verify_all_journals_balanced(session: Session) -> Dict[str, Any]:
    """
    Invariant 2 (Spec 07): Verify that every double-entry JournalEntry in the ledger
    has sum(debit_minor) == sum(credit_minor) > 0.
    """
    journals = session.exec(select(JournalEntry)).all()
    unbalanced_journals: List[str] = []

    for journal in journals:
        lines = journal.lines
        total_debits = sum(line.debit_minor for line in lines)
        total_credits = sum(line.credit_minor for line in lines)
        if total_debits <= 0 or total_debits != total_credits:
            unbalanced_journals.append(journal.journal_id)

    total_count = len(journals)
    compliance_pct = (
        100.0
        if len(unbalanced_journals) == 0
        else (((total_count - len(unbalanced_journals)) / max(total_count, 1)) * 100.0)
    )

    return {
        "total_journals": total_count,
        "unbalanced_journals": unbalanced_journals,
        "compliance_percentage": compliance_pct,
        "passed": len(unbalanced_journals) == 0,
    }


def run_evaluation_suite(session: Session) -> Dict[str, Any]:
    """
    Executes all 6 core ResolveFlow AI scenarios (S1-S6) and checks all 4 hard
    financial invariants, returning structured metrics and a formatted scorecard.
    """
    latencies_seconds: List[float] = []
    scenario_results: Dict[str, bool] = {}
    unauthorised_credits = 0
    duplicate_credits_detected = 0

    # -------------------------------------------------------------------------
    # Scenario S1: Received, Not Allocated -> AUTO_ALLOCATE
    # -------------------------------------------------------------------------
    t0 = time.perf_counter()
    s1_res = run_case_investigation(session, "CASE-S1-001")
    latencies_seconds.append(time.perf_counter() - t0)

    s1_balance = get_account_balance_minor(session, "ACC-LEN-001")
    s1_payment = session.exec(
        select(IncomingPayment).where(IncomingPayment.payment_id == "PAY-S1-001")
    ).first()
    scenario_results["S1"] = (
        s1_res.proposed_action == "AUTO_ALLOCATE"
        and s1_res.execution_status == "EXECUTED"
        and s1_balance == 50000
        and s1_payment is not None
        and s1_payment.allocation_status == AllocationStatus.ALLOCATED
    )

    # -------------------------------------------------------------------------
    # Scenario S2: Reference Given, No Receipt -> TRACE_WITH_PROVIDER
    # -------------------------------------------------------------------------
    t0 = time.perf_counter()
    s2_res = run_case_investigation(session, "CASE-S2-002")
    latencies_seconds.append(time.perf_counter() - t0)

    s2_balance = get_account_balance_minor(session, "ACC-LEN-002")
    if s2_balance > 0:
        unauthorised_credits += 1

    scenario_results["S2"] = (
        s2_res.proposed_action == "TRACE_WITH_PROVIDER"
        and s2_res.execution_status == "NOT_EXECUTED"
        and s2_res.case_state == CaseState.WAITING_PROVIDER.value
        and s2_balance == 0
    )

    # -------------------------------------------------------------------------
    # Scenario S3: Ambiguous Beneficiary -> HUMAN_REVIEW -> Operator Disambiguation
    # -------------------------------------------------------------------------
    t0 = time.perf_counter()
    s3_res = run_case_investigation(session, "CASE-S3-003")
    latencies_seconds.append(time.perf_counter() - t0)

    s3_halted_safely = (
        s3_res.proposed_action == "HUMAN_REVIEW"
        and s3_res.execution_status == "AWAITING_HUMAN_APPROVAL"
        and s3_res.approval_id is not None
    )

    s3_resumed_safely = False
    if s3_halted_safely and s3_res.approval_id:
        approval_res = process_approval_decision(
            session=session,
            approval_id=s3_res.approval_id,
            payload=ApprovalDecisionRequest(
                reviewer_id="ops_sarah",
                reviewer_role="PAYMENT_OPERATIONS",
                decision="APPROVED",
                selected_account_id="ACC-LEN-003",
                reason="Verified sender statement matching ACC-LEN-003",
            ),
        )
        s3_resumed_safely = (
            approval_res.execution_status == "EXECUTED"
            and get_account_balance_minor(session, "ACC-LEN-003") == 120000
        )

    scenario_results["S3"] = s3_halted_safely and s3_resumed_safely

    # -------------------------------------------------------------------------
    # Scenario S4: Duplicate Event / Replay (10x Replay Stress Test)
    # -------------------------------------------------------------------------
    t0 = time.perf_counter()
    for _ in range(10):
        ingest_provider_event(
            session=session,
            payload=ProviderEventPayload(
                event_id="EVT-S4-001",
                source="FASTER_PAYMENTS",
                event_type="FUNDS_SETTLED",
                provider_reference="FP-2026-X884",
                sender_bank="Monzo",
                raw_reference="MON-11223 ACC-LEN-004",
                amount_minor=32000,
                currency="GBP",
            ),
        )
    s4_res = run_case_investigation(session, "CASE-S4-004")
    latencies_seconds.append(time.perf_counter() - t0)

    s4_credits = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.account_id == "ACC-LEN-004",
            LedgerEntry.credit_minor > 0,
        )
    ).all()
    if len(s4_credits) > 1:
        duplicate_credits_detected += len(s4_credits) - 1

    scenario_results["S4"] = (
        s4_res.execution_status == "IDEMPOTENT_NOOP"
        and len(s4_credits) == 1
        and get_account_balance_minor(session, "ACC-LEN-004") == 32000
    )

    # -------------------------------------------------------------------------
    # Scenario S5: Restricted / Frozen Account -> HUMAN_REVIEW (Blocked)
    # -------------------------------------------------------------------------
    t0 = time.perf_counter()
    s5_res = run_case_investigation(session, "CASE-S5-005")
    latencies_seconds.append(time.perf_counter() - t0)

    s5_balance = get_account_balance_minor(session, "ACC-LEN-005")
    if s5_balance > 0:
        unauthorised_credits += 1

    scenario_results["S5"] = (
        s5_res.proposed_action == "HUMAN_REVIEW"
        and s5_res.execution_status == "AWAITING_HUMAN_APPROVAL"
        and s5_balance == 0
    )

    # -------------------------------------------------------------------------
    # Scenario S6: Late Arrival Resumes S2 Case -> AUTO_ALLOCATE
    # -------------------------------------------------------------------------
    t0 = time.perf_counter()
    ingest_provider_event(
        session=session,
        payload=ProviderEventPayload(
            event_id="EVT-S6-LATE-001",
            source="FASTER_PAYMENTS",
            event_type="FUNDS_SETTLED",
            provider_reference="FP-2026-X998",
            sender_bank="Monzo",
            raw_reference="MON-99881 ACC-LEN-002",
            amount_minor=25000,
            currency="GBP",
        ),
    )
    s6_res = run_case_investigation(session, "CASE-S2-002")
    latencies_seconds.append(time.perf_counter() - t0)

    s2_balance_after = get_account_balance_minor(session, "ACC-LEN-002")
    scenario_results["S6"] = (
        s6_res.proposed_action == "AUTO_ALLOCATE"
        and s6_res.execution_status == "EXECUTED"
        and s6_res.case_state == CaseState.RESOLVED.value
        and s2_balance_after == 25000
    )

    # -------------------------------------------------------------------------
    # Global Ledger Balance Invariant Check
    # -------------------------------------------------------------------------
    balance_report = verify_all_journals_balanced(session)

    passed_scenarios = sum(1 for passed in scenario_results.values() if passed)
    total_scenarios = len(scenario_results)
    sorted_latencies = sorted(latencies_seconds)
    p95_index = min(int(len(sorted_latencies) * 0.95), len(sorted_latencies) - 1)
    p95_latency = sorted_latencies[p95_index] if sorted_latencies else 0.0

    duplicate_rate_pct = 0.0 if duplicate_credits_detected == 0 else 100.0
    human_review_success_pct = (
        100.0 if (scenario_results["S3"] and scenario_results["S5"]) else 0.0
    )

    scorecard_text = (
        "======================= RESOLVEFLOW EVALUATION SCORECARD =======================\n"
        f"Scenario Pass Rate:              {passed_scenarios}/{total_scenarios} "
        f"({int((passed_scenarios / total_scenarios) * 100)}%)\n"
        f"Duplicate Credit Rate:           {duplicate_rate_pct:.2f}% across replay tests\n"
        f"Ledger Invariant Compliance:     {balance_report['compliance_percentage']:.1f}% "
        f"({'Zero unbalanced journals' if balance_report['passed'] else 'UNBALANCED JOURNALS DETECTED'})\n"
        f"Unauthorised Credits:            {unauthorised_credits}\n"
        f"p95 Case Investigation Latency:  {p95_latency:.3f}s (< 1.8s target)\n"
        f"Human Review Interrupt Success:  {int(human_review_success_pct)}% "
        "(State safely persisted and resumed)\n"
        "================================================================================"
    )

    return {
        "scenario_results": scenario_results,
        "passed_scenarios": passed_scenarios,
        "total_scenarios": total_scenarios,
        "duplicate_credits_detected": duplicate_credits_detected,
        "duplicate_credit_rate_pct": duplicate_rate_pct,
        "ledger_compliance_pct": balance_report["compliance_percentage"],
        "ledger_balanced": balance_report["passed"],
        "unauthorised_credits": unauthorised_credits,
        "p95_latency_seconds": p95_latency,
        "human_review_interrupt_success_pct": human_review_success_pct,
        "scorecard": scorecard_text,
    }
