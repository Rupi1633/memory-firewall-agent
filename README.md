# Memory Firewall Agent (MemMachine + Neo4j)

## Problem
Most agents “remember” conversational context but do not enforce persistent user constraints. This causes unsafe or non-compliant actions (late meetings, prohibited data sharing, overspending).

## Solution
A constraint-aware agent that:
1) Stores durable user constraints in MemMachine (long-term memory)
2) Mirrors constraints and actions into Neo4j
3) Blocks or approves requests based on constraints
4) Explains violations using graph traversals (Action -> VIOLATES -> Constraint)

## Tech Stack
- MemVerge MemMachine (persistent memory)
- Neo4j Graph Database (explainable reasoning paths)
- FastAPI (demo API)

## How MemMachine is used
Each constraint is stored as a durable memory record:
- `{id, type, severity, params, text}`
On every request, the agent retrieves constraints and enforces them.

## How Neo4j is used
Nodes:
- User, Constraint, Action, TimeWindow, Resource  
Edges:
- HAS_CONSTRAINT, REQUESTED, VIOLATES, REQUIRES_TIMEWINDOW, BANS_RESOURCE

Explain query:
```cypher
MATCH (u:User {id:$user_id})-[:HAS_CONSTRAINT]->(c:Constraint),
      (a:Action {id:$action_id})-[:VIOLATES]->(c)
OPTIONAL MATCH (c)-[:REQUIRES_TIMEWINDOW]->(tw:TimeWindow)
OPTIONAL MATCH (c)-[:BANS_RESOURCE]->(r:Resource)
RETURN c, tw, r;
