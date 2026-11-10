from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID, uuid4
from sqlmodel import Field, Relationship, SQLModel


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JournalEntry(SQLModel, table=True):
    """Balanced double-entry journal container."""

    __tablename__ = "journal_entries"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    journal_id: str = Field(index=True, unique=True, description="Human-readable ID e.g. JRN-001")
    idempotency_key: str = Field(index=True, unique=True)
    reference: str = Field(index=True, description="Payment ID or case reference")
    description: str
    posted_at: datetime = Field(default_factory=get_utc_now)

    lines: List["LedgerEntry"] = Relationship(back_populates="journal")


class LedgerEntry(SQLModel, table=True):
    """Individual debit or credit posting line."""

    __tablename__ = "ledger_entries"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    journal_id: UUID = Field(foreign_key="journal_entries.id", index=True)
    account_id: str = Field(index=True, description="Account code e.g. ACC-SETTLEMENT-CASH, ACC-LEN-001")
    debit_minor: int = Field(default=0, ge=0)
    credit_minor: int = Field(default=0, ge=0)
    source_payment_id: Optional[str] = Field(default=None, index=True)
    posted_at: datetime = Field(default_factory=get_utc_now)

    journal: Optional[JournalEntry] = Relationship(back_populates="lines")

