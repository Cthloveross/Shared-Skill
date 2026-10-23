"""Persist conservative local model admission limits across gateway restarts."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from scripts.wildclawbench.paid_judge import BudgetExceeded
from scripts.wildclawbench.protocol import write_json


class LocalRequestBudget:
    def __init__(self, config: dict):
        self.spec = config.get("local_request_budget")
        self.context = config.get("context_length")
        if self.spec is None:
            return
        if not isinstance(self.spec, dict) or set(self.spec) != {"path", "max_requests", "max_tokens"}:
            raise ValueError("local_request_budget requires path, max_requests and max_tokens")
        if any(type(value) is not int or value <= 0 for value in
               (self.context, self.spec["max_requests"], self.spec["max_tokens"])):
            raise ValueError("Local model limits must be positive integers")
        self.path = Path(self.spec["path"])
        if not self.path.is_absolute():
            raise ValueError("Local model budget path must be absolute")

    def reserve(self) -> dict | None:
        if self.spec is None:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            contract = {"max_requests": self.spec["max_requests"], "max_tokens": self.spec["max_tokens"],
                        "tokens_reserved_per_request": self.context}
            state = json.loads(self.path.read_text()) if self.path.exists() else {
                "schema": "local-context-reservation-v1", "contract": contract,
                "requests": 0, "reserved_tokens": 0}
            if (state.get("schema") != "local-context-reservation-v1" or state.get("contract") != contract
                    or any(type(state.get(key)) is not int or state[key] < 0
                           for key in ("requests", "reserved_tokens"))
                    or state["reserved_tokens"] != state["requests"] * self.context):
                raise ValueError("Local model budget checkpoint changed or is corrupt")
            if (state["requests"] >= contract["max_requests"]
                    or state["reserved_tokens"] + self.context > contract["max_tokens"]):
                raise BudgetExceeded("Local model request/token reservation budget exhausted")
            state["requests"] += 1
            state["reserved_tokens"] += self.context
            # A failed or interrupted request keeps its reservation. This is an
            # upper bound, never an estimate of actual usage or refunded credit.
            write_json(self.path, state)
            return dict(state)


class MediaRequestBudget:
    """Count admitted audio/image jobs without pretending their cost is tokens."""

    def __init__(self, config: dict):
        self.spec = config.get("media_request_budget")
        if self.spec is None:
            return
        if (not isinstance(self.spec, dict) or set(self.spec) != {"path", "max_requests"}
                or type(self.spec["max_requests"]) is not int or self.spec["max_requests"] <= 0):
            raise ValueError("media_request_budget requires path and positive max_requests")
        self.path = Path(self.spec["path"])
        if not self.path.is_absolute():
            raise ValueError("Media budget path must be absolute")

    def reserve(self, role: str) -> dict | None:
        if self.spec is None:
            return None
        if role not in {"audio", "image"}:
            raise ValueError("Invalid media budget role")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {
                "schema": "local-media-request-budget-v1", "max_requests": self.spec["max_requests"],
                "requests": 0, "by_role": {"audio": 0, "image": 0}}
            counts = state.get("by_role", {})
            if (state.get("schema") != "local-media-request-budget-v1"
                    or state.get("max_requests") != self.spec["max_requests"]
                    or set(counts) != {"audio", "image"}
                    or any(type(n) is not int or n < 0 for n in [state.get("requests"), *counts.values()])
                    or state["requests"] != sum(counts.values())):
                raise ValueError("Media budget checkpoint changed or is corrupt")
            if state["requests"] >= self.spec["max_requests"]:
                raise BudgetExceeded("Media request budget exhausted")
            state["requests"] += 1
            state["by_role"][role] += 1
            write_json(self.path, state)
            return state
