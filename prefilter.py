"""
prefilter.py
Cheap deterministic pre-filter executed BEFORE calling Gemini, to save API calls on
offers that explicitly state a USD hourly rate below the configured minimum.

It is intentionally conservative: it only rejects when an explicit USD hourly rate is
found and ALL detected rates are below the minimum. Anything ambiguous goes to Gemini.
"""

from __future__ import annotations

import os
import re
from typing import Optional

_HOURLY_RE = re.compile(
    r"\$\s?(\d{1,3}(?:\.\d+)?)"                       # first amount
    r"(?:\s*(?:-|–|to)\s*\$?\s?(\d{1,3}(?:\.\d+)?))?"  # optional range end
    r"\s*(?:usd)?\s*(?:/|per\s+)\s*(?:hr|hour)\b",
    re.IGNORECASE,
)


def extract_hourly_rates_usd(text: str) -> list[float]:
    """Returns every explicit USD hourly amount found (range ends included)."""
    rates: list[float] = []
    for match in _HOURLY_RE.finditer(text or ""):
        for group in match.groups():
            if group:
                rates.append(float(group))
    return rates


def quick_reject_reason(
    salary: str,
    description: str,
    is_india: bool = False,
    min_hourly_rate: Optional[float] = None,
) -> Optional[str]:
    """
    Returns a human-readable rejection reason, or None if the job should go on to Gemini.
    """
    if is_india and os.getenv("ALLOW_INDIA_JOBS", "false").strip().lower() in ("true", "1", "yes"):
        return None  # regional rate rules are handled by the LLM

    if min_hourly_rate is None:
        try:
            min_hourly_rate = float(os.getenv("MIN_HOURLY_RATE", "20"))
        except ValueError:
            min_hourly_rate = 20.0

    rates = extract_hourly_rates_usd(f"{salary}\n{description}")
    if rates and max(rates) < min_hourly_rate:
        return f"Tarifa explicita en USD/h (max ${max(rates):g}) por debajo del minimo ${min_hourly_rate:g}/h."
    return None
