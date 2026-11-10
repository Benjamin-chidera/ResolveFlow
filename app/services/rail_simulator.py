from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from app.services.ingestion_service import ProviderEventPayload


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaymentRailSimulator:
    """Synthetic payment rail simulator for Faster Payments / ClearBank / Modulr."""

    @staticmethod
    def emit_settled_payment(
        source: str = "FASTER_PAYMENTS",
        sender_bank: str = "Monzo",
        amount_minor: int = 50000,
        raw_reference: str = "MON-78291 ACC-LEN-001",
        provider_ref: Optional[str] = None,
        event_id: Optional[str] = None,
        currency: str = "GBP",
    ) -> ProviderEventPayload:
        """Generates a synthetic settled payment webhook payload."""
        short_token = uuid4().hex[:8].upper()
        resolved_provider_ref = provider_ref or f"FP-2026-{short_token}"
        resolved_event_id = event_id or f"EVT-{short_token}"

        return ProviderEventPayload(
            event_id=resolved_event_id,
            source=source,
            event_type="FUNDS_SETTLED",
            provider_reference=resolved_provider_ref,
            sender_bank=sender_bank,
            raw_reference=raw_reference,
            amount_minor=amount_minor,
            currency=currency,
            event_time=get_utc_now(),
        )

    @staticmethod
    def replay_event(
        original: ProviderEventPayload,
        alter_event_id: bool = False,
        shift_seconds: int = 45,
    ) -> ProviderEventPayload:
        """Simulates a duplicate webhook delivery from the payment provider.

        - If alter_event_id is False: tests Tier 1 deduplication (same source + event_id,
          even if timestamp shifts).
        - If alter_event_id is True: tests Tier 2 canonical fingerprinting (provider
          re-emitted the same wire with a new event_id and new timestamp).
        """
        new_event_id = f"EVT-RETRY-{uuid4().hex[:6].upper()}" if alter_event_id else original.event_id
        shifted_time = original.event_time + timedelta(seconds=shift_seconds)

        return ProviderEventPayload(
            event_id=new_event_id,
            source=original.source,
            event_type=original.event_type,
            provider_reference=original.provider_reference,
            sender_bank=original.sender_bank,
            raw_reference=original.raw_reference,
            amount_minor=original.amount_minor,
            currency=original.currency,
            event_time=shifted_time,
        )

