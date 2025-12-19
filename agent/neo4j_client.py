# agent/neo4j_client.py
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase


@dataclass
class ConstraintPayload:
    id: str
    type: str
    severity: str
    text: str
    params: Dict[str, Any]


@dataclass
class ActionPayload:
    id: str
    type: str
    text: str
    ts: Optional[str] = None  # ISO timestamp string (optional)


class Neo4jClient:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    # ---------------------------
    # Optional: constraints/indexes
    # ---------------------------
    def ensure_schema(self) -> None:
        cypher_statements = [
            "CREATE CONSTRAINT user_id_unique IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE",
            "CREATE CONSTRAINT constraint_id_unique IF NOT EXISTS FOR (c:Constraint) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT action_id_unique IF NOT EXISTS FOR (a:Action) REQUIRE a.id IS UNIQUE",
            "CREATE CONSTRAINT timewindow_id_unique IF NOT EXISTS FOR (t:TimeWindow) REQUIRE t.id IS UNIQUE",
            "CREATE CONSTRAINT resource_id_unique IF NOT EXISTS FOR (r:Resource) REQUIRE r.id IS UNIQUE",
        ]
        with self._driver.session() as session:
            for stmt in cypher_statements:
                session.run(stmt)

    # ---------------------------
    # Core writes
    # ---------------------------
    def upsert_user(self, user_id: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (u:User {id:$user_id}) RETURN u",
                user_id=user_id,
            )

    def upsert_constraint(self, user_id: str, constraint: ConstraintPayload) -> None:
        """
        Creates/updates a Constraint node and links it to the user.
        Also materializes supporting nodes for certain types:
          - NO_MEETINGS_AFTER_HOUR -> creates/links TimeWindow(0..hour)
          - NO_SHARING_WITH_EXTERNALS -> creates/links Resource(kind='party', name='external/contractor')
        """
        params_json = json.dumps(constraint.params, ensure_ascii=False)

        with self._driver.session() as session:
            # Create/Update constraint + link to user
            session.run(
                """
                MERGE (u:User {id:$user_id})
                MERGE (c:Constraint {id:$cid})
                SET c.type=$ctype,
                    c.severity=$severity,
                    c.text=$text,
                    c.params_json=$params_json
                MERGE (u)-[:HAS_CONSTRAINT]->(c)
                """,
                user_id=user_id,
                cid=constraint.id,
                ctype=constraint.type,
                severity=constraint.severity,
                text=constraint.text,
                params_json=params_json,
            )

            # Type-specific graph structures for explainability
            if constraint.type == "NO_MEETINGS_AFTER_HOUR":
                # Expect constraint.params["hour"] (e.g., 21)
                hour = int(constraint.params.get("hour", 21))
                tw_id = f"tw-0-{hour}"
                session.run(
                    """
                    MATCH (c:Constraint {id:$cid})
                    MERGE (tw:TimeWindow {id:$tw_id})
                    SET tw.startHour=0, tw.endHour=$hour
                    MERGE (c)-[:REQUIRES_TIMEWINDOW]->(tw)
                    """,
                    tw_id=tw_id,
                    hour=hour,
                    cid=constraint.id,
                )


            if constraint.type == "NO_SHARING_WITH_EXTERNALS":
                banned_party = str(constraint.params.get("banned_party", "external")).lower()
                r_id = f"party-{banned_party}"
                session.run(
                    """
                    MERGE (r:Resource {id:$rid})
                    SET r.kind='party', r.name=$name
                    MATCH (c:Constraint {id:$cid})
                    MERGE (c)-[:BANS_RESOURCE]->(r)
                    """,
                    rid=r_id,
                    name=banned_party,
                    cid=constraint.id,
                )

    def record_action(self, user_id: str, action: ActionPayload) -> None:
        with self._driver.session() as session:
            session.run(
                """
                MERGE (u:User {id:$user_id})
                MERGE (a:Action {id:$aid})
                SET a.type=$atype,
                    a.text=$text
                FOREACH (_ IN CASE WHEN $ts IS NULL THEN [] ELSE [1] END |
                    SET a.ts = $ts
                )
                MERGE (u)-[:REQUESTED]->(a)
                """,
                user_id=user_id,
                aid=action.id,
                atype=action.type,
                text=action.text,
                ts=action.ts,
            )

    def record_violation(self, action_id: str, constraint_id: str, reason: str) -> None:
        with self._driver.session() as session:
            session.run(
                """
                MATCH (a:Action {id:$aid})
                MATCH (c:Constraint {id:$cid})
                MERGE (a)-[v:VIOLATES]->(c)
                SET v.reason=$reason
                """,
                aid=action_id,
                cid=constraint_id,
                reason=reason,
            )

    # ---------------------------
    # Explainability query (your “wow”)
    # ---------------------------
    def explain_violations(self, user_id: str, action_id: str) -> List[Dict[str, Any]]:
        """
        Returns a list of dicts with the violated constraint(s) and optional
        linked TimeWindow/Resource details.
        """
        cypher = """
        MATCH (u:User {id:$user_id})-[:HAS_CONSTRAINT]->(c:Constraint),
              (a:Action {id:$action_id})-[:VIOLATES]->(c)
        OPTIONAL MATCH (c)-[:REQUIRES_TIMEWINDOW]->(tw:TimeWindow)
        OPTIONAL MATCH (c)-[:BANS_RESOURCE]->(r:Resource)
        RETURN c.id AS constraint_id,
               c.type AS type,
               c.severity AS severity,
               c.text AS text,
               c.params_json AS params_json,
               tw.startHour AS startHour,
               tw.endHour AS endHour,
               r.kind AS bannedKind,
               r.name AS bannedName
        """
        with self._driver.session() as session:
            result = session.run(cypher, user_id=user_id, action_id=action_id)
            rows: List[Dict[str, Any]] = []
            for rec in result:
                params_json = rec.get("params_json")
                rows.append(
                    {
                        "constraint_id": rec.get("constraint_id"),
                        "type": rec.get("type"),
                        "severity": rec.get("severity"),
                        "text": rec.get("text"),
                        "params": json.loads(params_json) if params_json else {},
                        "time_window": (
                            {"startHour": rec.get("startHour"), "endHour": rec.get("endHour")}
                            if rec.get("endHour") is not None
                            else None
                        ),
                        "banned_resource": (
                            {"kind": rec.get("bannedKind"), "name": rec.get("bannedName")}
                            if rec.get("bannedName") is not None
                            else None
                        ),
                    }
                )
            return rows
