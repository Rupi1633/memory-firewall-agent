# agent/orchestrator.py
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from agent.neo4j_client import Neo4jClient, ActionPayload
from agent.policy import (
    BUDGET_CAP,
    NO_MEETINGS_AFTER_HOUR,
    NO_SHARING_WITH_EXTERNALS,
)

# Action types
SCHEDULE_MEETING = "SCHEDULE_MEETING"
SHARE_DATA = "SHARE_DATA"
SPEND_MONEY = "SPEND_MONEY"
UNKNOWN = "UNKNOWN"


@dataclass
class Decision:
    ok: bool
    action_id: str
    action_type: str
    message: str
    violations: List[Dict[str, Any]]
    alternatives: List[str]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def classify_action(user_request: str) -> str:
    t = _norm(user_request)

    if any(w in t for w in ["meeting", "call", "schedule", "book", "invite"]):
        return SCHEDULE_MEETING

    if any(w in t for w in ["share", "send", "export", "forward", "upload"]) and any(
        w in t for w in ["dataset", "data", "file", "files", "csv"]
    ):
        return SHARE_DATA

    if any(w in t for w in ["buy", "purchase", "spend", "pay", "order"]) or "$" in t:
        return SPEND_MONEY

    return UNKNOWN


def _parse_time_to_hour(text: str) -> Optional[int]:
    """
    Extract hour (0-23) from:
      - 10:30pm, 10pm, 9 p.m., 21:00
    """
    t = _norm(text)

    # 10:30pm / 10pm
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", t)
    if m:
        h = int(m.group(1))
        ampm = m.group(3).replace(".", "")
        if ampm == "pm" and h != 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return h

    # 21:00 or 21
    m2 = re.search(r"\b(\d{1,2})(?::\d{2})\b", t)
    if m2:
        h = int(m2.group(1))
        if 0 <= h <= 23:
            return h

    return None


def _parse_amount(text: str) -> Optional[float]:
    t = _norm(text)
    m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", t)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _mentions_external_party(text: str) -> bool:
    t = _norm(text)
    return any(w in t for w in ["contractor", "external", "third party", "3rd party", "vendor"])


def _alternatives_for_meeting(max_hour: int) -> List[str]:
    # Simple alternatives, no calendar integration.
    # max_hour e.g. 21 means suggest <= 21.
    safe_hour = min(max_hour, 20)  # suggest 8pm by default
    return [
        f"Schedule it at {safe_hour}:00 (8pm) instead of after {max_hour}:00.",
        "Schedule it tomorrow at 8:00pm.",
        "If it must be late, ask for an explicit exception/override first."
    ]


def _alternatives_for_sharing() -> List[str]:
    return [
        "Share a redacted/synthetic dataset instead of the full customer dataset.",
        "Share only aggregated metrics or schema, not raw records.",
        "Route the request through an approved internal channel or get written approval."
    ]


def _alternatives_for_budget(max_amount: float) -> List[str]:
    return [
        f"Reduce scope to stay within the ${max_amount:.0f} budget cap.",
        "Request approval to increase budget (one-time exception).",
        "Split the purchase into phases or use a lower-cost alternative."
    ]


def evaluate_request(
    *,
    user_id: str,
    user_request: str,
    constraints: List[Dict[str, Any]],  # fetched from MemMachine (source of truth)
    neo: Neo4jClient,
) -> Decision:
    """
    constraints items should look like:
      {
        "constraint_id" or "id": "...",
        "type": "...",
        "severity": "HARD",
        "params": {...},
        "text": "..."
      }
    """
    action_type = classify_action(user_request)
    action_id = f"a-{uuid4().hex[:10]}"

    # Record action in graph
    neo.record_action(
        user_id,
        ActionPayload(
            id=action_id,
            type=action_type,
            text=user_request,
            ts=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        ),
    )

    violations: List[Tuple[str, str]] = []  # (constraint_id, reason)
    alternatives: List[str] = []

    # Normalize constraints input keys
    normalized_constraints: List[Dict[str, Any]] = []
    for c in constraints:
        cid = c.get("constraint_id") or c.get("id") or c.get("constraintId")
        normalized_constraints.append(
            {
                "id": cid,
                "type": c.get("type"),
                "severity": c.get("severity", "HARD"),
                "params": c.get("params", {}) or {},
                "text": c.get("text", ""),
            }
        )

    # --- Check A: meeting time ---
    if action_type == SCHEDULE_MEETING:
        req_hour = _parse_time_to_hour(user_request)
        # Find matching constraint
        for c in normalized_constraints:
            if c["type"] == NO_MEETINGS_AFTER_HOUR:
                max_hour = int(c["params"].get("hour", 21))
                if req_hour is not None and req_hour > max_hour:
                    violations.append((c["id"], f"Requested meeting at {req_hour}:00 exceeds allowed end hour {max_hour}:00"))
                    alternatives.extend(_alternatives_for_meeting(max_hour))

    # --- Check B: external sharing ---
    if action_type == SHARE_DATA:
        if _mentions_external_party(user_request):
            for c in normalized_constraints:
                if c["type"] == NO_SHARING_WITH_EXTERNALS:
                    violations.append((c["id"], "Request involves external/contractor sharing, which is prohibited"))
                    alternatives.extend(_alternatives_for_sharing())

    # --- Check C: budget cap ---
    if action_type == SPEND_MONEY:
        amt = _parse_amount(user_request)
        for c in normalized_constraints:
            if c["type"] == BUDGET_CAP:
                cap = float(c["params"].get("max_amount", 0))
                if amt is not None and cap > 0 and amt > cap:
                    violations.append((c["id"], f"Requested spend ${amt:.2f} exceeds budget cap ${cap:.2f}"))
                    alternatives.extend(_alternatives_for_budget(cap))

    # If violations found, materialize in graph and return explainability
    if violations:
        for cid, reason in violations:
            neo.record_violation(action_id, cid, reason)

        explain = neo.explain_violations(user_id, action_id)

        # Deduplicate alternatives
        uniq_alts = []
        seen = set()
        for a in alternatives:
            if a not in seen:
                uniq_alts.append(a)
                seen.add(a)

        return Decision(
            ok=False,
            action_id=action_id,
            action_type=action_type,
            message="Blocked: request violates one or more persistent constraints.",
            violations=explain,
            alternatives=uniq_alts[:5],
        )

    # Otherwise approve
    return Decision(
        ok=True,
        action_id=action_id,
        action_type=action_type,
        message="Approved: no constraint violations detected.",
        violations=[],
        alternatives=[],
    )
