from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4
from sqlmodel import Field, SQLModel
from app.models.enums import AccountStatus, AccountType


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CustomerAccount(SQLModel, table=True):
    """Customer bank account entity at ResolveFlow / LenFi UK."""

    __tablename__ = "customers_accounts"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: str = Field(default="lenfi_uk", index=True)
    customer_id: str = Field(index=True)
    customer_name: str = Field(default="Benjamin Chidera")
    account_id: str = Field(index=True, unique=True, description="e.g. ACC-LEN-001")
    account_number: str = Field(default="84920101", index=True)
    sort_code: str = Field(default="04-00-75")
    account_type: AccountType = Field(default=AccountType.LIABILITY)
    currency: str = Field(default="GBP", max_length=3)
    status: AccountStatus = Field(default=AccountStatus.ACTIVE)
    pin_hash: Optional[str] = Field(default=None)
    pin_salt: Optional[str] = Field(default=None)
    is_onboarded: bool = Field(default=False)
    created_at: datetime = Field(default_factory=get_utc_now)
