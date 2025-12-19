# agent/policy.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4


# Constraint types (keep these stable across your app)
NO_MEETINGS_AFTER_HOUR = "NO_MEETINGS_AFTER_HOUR"
BUDGET_CAP = "BUDGET_CAP"
NO_SHARING_WITH_EXTERNALS = "NO_SHARING_WITH_EXTERNALS"

HARD = "HARD"
SOFT = "SOFT"


@dataclass
class ParsedConstraint:
    id: str
    type: str
    severity: str
    text: str
    params: Dict[str, Any]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _parse_time_to_hour(text: str) -> Optional[int]:
    """
    Extract an hour in 24h form from phrases like:
      - "after 9pm"
      - "after 21:00"
      - "after 9 p.m."
    Returns hour int (0-23) or None.
    """
    t = _normalize(text)

    # Matches: after 9pm / after 9 pm / after 9 p.m.
    m = re.search(r"\bafter\s+(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\b", t)
    if m:
        h = int(m.group(1))
        ampm = m.group(2).replace(".", "")
        if ampm == "pm" and h != 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h

    # Matches: after 21:00 or after 21
    m2 = re.search(r"\bafter\s+(\d{1,2})(?::\d{2})?\b", t)
    if m2:
        h = int(m2.group(1))
        if 0 <= h <= 23:
            return h

    return None


def _parse_money_amount(text: str) -> Optional[float]:
    """
    Extract a numeric amount from "$1000", "1000", "1,200", "1200.50".
    Returns float or None.
    """
    t = _normalize(text)

    # Prefer $ patterns
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", t)
    if m:
        return float(m.group(1).replace(",", ""))

    # Fallback: 'budget cap 1000' or 'max 1200'
    m2 = re.search(r"\b(?:cap|max(?:imum)?)\s*[:=]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\b", t)
    if m2:
        return float(m2.group(1).replace(",", ""))

    return None


def parse_constraint(user_text: str) -> Tuple[Optional[ParsedConstraint], Optional[str]]:
    """
    Returns (ParsedConstraint, None) if recognized, else (None, error_message).
    Supports exactly three constraint types:
      1) No meetings after HOUR
      2) Budget cap $X
      3) No sharing with external contractors
    """
    raw = (user_text or "").strip()
    t = _normalize(raw)

    # 1) No meetings after 9pm
    # Trigger keywords: meeting/call + after
    if ("meeting" in t or "call" in t) and "after" in t:
        hour = _parse_time_to_hour(t)
        if hour is None:
            return None, "Could not parse the time. Example: 'No meetings after 9pm'."
        c = ParsedConstraint(
            id=f"c-{uuid4().hex[:8]}",
            type=NO_MEETINGS_AFTER_HOUR,
            severity=HARD,
            text=raw,
            params={"hour": hour},
        )
        return c, None

    # 2) Budget cap $X
    if ("budget" in t and "cap" in t) or ("max" in t and "budget" in t) or ("budget cap" in t) or ("spend" in t and "max" in t):
        amt = _parse_money_amount(t)
        if amt is None:
            return None, "Could not parse the amount. Example: 'Budget cap $1000'."
        c = ParsedConstraint(
            id=f"c-{uuid4().hex[:8]}",
            type=BUDGET_CAP,
            severity=HARD,
            text=raw,
            params={"max_amount": amt},
        )
        return c, None

    # 3) Do not share datasets with external contractors
    # Trigger: share/send + dataset/file/data + external/contractor
    share_words = any(w in t for w in ["share", "send", "export", "give", "forward"])
    data_words = any(w in t for w in ["dataset", "data", "file", "files"])
    ext_words = any(w in t for w in ["contractor", "external", "third party", "3rd party", "vendor"])
    deny_words = any(w in t for w in ["never", "do not", "don't", "no "])

    if share_words and data_words and ext_words:
        banned_party = "contractor" if "contractor" in t else "external"
        c = ParsedConstraint(
            id=f"c-{uuid4().hex[:8]}",
            type=NO_SHARING_WITH_EXTERNALS,
            severity=HARD,
            text=raw,
            params={"banned_party": banned_party},
        )
        return c, None

    # Not recognized
    return None, (
        "Unrecognized constraint. Supported examples:\n"
        "1) No meetings after 9pm\n"
        "2) Budget cap $1000\n"
        "3) Never share datasets with external contractors"
    )
