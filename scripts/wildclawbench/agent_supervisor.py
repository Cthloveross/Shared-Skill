"""Bound one Agent process tree; leave independently started mock services alone."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[0], fields[19]
    except (OSError, ValueError, IndexError):
        return None


def processes(owner):
    # Do not scan the host's unrelated /proc entries on a deadline-critical path.
    result, pending, seen = {}, [owner], set()
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        for path in Path(f'/proc/{parent}/task').glob('*/children'):
            try:
                children = [int(value) for value in path.read_text().split()]
            except (OSError, ValueError):
                continue
            for pid in children:
                item = identity(pid)
                if item is not None:
                    result[pid] = item
                    pending.append(pid)
    return result


def stop_children(owner: int) -> dict:
    identities = {}
    # Stop parents before killing, so detached grandchildren cannot keep forking.
    for _ in range(8):
        table = processes(owner)
        children = set(table)
        fresh = False
        for pid in children:
            if table[pid][1] in {'Z', 'X'}:
                continue
            identities[pid] = table[pid][2]
            if table[pid][1] not in {'T', 't'}:
                try:
                    os.kill(pid, signal.SIGSTOP)
                    fresh = True
                except ProcessLookupError:
                    pass
        if not fresh:
            break
        time.sleep(.01)
    for pid, ticks in identities.items():
        if (identity(pid) or (None, None, None))[2] == ticks:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 2
    survivors = []
    while time.monotonic() < deadline:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        table = processes(owner)
        survivors = [pid for pid in table if table[pid][1] not in {'Z', 'X'}]
        if not survivors:
            break
        time.sleep(.01)
    return {'stopped_process_identities': identities, 'surviving_writers': survivors,
            'quiescent': not survivors, 'quiescent_at_unix': time.time()}


def supervise(command: list[str], timeout: float, evidence: Path) -> int:
    # Orphans from double-fork/setsid remain our children, rather than escaping to init.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot establish Agent subreaper')
    started = time.time()
    limit = time.monotonic() + timeout
    child = subprocess.Popen(command, start_new_session=True)
    expired = False
    while child.poll() is None:
        if time.monotonic() >= limit:
            expired = True
            break
        time.sleep(min(.02, max(0, limit - time.monotonic())))
    exit_code = child.poll()
    stopped = stop_children(os.getpid())
    record = {'policy': 'agent-subtree-stop-v1', 'agent_pid': child.pid,
              'agent_started_unix': started, 'timeout_seconds': timeout,
              'deadline_unix': started + timeout, 'deadline_expired': expired,
              'agent_exit_code': exit_code, **stopped}
    evidence.parent.mkdir(parents=True, exist_ok=True)
    temporary = evidence.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.replace(evidence)
    return 70 if not stopped['quiescent'] else 124 if expired else (exit_code or 0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or args.timeout <= 0:
        parser.error('A command and positive timeout are required')
    raise SystemExit(supervise(command, args.timeout, args.evidence))
