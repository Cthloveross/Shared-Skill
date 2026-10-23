"""Actual private worker processes and HTTP; never load a model or touch CUDA."""

import asyncio
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from scripts.wildclawbench.services import auxiliary_server as server
from scripts.wildclawbench.services import auxiliary_worker as managed


@pytest.fixture
def fake_worker(tmp_path):
    script = tmp_path / 'worker.py'
    script.write_text('''import json,os,signal,subprocess,sys,time
from pathlib import Path
from fastapi import FastAPI
import uvicorn
app=FastAPI()
root=Path(sys.argv[2])
@app.get('/healthz')
def health(): return {'ready':True}
@app.post('/v1/images/generations')
def image(body:dict):
    if body.get('slow'):
        child="import signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(2);Path(%r).write_text('late')" % str(root/'late')
        process=subprocess.Popen([sys.executable,'-c',child], start_new_session=True)
        (root/'child.json').write_text(json.dumps({'pid':process.pid}))
        (root/'started').write_text('started')
        time.sleep(10)
    return {'ready':True,'pid':os.getpid()}
uvicorn.run(app,uds=sys.argv[1],log_level='error',access_log=False)
''')
    return [sys.executable, '-B', str(script), '{socket}', str(tmp_path)]


def config(tmp_path, **overrides):
    return {'runtime_dir': str(tmp_path / 'state'), 'auxiliary_limits': {
        'model_load_timeout_seconds': 5, 'queue_timeout_seconds': .2,
        'request_timeout_seconds': .5, **overrides}}


def test_hard_timeout_kills_worker_and_detached_child_then_allows_reload(tmp_path, fake_worker):
    app = managed.create_proxy('image', config(tmp_path), command=fake_worker)
    with TestClient(app) as client:
        first = client.post('/v1/images/generations', json={})
        first_pid = first.json()['pid']
        failed = client.post('/v1/images/generations', json={'slow': True})
        assert failed.status_code == 504
        assert (tmp_path / 'started').exists()
        child_pid = json.loads((tmp_path / 'child.json').read_text())['pid']
        assert managed.identity(first_pid) is None or managed.identity(first_pid)[1] == 'Z'
        assert managed.identity(child_pid) is None or managed.identity(child_pid)[1] == 'Z'
        assert not app.state.worker.alive()
        # Reload has its own bounded request budget. No worker is silently left active.
        app.state.worker.limits['request_timeout_seconds'] = 3
        recovered = client.post('/v1/images/generations', json={})
        assert recovered.status_code == 200 and recovered.json()['pid'] != first_pid
    time.sleep(2)
    assert not (tmp_path / 'late').exists()
    records = [json.loads(line) for line in (app.state.worker.directory / 'worker-stops.jsonl').read_text().splitlines()]
    assert all(item['quiescent'] for item in records)


