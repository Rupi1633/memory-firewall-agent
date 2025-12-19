# agent/memmachine_client.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests


class MemMachineClient:
    """
    Minimal client wrapper.

    Mode A (preferred for hackathon): real MemMachine via endpoint + API key.
    Mode B (fallback): local JSON persistence so you can keep building/demoing.
    """

    def __init__(self, endpoint: str, api_key: str, namespace: str) -> None:
        self.endpoint = (endpoint or "").rstrip("/")
        self.api_key = api_key or ""
        self.namespace = namespace or "memory_firewall_demo"

        # Fallback local store (persist across restarts)
        self._local_path = os.path.join(os.getcwd(), f".memmachine_fallback_{self.namespace}.json")

    def _enabled(self) -> bool:
        return bool(self.endpoint and self.api_key)

    # -----------------------
    # Public API expected by app.py
    # -----------------------
    def store_constraint(self, user_id: str, constraint_dict: Dict[str, Any]) -> None:
        """
        Stores a constraint as a durable memory item.
        """
        if self._enabled():
            self._store_remote(user_id, constraint_dict)
        else:
            self._store_local(user_id, constraint_dict)

    def list_constraints(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Returns all stored constraint items for the user.
        """
        if self._enabled():
            return self._list_remote(user_id)
        return self._list_local(user_id)

    # -----------------------
    # Fallback local store
    # -----------------------
    def _read_local(self) -> Dict[str, Any]:
        if not os.path.exists(self._local_path):
            return {"namespace": self.namespace, "users": {}}
        with open(self._local_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_local(self, data: Dict[str, Any]) -> None:
        with open(self._local_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _store_local(self, user_id: str, constraint_dict: Dict[str, Any]) -> None:
        data = self._read_local()
        users = data.setdefault("users", {})
        items = users.setdefault(user_id, [])

        # Upsert by id
        cid = constraint_dict.get("id")
        items = [x for x in items if x.get("id") != cid]
        items.append(constraint_dict)
        users[user_id] = items
        self._write_local(data)

    def _list_local(self, user_id: str) -> List[Dict[str, Any]]:
        data = self._read_local()
        return data.get("users", {}).get(user_id, [])

    # -----------------------
    # Remote MemMachine (adjust endpoints to actual MemMachine docs)
    # -----------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _store_remote(self, user_id: str, constraint_dict: Dict[str, Any]) -> None:
        """
        TODO: Replace URL/path with MemMachine's actual API.
        """
        url = f"{self.endpoint}/memories"
        payload = {
            "namespace": self.namespace,
            "user_id": user_id,
            "type": "policy_constraint",
            "content": constraint_dict,
        }
        r = requests.post(url, headers=self._headers(), json=payload, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"MemMachine store failed ({r.status_code}): {r.text}")

    def _list_remote(self, user_id: str) -> List[Dict[str, Any]]:
        """
        TODO: Replace URL/path with MemMachine's actual API.
        """
        url = f"{self.endpoint}/memories"
        params = {"namespace": self.namespace, "user_id": user_id, "type": "policy_constraint"}
        r = requests.get(url, headers=self._headers(), params=params, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"MemMachine list failed ({r.status_code}): {r.text}")

        data = r.json()
        # Expected shape may differ; adapt accordingly.
        # We assume: {"items":[{"content":{...}}, ...]}
        items = data.get("items", [])
        return [it.get("content", {}) for it in items]

