from enum import Enum


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"


class AccountType(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class ProviderPaymentStatus(str, Enum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    REVERSED = "REVERSED"
    FAILED = "FAILED"


class AllocationStatus(str, Enum):
    UNALLOCATED = "UNALLOCATED"
    ALLOCATED = "ALLOCATED"
    ON_HOLD = "ON_HOLD"


class CaseState(str, Enum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    WAITING_PROVIDER = "WAITING_PROVIDER"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class ProposedAction(str, Enum):
    AUTO_ALLOCATE = "AUTO_ALLOCATE"
    WAIT_AND_RECHECK = "WAIT_AND_RECHECK"
    TRACE_WITH_PROVIDER = "TRACE_WITH_PROVIDER"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ApprovalDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"


class MessageSenderRole(str, Enum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


class EventVisibility(str, Enum):
    CUSTOMER_SAFE = "CUSTOMER_SAFE"
    INTERNAL_ONLY = "INTERNAL_ONLY"






