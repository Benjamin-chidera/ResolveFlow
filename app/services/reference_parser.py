import re
from typing import Optional

# Matches either "ACC-LEN-001" or shorthand "LEN-001" (case-insensitive)
ACCOUNT_CODE_PATTERN = re.compile(r"\b(?:ACC-)?(LEN-\d{3})\b", re.IGNORECASE)


def parse_beneficiary_account_id(raw_reference: str) -> Optional[str]:
    """Extracts a single unambiguous customer account ID from a raw payment reference.

    Rules:
    - Normalizes 'LEN-001' or 'ACC-LEN-001' to 'ACC-LEN-001'.
    - If zero account codes are present (e.g. 'MON-33112 BEN CHIDERA'), returns None.
    - If multiple distinct account codes are present (e.g. 'LEN-001 OR LEN-002'),
      returns None so the payment is flagged as ambiguous rather than guessing.
    """
    if not raw_reference:
        return None

    matches = ACCOUNT_CODE_PATTERN.findall(raw_reference)
    unique_codes = {f"ACC-{match.upper()}" for match in matches}

    if len(unique_codes) == 1:
        return next(iter(unique_codes))

    return None

