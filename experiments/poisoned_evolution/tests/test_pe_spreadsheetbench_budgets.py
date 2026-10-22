from __future__ import annotations

import pytest

from experiments.poisoned_evolution.spreadsheetbench.engine import RecordingBoundedLLM
from experiments.poisoned_evolution.spreadsheetbench.models import EngineError
from experiments.poisoned_evolution.spreadsheetbench.usage import (
    UsageBudgetExceeded,
    UsageLedger,
)


def test_global_ledger_is_shared_across_groups_and_keeps_crash_reservations(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json", max_requests=2, max_tokens=1000)
    first = ledger.reserve(group="collection", estimated_tokens=600, group_request_limit=2)
    ledger.settle(first, total_tokens=100)
    ledger.reserve(group="formal", estimated_tokens=600, group_request_limit=2)

    # The second reservation models a process crash. It remains charged even
    # though no settlement was written, and both phases share the request cap.
    with pytest.raises(UsageBudgetExceeded, match="global 2-request"):
        ledger.reserve(group="evolution_smoke", estimated_tokens=1)
    snapshot = ledger.snapshot()
    assert snapshot["totals"] == {
        "requests": 2,
        "tokens_consumed": 100,
        "tokens_reserved": 600,
    }
    assert len(snapshot["reservations"]) == 1


def test_global_ledger_refuses_request_before_token_cap_can_be_crossed(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json", max_requests=10, max_tokens=100)
    with pytest.raises(UsageBudgetExceeded, match="global 100-token"):
        ledger.reserve(group="formal", estimated_tokens=101)
    assert ledger.snapshot()["totals"]["requests"] == 0


@pytest.mark.asyncio
async def test_recording_llm_counts_each_provider_retry_in_global_ledger(tmp_path):
    class Delegate:
        max_tokens = 10

        def __init__(self) -> None:
            self.attempts = 0

        async def chat(self, _messages, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("temporary failure after dispatch")
            return "ok", {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}

    ledger = UsageLedger(tmp_path / "usage.json", max_requests=10, max_tokens=100_000)
    llm = RecordingBoundedLLM(
        Delegate(),
        log_path=tmp_path / "calls.jsonl",
        max_tokens=10,
        chat_template_kwargs={"enable_thinking": False},
        infrastructure_retries=1,
        max_calls=2,
        usage_ledger=ledger,
        usage_group="formal",
        usage_group_request_limit=3840,
    )
    assert await llm.chat([{"role": "user", "content": "hello"}], temperature=0.2) == "ok"
    snapshot = ledger.snapshot()
    assert snapshot["totals"]["requests"] == 2
    assert snapshot["groups"]["formal"]["requests"] == 2
    assert llm.records[0].attempts == 2


@pytest.mark.asyncio
async def test_trial_request_cap_counts_provider_retries(tmp_path):
    class Delegate:
        max_tokens = 10

        def __init__(self) -> None:
            self.attempts = 0

        async def chat(self, _messages, **_kwargs):
            self.attempts += 1
            raise ConnectionError("retryable provider failure")

    delegate = Delegate()
    ledger = UsageLedger(tmp_path / "usage.json", max_requests=10, max_tokens=100_000)
    llm = RecordingBoundedLLM(
        delegate,
        log_path=tmp_path / "calls.jsonl",
        max_tokens=10,
        chat_template_kwargs={"enable_thinking": False},
        infrastructure_retries=1,
        max_calls=1,
        usage_ledger=ledger,
        usage_group="formal",
        usage_group_request_limit=3840,
    )

    with pytest.raises(EngineError, match="1-request trial hard cap"):
        await llm.chat([{"role": "user", "content": "hello"}], temperature=0.2)

    snapshot = ledger.snapshot()
    assert delegate.attempts == 1
    assert snapshot["totals"]["requests"] == 1
    assert snapshot["groups"]["formal"]["requests"] == 1
    assert llm.records[0].attempts == 2
