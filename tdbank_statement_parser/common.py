from decimal import Decimal
import re

import dateparser
from dateparser import date

DEBIT: str = "debit"
CREDIT: str = "credit"


def get_date(s: str) -> date:
    # Handle date strings with no spaces like "Dec082024"
    if s and len(s) >= 6:
        # Try to insert spaces for formats like "Dec082024" (3 letters + day + year)
        if re.match(r'^[A-Za-z]{3}\d{6}', s):
            s = re.sub(r'^([A-Za-z]{3})(\d{2})(\d{4})', r'\1 \2 \3', s)
    if parsed := dateparser.parse(s):
        return parsed.date()


def normalize_date(metadata: dict, datestr: str) -> date:
    candidate_years = [
        metadata["statement_period_start"].year - 1,
        metadata["statement_period_start"].year,
        metadata["statement_period_start"].year + 1,
    ]
    candidates = [
        get_date(f"{datestr}/{year}")
        for year in candidate_years
        if get_date(f"{datestr}/{year}")
    ]

    for candidate in candidates:
        if metadata["statement_period_start"] <= candidate <= metadata["statement_period_end"]:
            return candidate

    if candidates:
        return min(
            candidates,
            key=lambda d: abs(d - metadata["statement_period_start"]),
        )

    return get_date(f"{datestr}/{metadata['statement_period_start'].year}")


def to_decimal(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))
