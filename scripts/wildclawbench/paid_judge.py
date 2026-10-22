"""Host-only DeepSeek credentials, fixed judge parameters and durable cost ceiling."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
from pathlib import Path

from scripts.wildclawbench.protocol import write_json


class BudgetExceeded(ValueError):
    pass


def credential(config: dict) -> str:
    name = config.get("credential_env_var", "DEEPSEEK_API_KEY")
    value = os.environ.get(name)
    if not value and config.get("credential_file"):
        # Parse data; never source a user-controlled shell file.
        path = Path(config["credential_file"])
        for line in path.read_text().splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
            if match and match[1] == name:
                parts = shlex.split(match[2], comments=True)
                value = parts[0] if len(parts) == 1 else None
    if not value:
        raise ValueError("External judge credential is missing; configure the host credential file")
    return value


def judge_payload(body: dict, config: dict) -> dict:
    if body.get("tools") or body.get("stream"):
        raise ValueError("Judge accepts non-streaming evaluations without tool calls")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Judge requires messages")
    # Use a small allowlist: Qwen/OpenRouter-only fields must never reach DeepSeek.
    result = {"model": config["judge_model_id"], "messages": messages,
              "max_tokens": int(config.get("judge_max_tokens", 16384)),
              "thinking": {"type": "enabled"},
              "reasoning_effort": config.get("judge_reasoning_effort", "high"),
              "top_p": 0.95, "stream": False}
    if body.get("response_format"):
        result["response_format"] = body["response_format"]
    return result


class CostLedger:
    """Reserve worst-case peak cost atomically; missing usage consumes the reservation.

    The provider's 1M context bound is rounded up to 1,048,576 tokens. Reserving
    that bound also covers unknown vision tokenization. This is an upper-bound
    budget ledger, not a claim about the provider's final invoice.
    """

    INPUT_LIMIT = 1_048_576
    HIT_RATE = 0.006
    MISS_RATE = 0.30
    OUTPUT_RATE = 1.20

    def __init__(self, config: dict):
        self.directory = Path(config["runtime_dir"]) / "paid-api"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "budget.json"
        self.cap = float(config["paid_api_budget_usd"])
        self.max_output = int(config.get("judge_max_tokens", 16384))
        if not 0 < self.max_output <= 16384 or not 0 < self.cap <= 10:
            raise ValueError("Judge output and paid budget exceed this experiment's authorized limits")

    @contextlib.contextmanager
    def state(self):
        with (self.directory / "budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = json.loads(self.path.read_text()) if self.path.exists() else {
                "budget_usd": self.cap, "cost_upper_bound_usd": 0.0, "requests_settled": 0,
                "missing_usage_requests": 0, "pending": {},
                "accounting": "Frozen peak rates, cache usage when present; missing usage reserves full request cost",
            }
            if value["budget_usd"] != self.cap:
                raise ValueError("Paid budget changed after ledger creation")
            yield value
            write_json(self.path, value)

    def reserve(self, request_id: str) -> float:
        amount = (self.INPUT_LIMIT * self.MISS_RATE + self.max_output * self.OUTPUT_RATE) / 1_000_000
        with self.state() as state:
            committed = state["cost_upper_bound_usd"] + sum(state["pending"].values())
            if committed + amount > self.cap:
                raise BudgetExceeded("Paid judge budget exhausted; scoring remains pending")
            if request_id in state["pending"]:
                raise ValueError("Duplicate paid request identity")
            state["pending"][request_id] = amount
        return amount

    def settle(self, request_id: str, usage: dict | None) -> dict:
        with self.state() as state:
            reserved = state["pending"].pop(request_id)
            valid = isinstance(usage, dict) and all(
                isinstance(usage.get(k), int) and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens")
            )
            if valid:
                prompt, output = usage["prompt_tokens"], usage["completion_tokens"]
                hit = usage.get("prompt_cache_hit_tokens", 0)
                if not isinstance(hit, int) or not 0 <= hit <= prompt:
                    hit = 0
                cost = ((prompt - hit) * self.MISS_RATE + hit * self.HIT_RATE + output * self.OUTPUT_RATE) / 1_000_000
                valid = cost <= reserved + 1e-9
            if not valid:
                cost = reserved
                state["missing_usage_requests"] += 1
            state["cost_upper_bound_usd"] += cost
            state["requests_settled"] += 1
            return {"cost_upper_bound_usd": cost, "usage_available": valid,
                    "usage": usage, "reserved_usd": reserved,
                    "rates_usd_per_million": {"input_cache_hit": self.HIT_RATE,
                                               "input_cache_miss": self.MISS_RATE,
                                               "output": self.OUTPUT_RATE}}
