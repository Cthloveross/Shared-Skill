"""Version-isolated adapter for one native e9c SkillClaw ``run_once``.

The formal adapter always launches the configured engine Python inside the
configured checkout.  It never imports the repository's current
``evolve_server`` package into the host process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .layout import resolve_path
from .models import EngineError, EngineRunResult, LLMCallRecord, ProtocolError, TrialDecision
from .usage import UsageLedger, conservative_token_reservation, ledger_path_from_config

E9C_COMMIT = "e9c745466f142721977735e92a291619ac536ee0"


def is_model_output_error(error_type: str | None) -> bool:
    """A received but unusable model answer is not an infrastructure outage."""

    return error_type in {"InvalidEvolverOutput", "InvalidEvolverSchema", "EmptySummary"}


class TrialEngine(Protocol):
    async def run_once(
        self,
        *,
        sessions: Sequence[Mapping[str, Any]],
        s0_path: str | Path,
        trial_dir: str | Path,
        output_dir: str | Path | None = None,
        expected_session_count: int,
    ) -> EngineRunResult: ...


def infrastructure_signature(error_type: str, message: str) -> str:
    """Collapse dynamic provider/process details into a stable breaker key."""

    normalized = str(message or "").casefold()
    normalized = re.sub(r"https?://\S+", "<url>", normalized)
    normalized = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<n>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()[:400]
    return hashlib.sha256(f"{error_type}:{normalized}".encode("utf-8")).hexdigest()[:24]


def _jsonable_usage(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else None
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    result = {field: getattr(value, field) for field in fields if getattr(value, field, None) is not None}
    return result or None


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _inference_controls(model: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    if "max_tokens" not in model:
        raise ProtocolError("model.max_tokens is required; native evolution may not use provider defaults")
    max_tokens = int(model["max_tokens"])
    if max_tokens <= 0:
        raise ProtocolError("model.max_tokens must be positive")
    chat_template_kwargs = model.get("chat_template_kwargs")
    if chat_template_kwargs != {"enable_thinking": False}:
        raise ProtocolError('model.chat_template_kwargs must be exactly {"enable_thinking": false}')
    return max_tokens, dict(chat_template_kwargs)


class RecordingBoundedLLM:
    """Transparent e9c ``chat`` interface with bounded concurrency and retry.

    When wrapping e9c's ``AsyncLLMClient`` the wrapper calls its underlying
    OpenAI client directly with SDK retries disabled.  This prevents the six
    hidden retries in the public snapshot from violating the preregistered
    single infrastructure retry, while preserving the exact messages and
    request parameters passed by the native summarizer/evolver.
    """

    def __init__(
        self,
        delegate: Any,
        *,
        log_path: str | Path,
        max_tokens: int,
        chat_template_kwargs: Mapping[str, Any],
        top_p: float | None = None,
        max_concurrency: int = 8,
        infrastructure_retries: int = 1,
        max_calls: int = 32,
        summary_timeout_seconds: float = 600.0,
        evolution_timeout_seconds: float = 1800.0,
        usage_ledger: UsageLedger | None = None,
        usage_group: str = "formal",
        usage_group_request_limit: int | None = None,
    ) -> None:
        if max_concurrency <= 0 or infrastructure_retries < 0 or max_calls <= 0 or max_tokens <= 0:
            raise ProtocolError("LLM concurrency, retry, and call limits must be valid positive bounds")
        if chat_template_kwargs != {"enable_thinking": False}:
            raise ProtocolError('chat_template_kwargs must be exactly {"enable_thinking": false}')
        self.delegate = delegate
        self.log_path = Path(log_path)
        self.max_tokens = int(max_tokens)
        self.chat_template_kwargs = dict(chat_template_kwargs)
        if top_p is not None and not 0 < top_p <= 1:
            raise ProtocolError("top_p must be in (0, 1]")
        self.top_p = top_p
        self.max_concurrency = int(max_concurrency)
        self.infrastructure_retries = int(infrastructure_retries)
        self.max_calls = int(max_calls)
        self.summary_timeout_seconds = float(summary_timeout_seconds)
        self.evolution_timeout_seconds = float(evolution_timeout_seconds)
        self.usage_ledger = usage_ledger
        self.usage_group = usage_group
        self.usage_group_request_limit = usage_group_request_limit
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._index_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._log_lock = asyncio.Lock()
        self._issued = 0
        self._dispatched = 0
        self.records: list[LLMCallRecord] = []

    @staticmethod
    def _stage(kwargs: Mapping[str, Any]) -> str:
        temperature = float(kwargs.get("temperature", 0.4))
        if temperature == 0.2:
            return "summary"
        if temperature == 0.3:
            return "merge"
        return "evolver"

    async def _next_index(self) -> int:
        async with self._index_lock:
            if self._issued >= self.max_calls:
                raise EngineError(f"native run exceeded the {self.max_calls}-call trial hard cap")
            self._issued += 1
            return self._issued

    async def _authorize_dispatch(self) -> None:
        """Reserve one trial-local provider request, including retries.

        The global ledger already charges every retry.  This second counter is
        deliberately local to one native ``run_once`` so the configured
        ``trial_request_hard_cap`` cannot be doubled by the single allowed
        infrastructure retry.
        """

        async with self._dispatch_lock:
            if self._dispatched >= self.max_calls:
                raise EngineError(f"native run exceeded the {self.max_calls}-request trial hard cap")
            self._dispatched += 1

    async def _append(self, record: LLMCallRecord) -> None:
        async with self._log_lock:
            self.records.append(record)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _provider_request(
        self,
        messages: list[dict[str, str]],
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        temperature = float(kwargs.get("temperature", getattr(self.delegate, "temperature", 0.4)))
        extra = {key: value for key, value in kwargs.items() if key not in {"max_tokens", "temperature", "extra_body"}}
        forbidden = {"model", "messages", "max_completion_tokens"}.intersection(extra)
        if forbidden:
            raise ProtocolError(f"native caller attempted to override frozen request fields: {sorted(forbidden)}")
        raw_extra_body = kwargs.get("extra_body", {})
        if not isinstance(raw_extra_body, Mapping):
            raise ProtocolError("native caller extra_body must be an object")
        extra_body = dict(raw_extra_body)
        configured = extra_body.get("chat_template_kwargs")
        if configured is not None and configured != self.chat_template_kwargs:
            raise ProtocolError("native caller chat_template_kwargs conflicts with the frozen model config")
        extra_body["chat_template_kwargs"] = dict(self.chat_template_kwargs)
        if self.top_p is not None:
            if "top_p" in extra and extra["top_p"] != self.top_p:
                raise ProtocolError("native top_p conflicts with frozen model config")
            extra["top_p"] = self.top_p
        return {
            "model": str(getattr(self.delegate, "model", "") or ""),
            "messages": messages,
            "max_completion_tokens": self.max_tokens,
            "temperature": temperature,
            **extra,
            "extra_body": extra_body,
        }

    async def _invoke(
        self,
        messages: list[dict[str, str]],
        provider_request: Mapping[str, Any],
    ) -> tuple[str, Any]:
        sync_client = getattr(self.delegate, "_client", None)
        if sync_client is not None and hasattr(sync_client, "chat"):
            if hasattr(sync_client, "with_options"):
                sync_client = sync_client.with_options(max_retries=0)
            response = await asyncio.to_thread(
                sync_client.chat.completions.create,
                **dict(provider_request),
            )
            content = response.choices[0].message.content or ""
            return str(content), getattr(response, "usage", None)

        fallback_kwargs = {
            key: value
            for key, value in provider_request.items()
            if key not in {"model", "messages", "max_completion_tokens"}
        }
        fallback_kwargs["max_tokens"] = self.max_tokens
        response = self.delegate.chat(messages, **fallback_kwargs)
        if isinstance(response, Awaitable):
            response = await response
        if isinstance(response, tuple) and len(response) == 2:
            return str(response[0] or ""), response[1]
        if isinstance(response, Mapping):
            return str(response.get("content") or ""), response.get("usage")
        return str(response or ""), None

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        call_index = await self._next_index()
        stage = self._stage(kwargs)
        timeout = self.summary_timeout_seconds if stage == "summary" else self.evolution_timeout_seconds
        request = self._provider_request(messages, kwargs)
        started_wall = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        attempts = 0
        last_error: BaseException | None = None

        async with self._semaphore:
            for attempts in range(1, self.infrastructure_retries + 2):
                usage_reservation = None
                try:
                    await self._authorize_dispatch()
                    if self.usage_ledger is not None:
                        usage_reservation = self.usage_ledger.reserve(
                            group=self.usage_group,
                            estimated_tokens=conservative_token_reservation(messages, self.max_tokens),
                            group_request_limit=self.usage_group_request_limit,
                        )
                    async with asyncio.timeout(timeout):
                        text, usage = await self._invoke(messages, request)
                    usage_value = _jsonable_usage(usage)
                    total_tokens = usage_value.get("total_tokens") if usage_value else None
                    if self.usage_ledger is not None and usage_reservation is not None:
                        self.usage_ledger.settle(
                            usage_reservation,
                            total_tokens=int(total_tokens) if isinstance(total_tokens, int) else None,
                        )
                        usage_reservation = None
                    record = LLMCallRecord(
                        call_index=call_index,
                        stage=stage,
                        started_at=started_wall,
                        elapsed_seconds=round(time.monotonic() - started, 6),
                        request=request,
                        response_text=text,
                        usage=usage_value,
                        attempts=attempts,
                    )
                    await self._append(record)
                    return text
                except BaseException as exc:  # preserve CancelledError after recording below
                    if self.usage_ledger is not None and usage_reservation is not None:
                        self.usage_ledger.settle(usage_reservation, total_tokens=None)
                    last_error = exc
                    if (
                        isinstance(exc, asyncio.CancelledError)
                        or attempts > self.infrastructure_retries
                        or not _retryable(exc)
                    ):
                        break
                    await asyncio.sleep(min(float(attempts), 2.0))

        assert last_error is not None
        record = LLMCallRecord(
            call_index=call_index,
            stage=stage,
            started_at=started_wall,
            elapsed_seconds=round(time.monotonic() - started, 6),
            request=request,
            response_text=None,
            usage=None,
            error_type=type(last_error).__name__,
            error_message=str(last_error),
            attempts=attempts,
        )
        await self._append(record)
        raise last_error


@dataclass(frozen=True)
class E9cSubprocessEngine:
    """Run the frozen native engine in its own interpreter and checkout."""

    engine_checkout: Path
    engine_python: Path
    llm_base_url: str
    llm_model: str
    llm_max_tokens: int
    llm_chat_template_kwargs: Mapping[str, Any]
    llm_api_key_env: str = "OPENAI_API_KEY"
    llm_top_p: float | None = None
    llm_temperature: float = 0.4
    expected_commit: str = E9C_COMMIT
    summary_concurrency: int = 8
    infrastructure_retries: int = 1
    request_hard_cap: int = 32
    summary_timeout_seconds: float = 600.0
    evolution_timeout_seconds: float = 1800.0
    trial_timeout_seconds: float = 3600.0
    usage_ledger_path: Path | None = None
    usage_max_requests: int = 5000
    usage_max_tokens: int = 200_000_000
    usage_group: str = "formal"
    usage_group_request_limit: int | None = 3840
    bridge_path: Path = Path(__file__).with_name("_e9c_bridge.py")

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        usage_group: str = "formal",
    ) -> "E9cSubprocessEngine":
        engine = config.get("engine") if isinstance(config.get("engine"), Mapping) else config
        model = config.get("model") if isinstance(config.get("model"), Mapping) else config
        budgets = config.get("budgets") if isinstance(config.get("budgets"), Mapping) else config
        matrix = config.get("matrix") if isinstance(config.get("matrix"), Mapping) else config
        checkout = engine.get("checkout_path") or engine.get("checkout") or engine.get("engine_checkout")
        python = engine.get("python_path") or engine.get("python") or engine.get("engine_python")
        if not checkout or not python:
            raise ProtocolError("engine.checkout_path and engine.python_path are required")
        repository_root = Path(__file__).resolve().parents[3]

        def resolve_checkout(value: Any) -> Path:
            return resolve_path(str(value), repo_root=repository_root)

        def resolve_python(value: Any) -> Path:
            # A venv's ``python`` is normally a symlink to the uv/base
            # interpreter.  Resolving it would discard pyvenv.cfg discovery
            # and therefore the engine venv's site-packages.
            return resolve_path(str(value), repo_root=repository_root, follow_symlinks=False)

        max_tokens, chat_template_kwargs = _inference_controls(model)

        return cls(
            engine_checkout=resolve_checkout(checkout),
            engine_python=resolve_python(python),
            llm_base_url=str(model.get("base_url") or model.get("llm_base_url") or ""),
            llm_model=str(model.get("id") or model.get("model") or model.get("llm_model") or ""),
            llm_max_tokens=max_tokens,
            llm_chat_template_kwargs=chat_template_kwargs,
            llm_top_p=float(model["top_p"]) if "top_p" in model else None,
            llm_temperature=float(model.get("temperature", 0.4)),
            llm_api_key_env=str(model.get("api_key_env") or "OPENAI_API_KEY"),
            expected_commit=str(engine.get("commit") or E9C_COMMIT),
            summary_concurrency=int(engine.get("summary_concurrency", matrix.get("summary_concurrency", 8))),
            infrastructure_retries=int(engine.get("infrastructure_retries", matrix.get("infra_retries", 1))),
            request_hard_cap=int(budgets.get("trial_request_hard_cap", 32)),
            summary_timeout_seconds=float(engine.get("summary_timeout_seconds", 600)),
            evolution_timeout_seconds=float(
                engine.get("evolver_timeout_seconds", engine.get("evolution_timeout_seconds", 1800))
            ),
            trial_timeout_seconds=float(
                engine.get(
                    "trial_timeout_seconds",
                    matrix.get("trial_timeout", budgets.get("trial_timeout_seconds", 3600)),
                )
            ),
            usage_ledger_path=ledger_path_from_config(config, repository_root=repository_root),
            usage_max_requests=int(budgets.get("max_model_requests_total", 5000)),
            usage_max_tokens=int(budgets.get("max_tokens_total", 200_000_000)),
            usage_group=usage_group,
            usage_group_request_limit=(
                int(budgets.get("matrix_max_model_requests", 3840)) if usage_group == "formal" else None
            ),
        )

    def validate(self) -> None:
        if self.expected_commit != E9C_COMMIT:
            raise ProtocolError(f"formal engine commit must be {E9C_COMMIT}")
        if not self.engine_checkout.is_dir():
            raise ProtocolError(f"engine checkout is missing: {self.engine_checkout}")
        if not self.engine_python.is_file() or not os.access(self.engine_python, os.X_OK):
            raise ProtocolError(f"engine Python is missing or not executable: {self.engine_python}")
        if not self.bridge_path.is_file():
            raise ProtocolError(f"e9c bridge is missing: {self.bridge_path}")
        if not self.llm_base_url or not self.llm_model:
            raise ProtocolError("engine base_url and model are required")
        if self.llm_max_tokens <= 0:
            raise ProtocolError("engine max_tokens must be positive")
        if self.llm_top_p is not None and not 0 < self.llm_top_p <= 1:
            raise ProtocolError("engine top_p must be in (0, 1]")
        if not 0 <= self.llm_temperature <= 2:
            raise ProtocolError("engine temperature must be in [0, 2]")
        if self.llm_chat_template_kwargs != {"enable_thinking": False}:
            raise ProtocolError('engine chat_template_kwargs must be exactly {"enable_thinking": false}')
        try:
            head = subprocess.run(
                ["git", "-C", str(self.engine_checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(self.engine_checkout), "status", "--porcelain", "--untracked-files=no"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolError(f"cannot verify engine checkout: {exc}") from exc
        if head != self.expected_commit:
            raise ProtocolError(f"engine checkout is {head}, expected {self.expected_commit}")
        if dirty:
            raise ProtocolError("engine checkout has tracked modifications")

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def run_once(
        self,
        *,
        sessions: Sequence[Mapping[str, Any]],
        s0_path: str | Path,
        trial_dir: str | Path,
        output_dir: str | Path | None = None,
        expected_session_count: int,
    ) -> EngineRunResult:
        self.validate()
        if len(sessions) != expected_session_count:
            raise ProtocolError(f"engine received {len(sessions)} sessions, expected {expected_session_count}")
        trial_root = Path(trial_dir).resolve()
        engine_root = trial_root / "engine"
        if engine_root.exists():
            raise ProtocolError(f"independent trial engine directory already exists: {engine_root}")
        engine_root.mkdir(parents=True)
        output_trial_root = Path(output_dir).resolve() if output_dir is not None else trial_root
        output_engine_root = output_trial_root / "engine"
        if output_engine_root != engine_root:
            if output_engine_root.exists():
                raise ProtocolError(f"independent trial output directory already exists: {output_engine_root}")
            output_engine_root.mkdir(parents=True)
        request_path = engine_root / "request.json"
        result_path = output_engine_root / "result.json"
        request = {
            "protocol": "poisoned-evolution-spreadsheetbench-e9c-v1",
            "expected_commit": self.expected_commit,
            "engine_checkout": str(self.engine_checkout),
            "repository_root": str(Path(__file__).resolve().parents[3]),
            "s0_path": str(Path(s0_path).resolve()),
            "work_dir": str(engine_root),
            "output_work_dir": str(output_engine_root),
            "group_id": "pe-single-round",
            "sessions": [dict(session) for session in sessions],
            "expected_session_count": expected_session_count,
            "llm": {
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "max_tokens": self.llm_max_tokens,
                "chat_template_kwargs": dict(self.llm_chat_template_kwargs),
                "top_p": self.llm_top_p,
                "temperature": self.llm_temperature,
                "api_key_env": self.llm_api_key_env,
                "summary_concurrency": self.summary_concurrency,
                "infrastructure_retries": self.infrastructure_retries,
                "request_hard_cap": self.request_hard_cap,
                "summary_timeout_seconds": self.summary_timeout_seconds,
                "evolution_timeout_seconds": self.evolution_timeout_seconds,
                "usage_ledger_path": str(self.usage_ledger_path) if self.usage_ledger_path else None,
                "usage_max_requests": self.usage_max_requests,
                "usage_max_tokens": self.usage_max_tokens,
                "usage_group": self.usage_group,
                "usage_group_request_limit": self.usage_group_request_limit,
            },
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")

        process = await asyncio.create_subprocess_exec(
            str(self.engine_python),
            str(self.bridge_path),
            "--request",
            str(request_path),
            "--result",
            str(result_path),
            cwd=str(self.engine_checkout),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.trial_timeout_seconds)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(process))
            raise
        except asyncio.TimeoutError:
            await self._terminate(process)
            message = f"native e9c run_once exceeded {self.trial_timeout_seconds:g}s"
            return EngineRunResult.unknown(
                error_type="TrialTimeout",
                error_message=message,
                infrastructure_signature=infrastructure_signature("TrialTimeout", message),
            )

        (output_engine_root / "stdout.log").write_bytes(stdout)
        (output_engine_root / "stderr.log").write_bytes(stderr)
        if not result_path.is_file():
            message = f"e9c bridge exited {process.returncode} without result.json"
            return EngineRunResult.unknown(
                error_type="BridgeFailure",
                error_message=message,
                infrastructure_signature=infrastructure_signature(
                    "BridgeFailure", message + ":" + stderr.decode("utf-8", errors="replace")[-500:]
                ),
            )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            message = f"invalid e9c bridge result: {exc}"
            return EngineRunResult.unknown(
                error_type="BridgeResultError",
                error_message=message,
                infrastructure_signature=infrastructure_signature("BridgeResultError", message),
            )

        if payload.get("engine_commit") != self.expected_commit:
            raise ProtocolError("e9c bridge did not prove the frozen engine commit")
        source = Path(str(payload.get("engine_source") or "")).resolve()
        try:
            source.relative_to(self.engine_checkout)
        except ValueError as exc:
            raise ProtocolError(f"bridge imported engine outside frozen checkout: {source}") from exc

        error_type = payload.get("error_type")
        error_message = payload.get("error_message")
        decision_raw = str(payload.get("decision") or "unknown")
        try:
            decision = TrialDecision(decision_raw)
        except ValueError:
            decision = TrialDecision.UNKNOWN
        signature = payload.get("infrastructure_signature")
        if is_model_output_error(error_type):
            signature = None
        elif error_type and not signature:
            signature = infrastructure_signature(str(error_type), str(error_message or ""))
        return EngineRunResult(
            decision=decision,
            summary=dict(payload.get("summary") or {}),
            summaries={str(key): str(value or "") for key, value in (payload.get("summaries") or {}).items()},
            raw_candidates=tuple(str(value or "") for value in payload.get("raw_candidates") or []),
            materialized_skills={
                str(key): str(value or "") for key, value in (payload.get("materialized_skills") or {}).items()
            },
            llm_calls=tuple(dict(value) for value in payload.get("llm_calls") or [] if isinstance(value, Mapping)),
            error_type=str(error_type) if error_type else None,
            error_message=str(error_message) if error_message else None,
            infrastructure_signature=str(signature) if signature else None,
        )