def test_real_http_client_disconnect_kills_inference(tmp_path, fake_worker):
    import threading
    import uvicorn
    app = managed.create_proxy('image', config(tmp_path, request_timeout_seconds=10), command=fake_worker)
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen()
    port = listener.getsockname()[1]
    service = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, ws='none'))
    thread = threading.Thread(target=service.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        limit = time.monotonic() + 6
        while not service.started and thread.is_alive() and time.monotonic() < limit:
            time.sleep(.02)
        assert service.started
        request = json.dumps({'slow': True}).encode()
        with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
            client.sendall((f'POST /v1/images/generations HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(request)}\r\n\r\n').encode() + request)
            limit = time.monotonic() + 3
            while not (tmp_path / 'started').exists() and time.monotonic() < limit:
                time.sleep(.01)
            assert (tmp_path / 'started').exists()
        limit = time.monotonic() + 3
        while app.state.worker.alive() and time.monotonic() < limit:
            time.sleep(.02)
        assert not app.state.worker.alive()
    finally:
        service.should_exit = True
        thread.join(timeout=6)
        listener.close()
        assert not thread.is_alive()
    time.sleep(2)
    assert not (tmp_path / 'late').exists()


def test_queue_size_and_cross_service_flock_have_hard_bounds(tmp_path):
    async def exercise():
        worker = managed.Worker('audio', config(tmp_path, queue_size=1, queue_timeout_seconds=.1))
        async with worker.slot():
            with pytest.raises(HTTPException) as full:
                async with worker.slot():
                    pytest.fail('must not queue beyond configured capacity')
            assert full.value.status_code == 429
        with worker.gpu_lock.open('a') as occupied:
            fcntl.flock(occupied, fcntl.LOCK_EX | fcntl.LOCK_NB)
            start = time.monotonic()
            with pytest.raises(HTTPException) as deadline:
                async with worker.slot():
                    pytest.fail('must not acquire another owner lock')
            assert deadline.value.status_code == 503 and time.monotonic() - start < 1
        assert worker.waiters == 0 and not worker.lock.locked()
        async with worker.slot():
            pass
        worker.socket_directory.cleanup()
    asyncio.run(exercise())


def test_expired_deadline_and_oversized_body_never_enter_worker(tmp_path, fake_worker):
    app = managed.create_proxy('image', config(tmp_path, max_body_bytes=16), command=fake_worker)
    with TestClient(app) as client:
        original = app.state.worker.process.pid
        assert client.post('/v1/images/generations', content='x' * 17).status_code == 413
        assert client.post('/v1/images/generations', json={}, headers={'X-Request-Deadline-Unix': 'nan'}).status_code == 400
        expired = client.post('/v1/images/generations', json={}, headers={'X-Request-Deadline-Unix': str(time.time() - 1)})
        assert expired.status_code in {503, 504}
        assert app.state.worker.process.pid == original and app.state.worker.alive()


def test_generation_limits_and_shared_resource_paths(tmp_path):
    assert managed.model_root({'runtime_dir': str(tmp_path / 'state'), 'shared_runtime_dir': str(tmp_path / 'shared')}) == tmp_path / 'shared/models'
    for settings in ({'unknown': 1}, {'max_audio_tokens': True}, {'request_timeout_seconds': 0}, {'queue_size': .5}):
        with pytest.raises(ValueError):
            managed.limits({'auxiliary_limits': settings})
    good = {'prompt': 'a blue dot', 'size': '256x256', 'steps': 1, 'n': 1}
    assert server.image_parameters(good)[:4] == (256, 256, 1, 1)
    for bad in ({'steps': 100000}, {'n': 5}, {'size': '9999x9999'}, {'guidance_scale': float('nan')},
                {'prompt': 'x' * 20001}, {'seed': -1}, {'n': True}):
        with pytest.raises(ValueError):
            server.image_parameters({**good, **bad})


def test_audio_probe_has_a_real_process_timeout(tmp_path, monkeypatch):
    audio = tmp_path / 'audio.wav'; audio.write_bytes(b'nonempty')
    executable = tmp_path / 'ffmpeg'
    executable.write_text('#!/bin/sh\nsleep 5\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setitem(server.LIMITS, 'ffmpeg_timeout_seconds', .1)
    # Use shell builtin read to avoid requiring another executable on the test PATH.
    executable.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(5)\n')
    with pytest.raises(subprocess.TimeoutExpired):
        server.audio_file(audio)


@pytest.mark.parametrize('extension,codec', [('wav', 'pcm_s16le'), ('mp3', 'libmp3lame'),
    ('flac', 'flac'), ('ogg', 'libvorbis'), ('opus', 'libopus'), ('aac', 'aac')])
def test_actual_ffmpeg_audio_formats_are_bounded_without_ffprobe(tmp_path, monkeypatch, extension, codec):
    import shutil
    import wave
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('A real ffmpeg is required for codec acceptance')
    audio = tmp_path / ('input.' + extension)
    subprocess.run([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.25', '-c:a', codec, '-y', str(audio)],
                   check=True, timeout=10, capture_output=True)
    monkeypatch.setitem(server.LIMITS, 'max_audio_seconds', .5)
    duration = server.audio_file(audio)
    assert 0 < duration <= .5
    normalized = audio.with_name('input.validated.wav')
    with wave.open(str(normalized), 'rb') as result:
        assert result.getframerate() == 16000 and result.getnchannels() == 1 and result.getsampwidth() == 2
    long_audio = tmp_path / ('long.' + extension)
    subprocess.run([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=1', '-c:a', codec, '-y', str(long_audio)],
                   check=True, timeout=10, capture_output=True)
    with pytest.raises(ValueError, match='duration'):
        server.audio_file(long_audio)
    assert (tmp_path / 'long.validated.wav').stat().st_size <= int(.6 * 16000 * 2) + 4096


def test_queued_disconnect_preserves_active_worker(tmp_path, fake_worker):
    from types import SimpleNamespace
    class Request:
        method = 'POST'
        headers = {'content-type': 'application/json'}
        url = SimpleNamespace(path='/v1/images/generations')
        base_url = 'http://local/'
        disconnected = False
        async def is_disconnected(self): return self.disconnected

    async def exercise():
        worker = managed.Worker('image', config(tmp_path, request_timeout_seconds=5,
                                               queue_timeout_seconds=3, queue_size=2), fake_worker)
        active, queued = Request(), Request()
        await worker.ensure_started()
        first = asyncio.create_task(worker.forward(active, b'{"slow":true}', time.monotonic() + 5))
        second = None
        try:
            limit = time.monotonic() + 2
            while not (tmp_path / 'started').exists() and time.monotonic() < limit:
                if first.done():
                    result = await first
                    pytest.fail(f'Active request finished before inference: {result.status_code} {result.body}')
                await asyncio.sleep(.01)
            assert (tmp_path / 'started').exists()
            pid = worker.process.pid
            second = asyncio.create_task(worker.forward(queued, b'{}', time.monotonic() + 5))
            await asyncio.sleep(.05)
            queued.disconnected = True
            with pytest.raises(HTTPException) as cancelled:
                await second
            assert cancelled.value.status_code == 499
            assert worker.alive() and worker.process.pid == pid and not first.done()
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            if second is not None:
                second.cancel()
                await asyncio.gather(second, return_exceptions=True)
            worker.stop('test cleanup')
            worker.socket_directory.cleanup()
    asyncio.run(exercise())


def test_failed_stop_fences_service_instead_of_restarting(tmp_path, monkeypatch):
    worker = managed.Worker('image', config(tmp_path))
    worker.process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(20)'])
    process = worker.process
    worker.ticks = managed.identity(process.pid)[2]
    try:
        with monkeypatch.context() as patch:
            patch.setattr(managed, 'stop_tree', lambda *args: {'quiescent': False, 'survivors': [process.pid]})
            worker.stop('failed confirmation')
        with pytest.raises(HTTPException) as fenced:
            asyncio.run(worker.ensure_started())
        assert fenced.value.status_code == 503 and worker.process.pid == process.pid
    finally:
        worker.stop('test cleanup')
        worker.socket_directory.cleanup()


def test_owned_media_command_dies_when_worker_parent_is_killed(tmp_path):
    marker, pidfile = tmp_path / 'late-command', tmp_path / 'command-pid'
    command = ('import os,time;from pathlib import Path;'
               f'Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(2);'
               f'Path({str(marker)!r}).write_text("late")')
    owner_code = ('from scripts.wildclawbench.services.auxiliary_worker import owned_command;'
                  f'owned_command([{sys.executable!r},"-c",{command!r}],timeout=10)')
    parent = subprocess.Popen([sys.executable, '-B', '-c', owner_code])
    try:
        limit = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < limit:
            time.sleep(.01)
        assert pidfile.exists()
        child = int(pidfile.read_text())
        parent.kill(); parent.wait(timeout=2)
        limit = time.monotonic() + 2
        while (item := managed.identity(child)) and item[1] not in {'Z', 'X'} and time.monotonic() < limit:
            time.sleep(.01)
        assert managed.identity(child) is None or managed.identity(child)[1] in {'Z', 'X'}
        time.sleep(2)
        assert not marker.exists()
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait(timeout=2)


def test_control_uses_shared_resources_and_only_current_run_state(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from scripts.wildclawbench.services import auxiliary_control as control
    settings = {'runtime_dir': str(tmp_path / 'current'), 'shared_runtime_dir': str(tmp_path / 'shared'),
                'model_base_url': 'http://127.0.0.1:1/v1', 'audio_base_url': 'http://127.0.0.1:2/v1',
                'image_base_url': 'http://127.0.0.1:3/v1', 'gpu_index': 1}
    arguments = SimpleNamespace(config=tmp_path / 'frozen-service.json', action='status', kind='audio')
    monkeypatch.setattr(control, 'service_config', lambda parser: (arguments, settings))
    def inspect(action, kind):
        assert control.RUNTIME == tmp_path / 'current'
        assert control.PYTHON == tmp_path / 'shared/auxiliary-venv/bin/python'
        assert control.MODELS == tmp_path / 'shared/models'
        assert control.DOWNLOADS == tmp_path / 'shared/auxiliary-downloads.json'
        assert control.CONFIG_PATH == arguments.config
        assert control.GPU == '1' and control.PORTS == {'audio': 2, 'image': 3}
        return {'validated': True}
    monkeypatch.setattr(control, 'run', inspect)
    control.main()
    assert json.loads(capsys.readouterr().out)['validated']
    assert not (tmp_path / 'shared').exists(), 'A read-only status/config check must not populate shared resources'


def test_long_runtime_and_tmpdir_use_a_short_private_socket(tmp_path, fake_worker, monkeypatch):
    long_runtime = tmp_path / ('long-runtime-' * 12)
    long_scratch = long_runtime / 'auxiliary-image/tmp'
    long_scratch.mkdir(parents=True)
    monkeypatch.setenv('TMPDIR', str(long_scratch))
    monkeypatch.setattr(managed.tempfile, 'tempdir', str(long_scratch))
    app = managed.create_proxy('image', {**config(tmp_path), 'runtime_dir': str(long_runtime)}, command=fake_worker)
    path = app.state.worker.socket
    assert str(path).startswith('/tmp/sharedskill-aux-') and len(os.fsencode(path)) < 108
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        record = json.loads((app.state.worker.directory / 'worker-process.json').read_text())
        assert record['socket'] == str(path) and record['socket_directory_mode'] == '0700'
    assert not path.parent.exists()
