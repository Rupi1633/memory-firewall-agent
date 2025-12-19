# app.py
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent.neo4j_client import Neo4jClient, ConstraintPayload
from agent.memmachine_client import MemMachineClient
from agent.policy import parse_constraint
from agent.orchestrator import evaluate_request

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")

MEMMACHINE_ENDPOINT = os.getenv("MEMMACHINE_ENDPOINT", "")
MEMMACHINE_API_KEY = os.getenv("MEMMACHINE_API_KEY", "")
MEMMACHINE_NAMESPACE = os.getenv("MEMMACHINE_NAMESPACE", "memory_firewall_demo")

USER_ID = os.getenv("USER_ID", "demo_user")

app = FastAPI(title="Memory Firewall Agent", version="0.1.0")

neo = Neo4jClient(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
mem = MemMachineClient(
    endpoint=MEMMACHINE_ENDPOINT,
    api_key=MEMMACHINE_API_KEY,
    namespace=MEMMACHINE_NAMESPACE,
)


class ConstraintIn(BaseModel):
    text: str


class RequestIn(BaseModel):
    text: str


@app.on_event("startup")
def _startup() -> None:
    try:
        neo.ensure_schema()
        neo.upsert_user(USER_ID)
    except Exception as e:
        # Don’t hide errors; make them obvious early
        raise RuntimeError(f"Neo4j startup failed: {e}") from e


@app.on_event("shutdown")
def _shutdown() -> None:
    neo.close()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "neo4j_uri": NEO4J_URI,
        "memmachine_enabled": bool(MEMMACHINE_ENDPOINT and MEMMACHINE_API_KEY),
        "namespace": MEMMACHINE_NAMESPACE,
        "user_id": USER_ID,
    }


@app.post("/constraints")
def add_constraint(payload: ConstraintIn) -> Dict[str, Any]:
    parsed, err = parse_constraint(payload.text)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # 1) Store in MemMachine (source of truth)
    mem.store_constraint(
        user_id=USER_ID,
        constraint_dict={
            "id": parsed.id,
            "type": parsed.type,
            "severity": parsed.severity,
            "text": parsed.text,
            "params": parsed.params,
        },
    )

    # 2) Mirror into Neo4j for graph reasoning/explainability
    neo.upsert_constraint(
        user_id=USER_ID,
        constraint=ConstraintPayload(
            id=parsed.id,
            type=parsed.type,
            severity=parsed.severity,
            text=parsed.text,
            params=parsed.params,
        ),
    )

    return {
        "ok": True,
        "constraint": {
            "id": parsed.id,
            "type": parsed.type,
            "severity": parsed.severity,
            "text": parsed.text,
            "params": parsed.params,
        },
    }


@app.post("/request")
def process_request(payload: RequestIn) -> Dict[str, Any]:
    # Retrieve constraints from MemMachine
    constraints: List[Dict[str, Any]] = mem.list_constraints(user_id=USER_ID)

    decision = evaluate_request(
        user_id=USER_ID,
        user_request=payload.text,
        constraints=constraints,
        neo=neo,
    )

    return {
        "ok": decision.ok,
        "action_id": decision.action_id,
        "action_type": decision.action_type,
        "message": decision.message,
        "violations": decision.violations,
        "alternatives": decision.alternatives,
    }
