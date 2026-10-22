"""Process-safe request and token accounting for the whole reproduction.

The corpus collector and the isolated e9c subprocess both use this ledger.
Requests are charged before dispatch and token reservations remain charged if a
process dies, so a resume cannot silently exceed the preregistered totals.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .layout import resolve_path


class UsageBudgetExceeded(RuntimeError):
    """A provider request would cross a frozen usage limit."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def conservative_token_reservation(messages: Any, max_completion_tokens: int) -> int:
    """Return a safe upper bound for one chat request.

    UTF-8 byte length bounds input token count for the tokenizers used here.
    The extra 4 Ki tokens cover chat framing and provider-added metadata.
    """

    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return max(1, len(encoded) + max(0, int(max_completion_tokens)) + 4096)


@dataclass(frozen=True)
class UsageReservation:
    reservation_id: str
    group: str
    reserved_tokens: int


class UsageLedger:
    """Small JSON ledger guarded by a separate POSIX advisory-lock file."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_requests: int,
        max_tokens: int,
    ) -> None:
        if max_requests <= 0 or max_tokens <= 0:
            raise ValueError("global request and token limits must be positive")
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_requests = int(max_requests)
        self.max_tokens = int(max_tokens)

    def _initial(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "limits": {"requests": self.max_requests, "tokens": self.max_tokens},
            "totals": {"requests": 0, "tokens_consumed": 0, "tokens_reserved": 0},
            "groups": {},
            "reservations": {},
            "created_at": _now(),
            "updated_at": _now(),
        }

    def _load_locked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._initial()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise RuntimeError(f"invalid usage ledger: {self.path}")
        limits = value.get("limits") or {}
        if limits != {"requests": self.max_requests, "tokens": self.max_tokens}:
            raise RuntimeError("usage ledger limits differ from the frozen config")
        return value

    def _write_locked(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _mutate(self, operation):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            value = self._load_locked()
            result = operation(value)
            value["updated_at"] = _now()
            self._write_locked(value)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return result

    def reserve(
        self,
        *,
        group: str,
        estimated_tokens: int,
        group_request_limit: int | None = None,
        group_token_limit: int | None = None,
    ) -> UsageReservation:
        if not group or estimated_tokens <= 0:
            raise ValueError("usage group and positive token reservation are required")
        if group_request_limit is not None and group_request_limit <= 0:
            raise ValueError("group request limit must be positive")
        if group_token_limit is not None and group_token_limit <= 0:
            raise ValueError("group token limit must be positive")

        reservation_id = f"{os.getpid()}-{time.time_ns()}-{uuid.uuid4().hex[:12]}"

        def apply(value: dict[str, Any]) -> UsageReservation:
            totals = value["totals"]
            groups = value.setdefault("groups", {})
            bucket = groups.setdefault(
                group,
                {"requests": 0, "tokens_consumed": 0, "tokens_reserved": 0},
            )
            if int(totals["requests"]) + 1 > self.max_requests:
                raise UsageBudgetExceeded(f"global {self.max_requests}-request hard cap exhausted")
            held = int(totals["tokens_consumed"]) + int(totals["tokens_reserved"])
            if held + estimated_tokens > self.max_tokens:
                raise UsageBudgetExceeded(f"global {self.max_tokens}-token hard cap would be exceeded")
            if group_request_limit is not None and int(bucket["requests"]) + 1 > group_request_limit:
                raise UsageBudgetExceeded(f"{group} request hard cap exhausted")
            group_held = int(bucket["tokens_consumed"]) + int(bucket["tokens_reserved"])
            if group_token_limit is not None and group_held + estimated_tokens > group_token_limit:
                raise UsageBudgetExceeded(f"{group} token hard cap would be exceeded")
            totals["requests"] = int(totals["requests"]) + 1
            totals["tokens_reserved"] = int(totals["tokens_reserved"]) + estimated_tokens
            bucket["requests"] = int(bucket["requests"]) + 1
            bucket["tokens_reserved"] = int(bucket["tokens_reserved"]) + estimated_tokens
            value.setdefault("reservations", {})[reservation_id] = {
                "group": group,
                "reserved_tokens": estimated_tokens,
                "created_at": _now(),
            }
            return UsageReservation(reservation_id, group, estimated_tokens)

        return self._mutate(apply)

    def settle(self, reservation: UsageReservation, *, total_tokens: int | None) -> int:
        """Settle a dispatched request.

        Missing usage consumes the whole conservative reservation. This is
        intentionally pessimistic because a timed-out provider may still have
        generated tokens.
        """

        def apply(value: dict[str, Any]) -> int:
            raw = value.setdefault("reservations", {}).pop(reservation.reservation_id, None)
            if not isinstance(raw, dict):
                raise RuntimeError("usage reservation is missing or was already settled")
            reserved = int(raw["reserved_tokens"])
            group = str(raw["group"])
            consumed = reserved if total_tokens is None else int(total_tokens)
            if consumed < 0:
                raise ValueError("reported token usage cannot be negative")
            if consumed > reserved:
                raise RuntimeError("provider token usage exceeded the conservative reservation")
            totals = value["totals"]
            bucket = value["groups"][group]
            totals["tokens_reserved"] = int(totals["tokens_reserved"]) - reserved
            totals["tokens_consumed"] = int(totals["tokens_consumed"]) + consumed
            bucket["tokens_reserved"] = int(bucket["tokens_reserved"]) - reserved
            bucket["tokens_consumed"] = int(bucket["tokens_consumed"]) + consumed
            return consumed

        return self._mutate(apply)

    def snapshot(self) -> dict[str, Any]:
        return self._mutate(lambda value: json.loads(json.dumps(value)))


def ledger_path_from_config(config: Mapping[str, Any], *, repository_root: Path) -> Path:
    paths = config.get("paths") if isinstance(config.get("paths"), Mapping) else {}
    explicit = paths.get("usage_ledger")
    if explicit:
        return resolve_path(str(explicit), repo_root=repository_root)
    formal = paths.get("formal_outputs")
    if formal:
        root = resolve_path(str(formal), repo_root=repository_root)
        return root / "global-usage-ledger.json"
    root = resolve_path(str(paths.get("outputs_dir") or "outputs"), repo_root=repository_root)
    return root / "poisoned-evolution-global-usage-ledger.json"


def ledger_from_config(config: Mapping[str, Any], *, repository_root: Path) -> UsageLedger:
    budgets = config.get("budgets") if isinstance(config.get("budgets"), Mapping) else {}
    return UsageLedger(
        ledger_path_from_config(config, repository_root=repository_root),
        max_requests=int(budgets.get("max_model_requests_total", 5000)),
        max_tokens=int(budgets.get("max_tokens_total", 200_000_000)),
    )
