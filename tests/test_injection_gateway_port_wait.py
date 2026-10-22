"""Gateway teardown is recoverable; a live service is never replaced."""

from __future__ import annotations

import json
import queue
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from scripts.injection import e2e_cycles as cycles


def events(root):
    return [json.loads(line) for line in (root / "gateway-port-wait.jsonl").read_text().splitlines()]


def test_time_wait_clears_before_single_frozen_start(tmp_path, monkeypatch):
    clock = [0.0]
    states = iter(["time_wait", "time_wait", "available"])
    starts = []
    settings = {"alignment_gateway_port": 19192}
    original_settings = dict(settings)
    monkeypatch.setattr(cycles, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda amount: clock.__setitem__(0, clock[0] + amount)))
    monkeypatch.setattr(cycles, "_gateway_port_state", lambda _port: next(states))

    def frozen(root, config, engine):
        assert clock[0] == 2
        assert config == original_settings
        starts.append((root, config, engine))
        return "process", "http://127.0.0.1:19192", 19192

    monkeypatch.setattr(cycles.e2e, "start_gateway", frozen)
    assert cycles.start_gateway(tmp_path, settings, "engine") == ("process", "http://127.0.0.1:19192", 19192)
    assert len(starts) == 1
    recorded = events(tmp_path)
    assert [row["event"] for row in recorded] == ["wait_started", "wait_complete"]
    assert recorded[-1]["elapsed_seconds"] == 2
    assert recorded[-1]["maximum_wait_seconds"] == 90


def test_real_listener_is_rejected_without_starting_or_stopping_it(tmp_path, monkeypatch):
    monkeypatch.setattr(cycles.e2e, "start_gateway", lambda *args: pytest.fail("A live service must not be reused"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="live listener"):
            cycles.start_gateway(tmp_path, {"alignment_gateway_port": port}, "engine")
        assert listener.getsockname()[1] == port
        assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
    assert [row["event"] for row in events(tmp_path)] == ["refused_listener"]


def test_busy_port_timeout_does_not_start_gateway_and_allows_heartbeat(tmp_path, monkeypatch):
    clock = [0.0]
    requests, acknowledgements = queue.Queue(), queue.Queue()
    beats = []

    def heartbeat():
        for index in range(3):
            requests.get(timeout=2)
            beats.append(index)
            acknowledgements.put(True)

    thread = threading.Thread(target=heartbeat)
    thread.start()

    def wait(amount):
        requests.put(True)
        acknowledgements.get(timeout=2)
        clock[0] += amount

    monkeypatch.setattr(cycles, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=wait))
    monkeypatch.setattr(cycles, "_gateway_port_state", lambda _port: "time_wait")
    monkeypatch.setattr(cycles.e2e, "start_gateway", lambda *args: pytest.fail("Timeout must not start execution"))
    with pytest.raises(RuntimeError, match="no gateway or Agent was started"):
        cycles.start_gateway(tmp_path, {"alignment_gateway_port": 19192}, "engine", port_wait_seconds=3)
    thread.join(timeout=2)
    assert not thread.is_alive() and beats == [0, 1, 2]
    recorded = events(tmp_path)
    assert [row["event"] for row in recorded] == ["wait_started", "wait_timed_out"]
    assert recorded[-1]["elapsed_seconds"] == 3


@pytest.mark.skipif(sys.platform != "linux", reason="The deployment reads Linux TCP socket state")
def test_kernel_time_wait_is_distinguished_from_a_live_listener():
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        client.connect(("127.0.0.1", port))
        accepted, _ = listener.accept()
        with accepted:
            accepted.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
            client.close()
        listener.close()
    assert cycles._gateway_port_state(port) == "time_wait"


def test_listener_appearing_during_teardown_is_refused(tmp_path, monkeypatch):
    clock = [0.0]
    states = iter(["time_wait", "listener"])
    monkeypatch.setattr(cycles, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda amount: clock.__setitem__(0, clock[0] + amount)))
    monkeypatch.setattr(cycles, "_gateway_port_state", lambda _port: next(states))
    monkeypatch.setattr(cycles.e2e, "start_gateway", lambda *args: pytest.fail("A new listener must remain untouched"))
    with pytest.raises(RuntimeError, match="live listener"):
        cycles.start_gateway(tmp_path, {"alignment_gateway_port": 19192}, "engine")
    assert [row["event"] for row in events(tmp_path)] == ["wait_started", "refused_listener"]
