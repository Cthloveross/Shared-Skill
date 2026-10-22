"""Versioned repair of OpenClaw's first-turn split-compaction guard.

Only fresh experiment-owned container layers are adapted. Native preparation,
summarization and append-only session handling remain the installed versions.
"""
from __future__ import annotations

import json
import re


POLICY = "openclaw-2026.3.11-split-turn-v1"
EVENT_QUEUE_POLICY = "openclaw-2026.3.11-split-turn-event-queue-v2"
PACKAGE_VERSION = "2026.3.11"
SOURCE = "/usr/lib/node_modules/openclaw/dist/compact-1mmJ_KWL.js"
BEFORE_SHA256 = "c84b8e207702913eddb89c1b686ce466b92b7a623a26ef62052b31b34a950f4c"
AFTER_SHA256 = "9534282ce8c5652135dffbc1457f9280fab9c968a7ef82ed6229d6a6c65f7ceb"
BEFORE = "if (!preparation.messagesToSummarize.some(isRealConversationMessage)) {"
AFTER = "if (![...preparation.messagesToSummarize, ...(preparation.turnPrefixMessages ?? [])].some(isRealConversationMessage)) {"
SDK_SOURCE = "/usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-coding-agent/dist/core/agent-session.js"
SDK_BEFORE_SHA256 = "b19e5a75f7ee6166b5a93602e745390561358a6e7f5f703bd8a3a67dea89cd90"
SDK_AFTER_SHA256 = "d3ac22833c71779d2ad1141e86dc82d21d797bef8fdffae7e94998ea841e07ed"
SDK_BEFORE = "        await this.agent.prompt(messages);\n        await this.waitForRetry();"
SDK_AFTER = "        await this.agent.prompt(messages);\n        await this._agentEventQueue;\n        await this.waitForRetry();"


def apply_native_compaction_guard(name, config):
    from scripts.wildclawbench import environment
    policy = config.get("native_compaction_guard")
    if policy not in {POLICY, EVENT_QUEUE_POLICY}:
        raise ValueError("Unknown native compaction guard policy")
    if not re.fullmatch(r"sharedskill-(?:task|evolve)-[0-9a-f]{16}", name):
        raise ValueError("Compaction adapter requires an exact experiment-owned container name")
    before = json.loads(environment._run(["docker", "inspect", name], config=config, timeout=15).stdout)[0]
    if (before.get("Name") != "/" + name or before.get("State", {}).get("Running") is not True
            or not config.get("task_image_id") or before.get("Image") != config["task_image_id"]):
        raise ValueError("Compaction container differs from its frozen task image or identity")
    contract = {"policy": policy, "version": PACKAGE_VERSION, "source": SOURCE,
        "before_sha256": BEFORE_SHA256, "after_sha256": AFTER_SHA256,
        "before": BEFORE, "after": AFTER}
    if policy == EVENT_QUEUE_POLICY:
        contract["sdk_event_queue"] = {"source": SDK_SOURCE, "version": "0.57.1",
            "before_sha256": SDK_BEFORE_SHA256, "after_sha256": SDK_AFTER_SHA256,
            "before": SDK_BEFORE, "after": SDK_AFTER}
    script = "CONTRACT=" + repr(contract) + "\n" + r'''
import hashlib,json,os
from pathlib import Path
package=Path('/usr/lib/node_modules/openclaw/package.json')
if json.loads(package.read_text())['version']!=CONTRACT['version']:
    raise RuntimeError('Unexpected installed OpenClaw version; refusing compaction adaptation')
path=Path(CONTRACT['source'])
if path.is_symlink():raise RuntimeError('Unexpected native compaction source symlink')
raw=path.read_bytes();old=CONTRACT['before'].encode();new=CONTRACT['after'].encode()
if hashlib.sha256(raw).hexdigest()!=CONTRACT['before_sha256'] or raw.count(old)!=1:
    raise RuntimeError('Unexpected native compaction source bytes; refusing adaptation')
updated=raw.replace(old,new)
if hashlib.sha256(updated).hexdigest()!=CONTRACT['after_sha256']:
    raise RuntimeError('Native compaction patch hash differs from frozen contract')
sdk=CONTRACT.get('sdk_event_queue')
if sdk:
    sdkpath=Path(sdk['source'])
    sdkpackage=sdkpath.parents[2]/'package.json'
    if sdkpath.is_symlink() or json.loads(sdkpackage.read_text())['version']!=sdk['version']:
        raise RuntimeError('Unexpected native SDK identity; refusing event queue adaptation')
    sdkraw=sdkpath.read_bytes();sdkold=sdk['before'].encode();sdknew=sdk['after'].encode()
    if hashlib.sha256(sdkraw).hexdigest()!=sdk['before_sha256'] or sdkraw.count(sdkold)!=1:
        raise RuntimeError('Unexpected native SDK bytes; refusing event queue adaptation')
    sdkupdated=sdkraw.replace(sdkold,sdknew)
    if hashlib.sha256(sdkupdated).hexdigest()!=sdk['after_sha256']:
        raise RuntimeError('Native SDK event queue patch hash differs from frozen contract')
mode=path.stat().st_mode
path.write_bytes(updated);os.chmod(path,mode)
if hashlib.sha256(path.read_bytes()).hexdigest()!=CONTRACT['after_sha256']:
    raise RuntimeError('Native compaction adaptation did not persist exact bytes')
if sdk:
    sdkmode=sdkpath.stat().st_mode
    sdkpath.write_bytes(sdkupdated);os.chmod(sdkpath,sdkmode)
    if hashlib.sha256(sdkpath.read_bytes()).hexdigest()!=sdk['after_sha256']:
        raise RuntimeError('Native SDK event queue adaptation did not persist exact bytes')
    sdk.update(original_mode=sdkmode,patched_mode=sdkpath.stat().st_mode)
print(json.dumps({**CONTRACT,'original_mode':mode,'patched_mode':path.stat().st_mode,
 'global_image_unchanged':True,'session_bytes_not_modified':True,
 'native_summarizer_unchanged':True,'history_truncation':False}))
'''
    applied = json.loads(environment._run(["docker", "exec", name, "python3", "-c", script],
                                         config=config, timeout=30).stdout)
    after = json.loads(environment._run(["docker", "inspect", name], config=config, timeout=15).stdout)[0]
    if any(before.get(key) != after.get(key) for key in ("Id", "Image", "Created", "Name")):
        raise RuntimeError("Native compaction adapter container identity changed")
    return {**applied, "container_name": name, "container_id": before["Id"],
            "container_image_id": before["Image"], "container_created": before["Created"],
            "scope": "Exact fresh container layer created by this execution; no image commit or global mutation"}
