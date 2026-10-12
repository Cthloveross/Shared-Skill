"""Execute genuine OpenClaw tasks and the pinned benchmark's Automated Checks.

Each invocation gets a clean container and mock state. Raw agent output, inputs,
check code, API outcomes and replayable mock responses are retained. A scoring
transport failure is pending_score, never a measured zero. Rescoring restores
the previous task output instead of running the Agent again.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import environment


def _save(path: Path, obj: Any) -> None:
    environment._write_json(path, obj)


def _docker(args: list[str], config: Mapping[str, Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return environment._run(["docker", *args], config=config, **kwargs)


def _copy_in(path: Path, name: str, destination: str, config: Mapping[str, Any]) -> None:
    _docker(["cp", str(path), f"{name}:{destination}"], config, timeout=180)


def _copy_out(name: str, source: str, path: Path, config: Mapping[str, Any], *, required: bool = True) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _docker(["cp", f"{name}:{source}", str(path)], config, timeout=300, check=False)
    if result.returncode and required:
        raise RuntimeError("Artifact collection failed: " + result.stderr)
    return result.returncode == 0


def _local_tools(api_url: str, model: str) -> str:
    return f"""# Local execution tools

This experiment uses local services only. No paid remote model or Brave API is
available. OPENROUTER_BASE_URL and OPENAI_BASE_URL point to {api_url}; their API
keys are local placeholders. Model: {model}. These environment names remain
compatible with the original task specifications.
Paid model provider hosts are blocked in this container; use the local endpoints.
Cloud Edge TTS is also blocked. Speech uses the local Qwen Omni Chelsie/Ethan
voices; a requested Microsoft Xiaoxiao voice is a separately recorded local
environment substitution, not an available Microsoft voice.

The OpenClaw browser can visit public pages and search-engine result pages for
free web search. The browser remains available; paid web_search is disabled.
All supplied shared skills are in /root/skills and in the OpenClaw skill catalog.

Use the following OpenAI-compatible local interfaces when a task needs media.
They create actual media through the configured local adapter. A service error
means the service is unavailable; preserve the error and report it.

```python
import os, base64
from openai import OpenAI
client = OpenAI(base_url=os.environ['OPENROUTER_BASE_URL'],
                api_key=os.environ['OPENROUTER_API_KEY'])
# Vision (put the real image in a data URL):
# client.chat.completions.create(model='{model}', messages=[...])

# Image generation; write the returned real image bytes to the required file.
image = client.images.generate(model='local-image', prompt='YOUR IMAGE PROMPT',
                               size='1024x1024', n=1, response_format='b64_json')
open('/tmp_workspace/output.png','wb').write(base64.b64decode(image.data[0].b64_json))

# Speech synthesis, including Chinese text when the local backend supports it.
audio = client.audio.speech.create(model='local-tts', voice='Chelsie',
                                  input='实际中文文本', response_format='wav')
audio.stream_to_file('/tmp_workspace/speech.wav')

# Audio understanding / transcription: pass actual WAV bytes. The gateway
# selects its local audio adapter from input_audio; no remote model is called.
wav = base64.b64encode(open('/tmp_workspace/audio.wav','rb').read()).decode()
answer = client.chat.completions.create(model='local-audio', messages=[{{
    'role':'user','content':[{{'type':'text','text':'Transcribe the actual audio.'}},
    {{'type':'input_audio','input_audio':{{'data':wav,'format':'wav'}}}}]}}])
print(answer.choices[0].message.content)
```

ffmpeg/ffprobe can extract audio, frames, clips, merge tracks and render video.
Image editing, layouts and diagrams can also be rendered using code and local
tools; API generation is only needed when the requested artifact requires it.
"""


def task_spec(task_id: str, config: Mapping[str, Any]) -> dict[str, Any]:
    source = environment.paths(config)["upstream"]
    sys.path.insert(0, str(source))
    from src.utils.task_parser import parse_task_md

    requested = config.get("task", {}).get("source_path")
    if requested and Path(requested).exists():
        file = Path(requested)
    else:
        candidates = list((source / "tasks").glob("*/*task_*.md"))
        file = next((p for p in candidates if p.stem == task_id), None)
        if file is None:
            match = re.fullmatch(r"(\d{2})\.(\d{1,2})", task_id)
            if match:
                prefix = match.group(1)
                number = int(match.group(2))
                file = next(
                    (
                        p
                        for p in candidates
                        if p.parent.name.startswith(prefix + "_") and re.search(r"_task_" + str(number) + r"_", p.name)
                    ),
                    None,
                )
        if file is None:
            raise KeyError(f"Unknown benchmark task {task_id}")
    spec = parse_task_md(file)
    # Resolve a reference copy of a task against the actual pinned runtime.
    raw = file.read_text()
    match = re.search(r"## Workspace Path\s*\n\s*```[^\n]*\n([^\n]+)", raw)
    if match:
        spec["workspace_path"] = str((source / match.group(1).strip()).resolve())
    spec["skills_path"] = str(source / "skills")
    return spec


def _variants(spec: dict[str, Any], config: Mapping[str, Any], output: Path, api_url: str) -> dict[str, Any]:
    patched = dict(spec)
    substitutions: list[dict[str, str]] = []
    model = str(config.get("model_id", "qwen3.8-27b"))
    for field in ("prompt", "automated_checks", "warmup"):
        original = str(patched.get(field, ""))
        current = original
        for remote in () if field == "prompt" else ("https://openrouter.ai/api/v1", "https://api.openai.com/v1"):
            if remote in current:
                current = current.replace(remote, api_url)
                substitutions.append({"field": field, "before": remote, "after": api_url})
        if "ocrbench" in spec["task_id"].lower():
            for requested in ("gpt-5-mini-2025-08-07",):
                if requested in current:
                    current = current.replace(requested, model)
                    substitutions.append({"field": field, "before": requested, "after": model})
        patched[field] = current
    if "ocrbench" in spec["task_id"].lower():
        patched["prompt"] += "\n\n" + _OCR_PROTOCOL
        patched["automated_checks"] = _OCR_ENV_CHECKS
        substitutions.append(
            {
                "field": "automated_checks",
                "before": "GPT5-mini score range 810-835",
                "after": "0.2 genuine VLMEvalKit environment + 0.8 complete auditable local-Qwen OCRBench evaluation",
            }
        )
    patched["env"] = "\n".join(
        sorted(
            set(str(spec.get("env", "")).splitlines()) | {"OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "JUDGE_MODEL"}
        )
    )
    _save(
        output / "task-variant.json",
        {
            "task_id": spec["task_id"],
            "source_path": spec["file_path"],
            "source_sha256": hashlib.sha256(Path(spec["file_path"]).read_bytes()).hexdigest(),
            "approved_variant": "local-Qwen OCR model"
            if "ocrbench" in spec["task_id"].lower()
            else "local execution and judge services",
            "substitutions": substitutions,
            "execution_preamble": None,
            "judge_model": model,
            "audio_judge_route": "local adapter via gateway",
            "shared_skill_bank": "complete supplied snapshot",
            "search": "free browser; Brave disabled",
        },
    )
    (output / "official-task.md").write_bytes(Path(spec["file_path"]).read_bytes())
    (output / "execution-prompt.txt").write_text(patched["prompt"])
    (output / "automated-checks.py").write_text(patched["automated_checks"])
    manifest_path = output / "task-variant.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["modified_prompt_sha256"] = hashlib.sha256(patched["prompt"].encode()).hexdigest()
    manifest["modified_checks_sha256"] = hashlib.sha256(patched["automated_checks"].encode()).hexdigest()
    if "ocrbench" in spec["task_id"].lower():
        manifest["ocr_protocol"] = _OCR_PROTOCOL
        manifest["ocr_rules_source"] = (
            "https://github.com/open-compass/VLMEvalKit/blob/main/vlmeval/dataset/image_vqa.py#L485"
        )
        manifest["ocr_task_completion_is_not_model_accuracy"] = True
    _save(manifest_path, manifest)
    return patched


_OCR_PROTOCOL = """【已批准的本地 Qwen 变体；覆盖原模型和原 810–835 评分区间】
目标模型为 qwen3.8-27b。必须实际调用本地 API 完整评测 OCRBench v1 的全部 1000 个样本，不能猜分数。
克隆并配置真实 VLMEvalKit，使用 /root/miniconda3/envs/eval 环境；环境可用计0.2分。
完整、真实且可审计的1000样本评测和正确汇总计0.8分，不要求模型达到特定准确率。保持原1200秒任务限时。
保存 /tmp_workspace/results/predictions.jsonl：每行含 index（TSV原index）、prediction（实际模型响应文本）、response_id。
保存 /tmp_workspace/results/api_calls.jsonl：每行含 index、request（实际API payload）、response（完整OpenAI JSON响应）。
每个样本使用非流式chat/completions，payload必须包含该样本原question和TSV原始base64图像，model为qwen3.8-27b。
勿替换图像、勿用groundtruth生成prediction；每个response_id必须对应本次执行gateway实际成功响应。
允许并发提高效率，但1000样本必须各自保留真实调用与prediction；服务失败或限时未完成应如实报告。
按OCRBench官方规则重算：普通类别把answer和prediction转小写、strip、换行替换为空格，用answer substring匹配；
Handwritten Mathematical Expression Recognition类别strip、换行替换为空格并删除全部空格，再substring匹配。
每个样本任一候选answer匹配则得1，否则0。六类Text Recognition合计300；其他VQA200/文档200/KIE200/数学100，总分1000。
按原结果JSON字段规范保存results/result.json，但model必须是qwen3.8-27b；
Final Score及分类计数必须与全部逐样本重算完全一致。
评分会校验本次gateway的真实调用记录，不根据GPT5-mini的810–835范围评分。
"""

_OCR_ENV_CHECKS = r"""
def grade(**kwargs):
    import os, subprocess
    from pathlib import Path
    usable=False
    for directory, dirs, files in os.walk('/tmp_workspace'):
        p=Path(directory)
        if 'run.py' in files and (p/'vlmeval/dataset/image_vqa.py').is_file() and (p/'.git').is_dir():
            remote=subprocess.run(['git','-C',str(p),'remote','get-url','origin'],capture_output=True,text=True)
            if 'open-compass/VLMEvalKit' not in remote.stdout: continue
            try:
                check=subprocess.run(['/root/miniconda3/envs/eval/bin/python','-c',
                    'import sys;sys.path.insert(0,'+repr(str(p))+');import vlmeval'],
                    capture_output=True,text=True,timeout=45)
                usable=check.returncode==0
            except Exception: usable=False
            if usable: break
    return {'overall_score':0.2 if usable else 0.0,'environment_score':0.2 if usable else 0.0}
"""


def _ocr_grade(output: Path, config: Mapping[str, Any], run_id: str, environment_score: float) -> dict[str, Any]:
    """Recompute OCRBench and authenticate all predictions against gateway logs."""
    workspace = output / "workspace"
    result = {
        "overall_score": environment_score,
        "environment_score": environment_score,
        "evaluation_completion_score": 0.0,
        "verified_samples": 0,
        "protocol": "local-Qwen OCRBench task completion, separate from OCR accuracy",
    }
    try:
        csv.field_size_limit(2**31 - 1)
        with (workspace / "OCRBench.tsv").open(newline="") as stream:
            rows = list(csv.DictReader(stream, delimiter="\t"))
        if len(rows) != 1000:
            raise ValueError("Pinned OCRBench v1 input does not have 1000 samples")
        results_dir = workspace / "results"
        predictions = [
            json.loads(line) for line in (results_dir / "predictions.jsonl").read_text().splitlines() if line.strip()
        ]
        calls = [
            json.loads(line) for line in (results_dir / "api_calls.jsonl").read_text().splitlines() if line.strip()
        ]
        if len(predictions) != 1000 or len(calls) != 1000:
            raise ValueError(f"Incomplete evaluation: {len(predictions)} predictions, {len(calls)} calls")
        by_index = {str(p["index"]): p for p in predictions}
        calls_by_index = {str(p["index"]): p for p in calls}
        if len(by_index) != 1000 or len(calls_by_index) != 1000:
            raise ValueError("Duplicate sample indices")
        evidence_root = (
            Path(config.get("gateway_log_dir", environment.paths(config)["runtime"] / "api-evidence")) / run_id
        )
        actual = {}
        for record_file in evidence_root.glob("*/record.json"):
            record = json.loads(record_file.read_text())
            if record.get("role") != "main" or record.get("status") != "completed" or record.get("http_status") != 200:
                continue
            folder = record_file.parent
            if not (folder / "response.bin").exists():
                continue
            try:
                response = json.loads((folder / "response.bin").read_bytes())
                request = json.loads((folder / "request.json").read_text())
                actual[response["id"]] = (request, response)
            except (ValueError, KeyError):
                continue
        counts: dict[str, int] = {}
        response_ids: set[str] = set()
        model = str(config.get("model_id", "qwen3.8-27b"))
        for row in rows:
            index = str(row["index"])
            prediction = by_index[index]
            call = calls_by_index[index]
            response_id = str(prediction["response_id"])
            if response_id in response_ids or response_id not in actual:
                raise ValueError(f"Sample {index}: missing/duplicate actual gateway response")
            response_ids.add(response_id)
            request, response = actual[response_id]
            if request.get("model") != model:
                raise ValueError(f"Sample {index}: wrong actual model")
            text = response["choices"][0]["message"].get("content", "")
            if not isinstance(text, str) or prediction["prediction"] != text:
                raise ValueError(f"Sample {index}: prediction differs from actual model response")
            if (
                call["response"].get("id") != response_id
                or call["response"]["choices"][0]["message"].get("content") != text
            ):
                raise ValueError(f"Sample {index}: recorded response is inconsistent")
            messages = request.get("messages", [])
            text_parts = []
            images = []
            for message in messages:
                content = message.get("content", [])
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                        if part.get("type") == "image_url":
                            image = part.get("image_url", {}).get("url", "")
                            if image.startswith("data:") and "," in image:
                                images.append(image.split(",", 1)[1])
            if row["question"] not in "\n".join(text_parts) or row["image"] not in images:
                raise ValueError(f"Sample {index}: actual payload lacks the correct question/image")
            answers = ast.literal_eval(row["answer"])
            category = row["category"]
            if category == "Handwritten Mathematical Expression Recognition":
                normalize = lambda x: str(x).strip().replace("\n", " ").replace(" ", "")
            else:
                normalize = lambda x: str(x).lower().strip().replace("\n", " ")
            hit = any(normalize(answer) in normalize(text) for answer in answers)
            counts[category] = counts.get(category, 0) + int(hit)
            result["verified_samples"] += 1
        report = json.loads((results_dir / "result.json").read_text())
        computed = {
            "Text Recognition": sum(
                counts.get(c, 0)
                for c in [
                    "Regular Text Recognition",
                    "Irregular Text Recognition",
                    "Artistic Text Recognition",
                    "Handwriting Recognition",
                    "Digit String Recognition",
                    "Non-Semantic Text Recognition",
                ]
            ),
            "Scene Text-centric VQA": counts.get("Scene Text-centric VQA", 0),
            "Document-oriented VQA": counts.get("Doc-oriented VQA", 0),
            "Key Information Extraction": counts.get("Key Information Extraction", 0),
            "Handwritten Mathematical Expression Recognition": counts.get(
                "Handwritten Mathematical Expression Recognition", 0
            ),
        }
        computed["Final Score"] = sum(computed.values())
        if report.get("model") != model or report.get("benchmark") != "OCRBench":
            raise ValueError("Result metadata has wrong model/benchmark")
        for key, value in computed.items():
            alias = "Doc-oriented VQA" if key == "Document-oriented VQA" else key
            if report.get(key, report.get(alias)) != value:
                raise ValueError(f"Reported {key} differs from official-rule recomputation")
        result.update(
            overall_score=round(environment_score + 0.8, 6),
            evaluation_completion_score=0.8,
            ocr_model_results=computed,
            ocr_accuracy=computed["Final Score"] / 1000,
        )
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        # An incomplete or inaccurate artifact is a task failure, not a missing
        # scoring service. It legitimately earns only its environment points.
        result["completion_failure"] = str(exc)
    return result


# Injected into the grader process, not into the Agent. It records API failures
# even when an official check catches the exception and returns a default zero.
_GRADE_MONITOR = r"""
import hashlib as _hashlib, json as _json, pathlib as _pathlib, urllib.parse as _urlparse
_api_log = _pathlib.Path('/tmp/sharedskill-judge-api.jsonl')
_mock_log = _pathlib.Path('/tmp/sharedskill-mock-replay.json')
_replay = _json.loads(_mock_log.read_text()) if _mock_log.exists() else {}
def _audit(kind, url, status=None, error=None, body=b'', **details):
    item={'kind':kind,'url':url,'status':status,'error':error,
          'fingerprint':_hashlib.sha256(str(url).encode()+body).hexdigest()}
    item.update(details)
    with _api_log.open('a') as f: f.write(_json.dumps(item)+'\n')
def _is_local_model_api(url):
    base=_urlparse.urlsplit(globals().get('_sharedskill_model_api_base_url',''))
    target=_urlparse.urlsplit(str(url))
    prefix=base.path.rstrip('/')
    return bool(base.scheme and base.netloc and prefix and
                target.scheme.lower()==base.scheme.lower() and
                target.netloc.lower()==base.netloc.lower() and
                (target.path==prefix or target.path.startswith(prefix+'/')))
def _timeout_evidence(value):
    if isinstance(value,dict):return {str(k):_timeout_evidence(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [_timeout_evidence(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    return repr(value)
def _audit_timeout(client,url,before,after):
    # A separate fingerprint keeps these observations from resolving API failures.
    fingerprint='timeout_override:'+client+':'+_hashlib.sha256(str(url).encode()).hexdigest()
    _audit('timeout_override',url,client=client,original_timeout=_timeout_evidence(before),
           effective_timeout=_timeout_evidence(after),fingerprint=fingerprint)
def _completion_issue(payload,require_choices=False):
    if not isinstance(payload,dict) or not isinstance(payload.get('choices'),list) or not payload['choices']:
        return 'Scoring completion API returned missing choices' if require_choices else None
    choice=payload['choices'][0]
    if not isinstance(choice,dict):return 'Scoring completion API returned an invalid choice'
    if choice.get('finish_reason')=='length':return 'Scoring model exhausted its output budget'
    message=choice.get('message',{})
    content=message.get('content') if isinstance(message,dict) else None
    if not str(content or choice.get('text') or '').strip():return 'Scoring model returned empty assistant content'
    return None
try:
    import httpx as _httpx
    _original_httpx_send = _httpx.Client.send
    def _httpx_send(self, request, *args, **kwargs):
        url=str(request.url);body=request.content
        if any(x in url for x in ['openrouter.ai','api.openai.com']):
            _audit('model_api',url,error='Forbidden remote model endpoint',body=body)
            raise RuntimeError('Forbidden remote model endpoint')
        if _is_local_model_api(url):
            before=request.extensions.get('timeout','<library-default>')
            after={key:_sharedskill_grading_timeout_seconds for key in ('connect','read','write','pool')}
            _audit_timeout('httpx',url,before,after)
            request.extensions['timeout']=after
        try:
            response=_original_httpx_send(self,request,*args,**kwargs)
            if '/v1/' in url:
                issue=None
                if response.status_code==200:
                    required=url.endswith('/chat/completions') or url.endswith('/completions')
                    try:issue=_completion_issue(response.json(),required)
                    except Exception:
                        if required:issue='Scoring completion API returned invalid JSON'
                _audit('model_api',url,status=response.status_code,error=issue,body=body)
            return response
        except Exception as exc:
            if '/v1/' in url:_audit('model_api',url,error=str(exc),body=body)
            raise
    _httpx.Client.send=_httpx_send
except ImportError: pass
try:
    import requests as _requests
    _original_requests_send=_requests.Session.send
    def _requests_send(self, request, *args, **kwargs):
        url=request.url;host=_urlparse.urlsplit(url).hostname
        body=request.body or b''
        if isinstance(body,str):body=body.encode()
        is_model=_is_local_model_api(url)
        is_mock=host in ['localhost','127.0.0.1'] and request.method=='GET' and not is_model
        if is_mock and url in _replay:
            response=_requests.Response();response.status_code=_replay[url]['status']
            response._content=_replay[url]['body'].encode();response.url=url
            return response
        if is_mock and globals().get('_sharedskill_replay_only',False):
            _audit('mock_api',url,error='Original mock state was not captured; cannot reconstruct it')
            raise RuntimeError('Original mock state was not captured; cannot reconstruct it')
        if any(x in url for x in ['openrouter.ai','api.openai.com']):
            _audit('model_api',url,error='Forbidden remote model endpoint',body=body)
            raise RuntimeError('Forbidden remote model endpoint')
        if is_model:
            before=kwargs.get('timeout','<library-default>')
            kwargs['timeout']=_sharedskill_grading_timeout_seconds
            _audit_timeout('requests',url,before,kwargs['timeout'])
        try:
            response=_original_requests_send(self,request,*args,**kwargs)
            if is_mock:
                _replay[url]={'status':response.status_code,'body':response.text}
                _mock_log.write_text(_json.dumps(_replay))
            elif '/v1/' in url:
                issue=None
                if response.status_code==200:
                    required=url.endswith('/chat/completions') or url.endswith('/completions')
                    try:issue=_completion_issue(response.json(),required)
                    except Exception:
                        if required:issue='Scoring completion API returned invalid JSON'
                _audit('model_api',url,status=response.status_code,error=issue,body=body)
            return response
        except Exception as exc:
            kind='mock_api' if is_mock else ('model_api' if '/v1/' in url else 'resource')
            _audit(kind,url,error=str(exc),body=body);raise
    _requests.Session.send=_requests_send
except ImportError: pass
try:
    import urllib.request as _urllib_request, urllib.error as _urllib_error, io as _io
    _original_urlopen=_urllib_request.urlopen
    class _CachedResponse(_io.BytesIO):
        def __init__(self, body, status=200, url=''):
            super().__init__(body);self.status=status;self.url=url;self.headers={'Content-Type':'application/json'}
        def getcode(self):return self.status
        def info(self):return self.headers
    def _urlopen(request, *args, **kwargs):
        url=request.full_url if hasattr(request,'full_url') else str(request)
        host=_urlparse.urlsplit(url).hostname
        method=request.get_method() if hasattr(request,'get_method') else 'GET'
        is_model=_is_local_model_api(url)
        is_mock=host in ['localhost','127.0.0.1'] and method=='GET' and not is_model
        if is_mock and url in _replay:
            item=_replay[url];return _CachedResponse(item['body'].encode(),item['status'],url)
        if is_mock and globals().get('_sharedskill_replay_only',False):
            _audit('mock_api',url,error='Original mock state was not captured; cannot reconstruct it')
            raise RuntimeError('Original mock state was not captured; cannot reconstruct it')
        if any(x in url for x in ['openrouter.ai','api.openai.com']):
            _audit('model_api',url,error='Forbidden remote model endpoint')
            raise RuntimeError('Forbidden remote model endpoint')
        if is_model:
            before=args[1] if len(args)>1 else kwargs.get('timeout','<library-default>')
            if len(args)>1:
                args=list(args);args[1]=_sharedskill_grading_timeout_seconds;args=tuple(args)
                kwargs.pop('timeout',None)
            else:kwargs['timeout']=_sharedskill_grading_timeout_seconds
            _audit_timeout('urllib',url,before,_sharedskill_grading_timeout_seconds)
        try:
            response=_original_urlopen(request,*args,**kwargs)
            if is_mock:
                body=response.read();status=response.status;response.close()
                _replay[url]={'status':status,'body':body.decode(errors='replace')}
                _mock_log.write_text(_json.dumps(_replay))
                return _CachedResponse(body,status,url)
            if '/v1/' in url:_audit('model_api',url,status=response.status)
            return response
        except Exception as exc:
            if is_mock and not isinstance(exc,_urllib_error.HTTPError):_audit('mock_api',url,error=str(exc))
            elif '/v1/' in url:_audit('model_api',url,error=str(exc))
            raise
    _urllib_request.urlopen=_urlopen
except ImportError: pass
"""


def _grade(
    name: str, spec: Mapping[str, Any], output: Path, config: Mapping[str, Any], *, replay_only: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = environment.paths(config)["upstream"]
    api_url = environment.container_api_url(
        config, run_id="wcb-" + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:20]
    )
    grading_timeout = int(config.get("grading_timeout_seconds", config.get("grade_timeout_seconds", 1800)))
    _copy_in(source / "src" / "utils" / "transcript_loader.py", name, "/tmp/_transcript_loader.py", config)
    runner = output / "grade-runner.py"
    runner.write_text(
        "_sharedskill_replay_only="
        + repr(replay_only)
        + "\n"
        + "_sharedskill_model_api_base_url="
        + repr(api_url)
        + "\n_sharedskill_grading_timeout_seconds="
        + repr(grading_timeout)
        + "\n"
        + _GRADE_MONITOR
        + "\nfrom _transcript_loader import load_transcript\n"
        "_transcript=load_transcript('/root/.openclaw/agents/main/sessions/chat.jsonl')\n"
        + str(spec["automated_checks"])
        + "\n"
        "print(__import__('json').dumps(grade(transcript=_transcript,workspace_path='/tmp_workspace')))\n"
    )
    _copy_in(runner, name, "/tmp/_grade_runner.py", config)
    _docker(["exec", name, "rm", "-f", "/tmp/sharedskill-judge-api.jsonl"], config)
    replay = output / "mock-replay.json"
    if replay.exists():
        _copy_in(replay, name, "/tmp/sharedskill-mock-replay.json", config)
    try:
        result = _docker(
            [
                "exec",
                "--env",
                "OPENROUTER_BASE_URL=" + api_url,
                "--env",
                "JUDGE_MODEL=" + str(config.get("model_id", "qwen3.8-27b")),
                name,
                "python3",
                "/tmp/_grade_runner.py",
            ],
            config,
            timeout=grading_timeout,
            check=False,
        )
        (output / "grade.stdout").write_text(result.stdout)
        (output / "grade.stderr").write_text(result.stderr)
        if result.returncode:
            scores: dict[str, Any] = {
                "error": "Official Automated Checks process failed: " + result.stderr[-3000:],
                "_grading_infrastructure_error": True,
            }
        else:
            scores = {}
            for line in reversed(result.stdout.splitlines()):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    scores = value
                    break
            if not scores:
                scores = {
                    "error": "Official Automated Checks returned no JSON scores",
                    "_grading_infrastructure_error": True,
                }
    except subprocess.TimeoutExpired:
        scores = {"error": "Official Automated Checks timed out", "_grading_infrastructure_error": True}
    finally:
        _copy_out(name, "/tmp/sharedskill-mock-replay.json", replay, config, required=False)
        _copy_out(name, "/tmp/sharedskill-judge-api.jsonl", output / "judge-api.jsonl", config, required=False)
    events = []
    log = output / "judge-api.jsonl"
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return scores, events


def _judge_diagnostics(scores: Mapping[str, Any], events: list[dict[str, Any]]) -> list[str]:
    """Only model-judge parsing failures; task artifacts still earn real failures."""
    if not any(e.get("kind") == "model_api" for e in events):
        return []
    issues = []
    for key, value in scores.items():
        if not isinstance(value, str) or not ("judge" in key.lower() or "llm" in key.lower()):
            continue
        text = value.lower()
        if ("method" in key.lower() and text in ("regex_fallback", "rule_fallback", "keyword_fallback")) or any(
            marker in text
            for marker in (
                "did not contain a json",
                "judge_failed",
                "empty response",
                "no json object",
                "failed to parse judge",
            )
        ):
            issues.append(key + ": " + value)
    return issues


def _session(output: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    reads: set[str] = set()
    calls: dict[str, dict[str, Any]] = {}
    tool_errors = 0
    skill_reads: dict[tuple[str, str], dict[str, str]] = {}
    membership = output / "shared-skill-names.json"
    shared_names = set(json.loads(membership.read_text())) if membership.exists() else set()
    chat = output / "chat.jsonl"
    if chat.exists():
        for line in chat.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entries.append(entry)
            message = entry.get("message", {})
            content = message.get("content", [])
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("toolCall", "tool_use", "tool_call"):
                    args = part.get("arguments", part.get("input", {}))
                    calls[str(part.get("id", ""))] = {"name": part.get("name"), "arguments": args}
            if message.get("role") in ("toolResult", "tool") and message.get("isError"):
                tool_errors += 1
            elif message.get("role") in ("toolResult", "tool"):
                call = calls.get(str(message.get("toolCallId", message.get("tool_call_id", ""))), {})
                raw = json.dumps(call.get("arguments", {}), ensure_ascii=False)
                if call.get("name") in ("read", "read_file", "readFile") or (
                    call.get("name") in ("exec", "bash") and re.search(r"\b(cat|sed|head|less)\b", raw)
                ):
                    for original_path in re.findall(r"((?:~/|/)[^\s\"']+/SKILL\.md)", raw):
                        expanded = "/root/" + original_path[2:] if original_path.startswith("~/") else original_path
                        path = posixpath.normpath(expanded)
                        name = Path(path).parent.name
                        shared = path == f"/root/skills/{name}/SKILL.md" and name in shared_names
                        skill_reads[(path, original_path)] = {
                            "path": path,
                            "original_path": original_path,
                            "name": name,
                            "environment_or_shared": "shared" if shared else "environment",
                        }
                        if shared:
                            reads.add(name)
    return {
        "task_id": spec["task_id"],
        "prompt_text": spec["prompt"],
        "messages": entries,
        "read_skills": sorted(reads),
        "actual_used_skill_refs": sorted(reads),
        "actual_skill_reads": [skill_reads[path] for path in sorted(skill_reads)],
        "raw_transcript_path": str(chat),
        "tool_errors": tool_errors,
    }


def _gateway_main_records(config: Mapping[str, Any], run_id: str, started_unix: float = 0) -> list[dict[str, Any]]:
    root = Path(config.get("gateway_log_dir", environment.paths(config)["runtime"] / "api-evidence")) / run_id
    records = []
    for file in root.glob("*/record.json"):
        try:
            record = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("started_unix", 0) < started_unix:
            continue
        if record.get("role") == "main":
            records.append(record)
    return records


def _gateway_main_failures(
    config: Mapping[str, Any],
    run_id: str,
    started_unix: float = 0,
    *,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if records is None:
        records = _gateway_main_records(config, run_id, started_unix)
    return [r for r in records if r.get("status") == "error" or (r.get("http_status") or 200) >= 400]


def _task_deadline_evidence(
    stdout: str,
    stderr: str,
    timeout: int,
    started_unix: float,
    finished_unix: float,
    *,
    outer_timeout: bool = False,
) -> dict[str, Any]:
    """Only the native task timer or the outer task wait proves a deadline."""
    clean_stderr = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stderr)
    markers = []
    for line in clean_stderr.splitlines():
        match = re.search(r"embedded run timeout: runId=(\S+) sessionId=chat timeoutMs=(\d+)(?:\s|$)", line)
        if match and int(match.group(2)) == timeout * 1000:
            markers.append({"run_id": match.group(1), "session_id": "chat", "timeout_ms": timeout * 1000, "line": line})
    try:
        payload = json.loads(stdout)
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
    except (ValueError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "confirmed": outer_timeout or bool(markers),
        "timeout_seconds": timeout,
        "agent_started_unix": started_unix,
        "agent_finished_unix": finished_unix,
        "deadline_not_before_unix": started_unix + timeout,
        "outer_timeout": outer_timeout,
        "native_deadline_markers": markers,
        # Aborted metadata also occurs on manual cancellation; retain it only as evidence.
        "native_meta": {key: meta[key] for key in ("aborted", "durationMs", "stopReason") if key in meta},
    }


def _deadline_stream_cancellations(
    failures: list[dict[str, Any]], records: list[dict[str, Any]], evidence: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not evidence["confirmed"]:
        return failures, []
    last_started = max((r.get("started_unix", 0) for r in records), default=0)
    cancelled = [
        r
        for r in failures
        if r.get("http_status") == 200
        and r.get("error") == "Streaming client disconnected before completion"
        and r.get("started_unix", 0) == last_started
        and evidence["agent_started_unix"] <= r.get("started_unix", 0) <= evidence["agent_finished_unix"]
        and evidence["deadline_not_before_unix"] <= (r.get("finished_unix") or 0) <= evidence["agent_finished_unix"] + 1
    ]
    return [r for r in failures if r not in cancelled], cancelled


def _execute_child(
    task_id: str, skill_dir: Path, output: Path, config: Mapping[str, Any], rescore: bool
) -> dict[str, Any]:
    begin = time.monotonic()
    begin_unix = time.time()
    source = environment.paths(config)["upstream"]
    spec = task_spec(task_id, config)
    run_id = "wcb-" + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:20]
    api_url = environment.container_api_url(config, run_id=run_id)
    spec = _variants(spec, config, output, api_url)
    name = "sharedskill-task-" + uuid.uuid4().hex[:16]
    timeout = int(spec["timeout_seconds"])
    result: dict[str, Any] = {
        "task_id": spec["task_id"],
        "status": "infrastructure_error",
        "score": None,
        "overall_score": None,
        "scores": {},
        "timed_out": False,
        "tool_errors": 0,
        "wall_seconds": 0,
        "run_id": run_id,
        "artifacts": {"output_dir": str(output), "transcript": str(output / "chat.jsonl")},
    }
    execution_file = output / "execution.json"
    live_rescore = False
    keep_container = False
    try:
        if rescore:
            saved_execution = json.loads(execution_file.read_text())
            if saved_execution.get("task_id") != spec["task_id"]:
                raise RuntimeError("Completed execution belongs to a different task")
            previous = saved_execution.get("container", "")
            if previous.startswith("sharedskill-task-"):
                probe = _docker(["inspect", previous], config, check=False)
                if probe.returncode == 0:
                    old = json.loads(probe.stdout)[0]
                    if (
                        old.get("State", {}).get("Running")
                        and old.get("Config", {}).get("Labels", {}).get("org.sharedskill.run_id") == run_id
                    ):
                        name, live_rescore = previous, True
                        keep_container = True
        original_workspace = Path(spec["workspace_path"]) / "exec"
        workspace = output / "workspace" if rescore else original_workspace
        # These two pinned upstream tasks obtain all inputs from the public web;
        # the official HF asset tree deliberately contains no exec directory.
        # Preserve that absence in the source tree and mount a private empty input.
        web_only = spec["task_id"] in {
            "01_Productivity_Flow_task_1_arxiv_digest",
            "01_Productivity_Flow_task_9_scp_crawl",
        }
        if not original_workspace.exists() and web_only:
            original_workspace = output / "input-empty"
            original_workspace.mkdir(exist_ok=True)
            if not rescore:
                workspace = original_workspace
        if not workspace.is_dir():
            raise RuntimeError("Missing task input/workspace snapshot: " + str(workspace))
        if not original_workspace.is_dir():
            raise RuntimeError("Missing immutable task input: " + str(original_workspace))
        args = [
            "run",
            "--detach",
            "--name",
            name,
            *environment.provider_network_args(),
            *environment.python_seed_args(config),
            "--label",
            "org.sharedskill.run_id=" + run_id,
            "--mount",
            f"type=bind,src={original_workspace},dst=/app,readonly",
            "--env",
            "OPENROUTER_API_KEY=" + str(config.get("local_api_key", "sharedskill-local")),
            "--env",
            "OPENROUTER_BASE_URL=" + api_url,
            "--env",
            "OPENAI_API_KEY=" + str(config.get("local_api_key", "sharedskill-local")),
            "--env",
            "OPENAI_BASE_URL=" + api_url,
            "--env",
            "ANTHROPIC_API_KEY=sharedskill-local",
            "--env",
            "GEMINI_API_KEY=sharedskill-local",
            "--env",
            "GOOGLE_API_KEY=sharedskill-local",
            "--env",
            "ELEVENLABS_API_KEY=",
            "--env",
            "XI_API_KEY=",
            "--env",
            "JUDGE_MODEL=" + str(config.get("model_id", "qwen3.8-27b")),
            "--env",
            "BRAVE_API_KEY=",
            "--env",
            "OMP_NUM_THREADS=4",
            "--env",
            "http_proxy=",
            "--env",
            "https_proxy=",
            "--env",
            "all_proxy=",
            "--env",
            "HTTP_PROXY=",
            "--env",
            "HTTPS_PROXY=",
            "--env",
            "ALL_PROXY=",
            "--env",
            "NO_PROXY=*",
            "--env",
            "no_proxy=*",
            environment.TASK_IMAGE,
            "tail",
            "-f",
            "/dev/null",
        ]
        if rescore and not live_rescore:
            args[1:1] = ["--mount", f"type=bind,src={workspace},dst=/previous_output,readonly"]
        if not live_rescore:
            _docker(args, config, timeout=120)
            restore_source = "/previous_output" if rescore else "/app"
            _docker(
                [
                    "exec",
                    name,
                    "bash",
                    "-c",
                    "mkdir -p /tmp_workspace && cp -a "
                    + restore_source
                    + "/. /tmp_workspace/ && chmod -R u+w /tmp_workspace && mkdir -p /root/skills",
                ],
                config,
                timeout=180,
            )
        initial_tmp = Path(spec["workspace_path"]) / "tmp"
        if not rescore and initial_tmp.is_dir():
            _docker(["exec", name, "mkdir", "-p", "/tmp_workspace/tmp"], config)
            _docker(["cp", str(initial_tmp) + "/.", f"{name}:/tmp_workspace/tmp/"], config, timeout=180)
        if rescore:
            _docker(["exec", name, "mkdir", "-p", "/root/.openclaw/agents/main/sessions"], config)
            _copy_in(output / "chat.jsonl", name, "/root/.openclaw/agents/main/sessions/chat.jsonl", config)
            execution = json.loads(execution_file.read_text())
            result["timed_out"] = execution.get("timed_out", False)
            if "task_deadline_evidence" in execution:
                result["task_deadline_evidence"] = execution["task_deadline_evidence"]
                result["artifacts"]["deadline_evidence"] = str(output / "agent-deadline.json")
            result["agent_wall_seconds"] = execution.get("agent_wall_seconds", 0)
            result["rescore"] = True
        else:
            if skill_dir.is_dir():
                _save(output / "shared-skill-names.json", sorted(p.parent.name for p in skill_dir.glob("*/SKILL.md")))
                _docker(["cp", str(skill_dir) + "/.", f"{name}:/root/skills/"], config, timeout=180)
            else:
                raise RuntimeError("Missing shared skill snapshot: " + str(skill_dir))
        # Persist the local-only configuration; the pinned image supplies OpenClaw.
        conf = environment.openclaw_config(config, "/tmp_workspace", api_url=api_url)
        conf_path = output / "openclaw-config.json"
        _save(conf_path, conf)
        _copy_in(conf_path, name, "/root/.openclaw/openclaw.json", config)
        # The image tool restricts media roots to the configured workspace.
        _docker(
            [
                "exec",
                name,
                "bash",
                "-c",
                "rm -rf /root/.openclaw/workspace && ln -s /tmp_workspace /root/.openclaw/workspace",
            ],
            config,
        )
        tools_file = output / "local-tools.md"
        tools_file.write_text(_local_tools(api_url, str(config.get("model_id", "qwen3.8-27b"))))
        _copy_in(tools_file, name, "/tmp_workspace/TOOLS.md", config)
        if not live_rescore:
            environment.apply_python_bootstrap(name, config, output / "python-bootstrap.json")
            environment.apply_local_tool_adapters(name, config, api_url, output / "local-tool-adapters.json")
        # Warmup services are backgrounded with detached stdin/stdout so the
        # parent docker-exec does not hang while mock servers stay alive.
        warmup = str(spec.get("warmup", ""))
        if not rescore:
            warmup_lines = []
            for index, line in enumerate(warmup.splitlines()):
                line = line.strip()
                if line.endswith("&"):
                    line = (
                        "nohup bash -c "
                        + shlex.quote(line[:-1].strip())
                        + f" >/tmp/sharedskill-warmup-{index}.log 2>&1 </dev/null &"
                    )
                warmup_lines.append(line)
            if warmup.strip():
                script = output / "warmup.sh"
                script.write_text("set -e\n" + "\n".join(warmup_lines) + "\n")
                _copy_in(script, name, "/tmp/sharedskill-warmup.sh", config)
                proc = _docker(
                    ["exec", "--workdir", "/tmp_workspace", name, "bash", "/tmp/sharedskill-warmup.sh"],
                    config,
                    timeout=int(config.get("warmup_timeout_seconds", 600)),
                    check=False,
                )
                with (output / "warmup.log").open("a") as stream:
                    stream.write(f"{proc.stdout}\n{proc.stderr}\n")
                if proc.returncode:
                    raise RuntimeError(f"Task warmup failed: {proc.stderr[-2000:]}")
            # Native local mode gives the same tool-capable OpenClaw agent,
            # without a gateway readiness race in every clean task container.
            agent_args = [
                "exec",
                "--workdir",
                "/tmp_workspace",
                name,
                "openclaw",
                "agent",
                "--agent",
                "main",
                "--session-id",
                "chat",
                "--local",
                "--json",
                "--timeout",
                str(timeout),
                "--message",
                spec["prompt"],
            ]
            start = time.monotonic()
            agent_started_unix = time.time()
            outer_timeout = False
            # A failed artifact copy must not reuse a transcript from an earlier
            # failed attempt in this output directory.
            (output / "chat.jsonl").unlink(missing_ok=True)
            try:
                proc = _docker(agent_args, config, timeout=timeout + 30, check=False)
                (output / "agent.stdout").write_text(proc.stdout)
                (output / "agent.stderr").write_text(proc.stderr)
                if proc.returncode:
                    result["agent_error"] = proc.stderr[-3000:]
            except subprocess.TimeoutExpired as exc:
                outer_timeout = True
                (output / "agent.stdout").write_text(
                    (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
                )
                stderr = (
                    (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
                )
                (output / "agent.stderr").write_text(stderr + "\nAgent timeout")
                _docker(["exec", name, "pkill", "-f", "openclaw"], config, check=False)
            deadline = _task_deadline_evidence(
                (output / "agent.stdout").read_text(),
                (output / "agent.stderr").read_text(),
                timeout,
                agent_started_unix,
                time.time(),
                outer_timeout=outer_timeout,
            )
            result["timed_out"] = deadline["confirmed"]
            result["task_deadline_evidence"] = deadline
            result["artifacts"]["deadline_evidence"] = str(output / "agent-deadline.json")
            _save(output / "agent-deadline.json", deadline)
            _copy_out(
                name, "/root/.openclaw/agents/main/sessions/chat.jsonl", output / "chat.jsonl", config, required=False
            )
            result["agent_wall_seconds"] = time.monotonic() - start
            session = _session(output, spec)
            assistant_activity = any(
                entry.get("message", {}).get("role") == "assistant"
                and any(
                    isinstance(part, dict)
                    and (
                        part.get("type") in ("toolCall", "tool_use", "tool_call")
                        or (part.get("type") == "text" and str(part.get("text", "")).strip())
                    )
                    for part in entry.get("message", {}).get("content", [])
                )
                for entry in session["messages"]
            )
            main_records = _gateway_main_records(config, run_id, begin_unix)
            main_errors = _gateway_main_failures(config, run_id, begin_unix, records=main_records)
            main_errors, cancelled = _deadline_stream_cancellations(main_errors, main_records, deadline)
            deadline["cancelled_gateway_requests"] = cancelled
            _save(output / "agent-deadline.json", deadline)
            transcript_model_errors = [
                entry["message"].get("errorMessage", "model returned an error")
                for entry in session["messages"]
                if entry.get("message", {}).get("role") == "assistant"
                and entry.get("message", {}).get("stopReason") == "error"
            ]
            unconfirmed_abort = deadline["native_meta"].get("aborted") is True and not deadline["confirmed"]
            if (
                (not assistant_activity and not result["timed_out"])
                or main_errors
                or transcript_model_errors
                or unconfirmed_abort
            ):
                result["gateway_main_errors"] = main_errors
                result["transcript_model_errors"] = transcript_model_errors
                result["unconfirmed_agent_abort"] = unconfirmed_abort
                raise RuntimeError(
                    "Agent infrastructure failure: "
                    + (
                        "native agent aborted without a confirmed task deadline"
                        if unconfirmed_abort
                        else "main model API failed"
                        if main_errors or transcript_model_errors
                        else "no actual assistant execution in transcript"
                    )
                )
            # Save task state before judging, including files modified in place.
            if (output / "workspace").exists():
                shutil.rmtree(output / "workspace")
            (output / "workspace").mkdir(exist_ok=True)
            _copy_out(name, "/tmp_workspace/.", output / "workspace", config)
            _save(
                execution_file,
                {
                    "status": "agent_completed",
                    "timed_out": result["timed_out"],
                    "task_deadline_evidence": deadline,
                    "agent_error": result.get("agent_error"),
                    "task_id": spec["task_id"],
                    "container": name,
                    "agent_wall_seconds": result["agent_wall_seconds"],
                },
            )
        gt = Path(spec["workspace_path"]) / "gt"
        if gt.is_dir() and not live_rescore:
            _copy_in(gt, name, "/tmp_workspace/gt", config)
        scoring_start = time.monotonic()
        scores, events = _grade(name, spec, output, config, replay_only=rescore and not live_rescore)
        if "ocrbench" in spec["task_id"].lower() and not scores.get("error"):
            scores = _ocr_grade(output, config, run_id, float(scores.get("environment_score", 0)))
        result["scoring_wall_seconds"] = time.monotonic() - scoring_start
        # A retry that succeeds resolves an earlier failure of the same request.
        final_events = {e["fingerprint"]: e for e in events}
        failures = [
            e
            for e in final_events.values()
            if e.get("kind") in ("model_api", "mock_api") and (e.get("error") or (e.get("status") or 200) >= 400)
        ]
        score = scores.get("overall_score")
        if score is None:
            numeric = [float(v) for v in scores.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            score = sum(numeric) / len(numeric) if numeric else None
        score_error = None
        if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)):
            score = float(score)
            if not 0 <= score <= 1:
                score_error = f"Unexpected benchmark score unit/range: {score}"
        else:
            score_error = "No finite task score"
        judge_issues = _judge_diagnostics(scores, events)
        pending = bool(failures or scores.get("_grading_infrastructure_error") or score_error or judge_issues)
        keep_container = pending
        result.update(
            status="pending_score" if pending else "completed",
            scores=scores,
            score=None if pending else score,
            overall_score=None if pending else score,
            scoring_api_failures=failures,
            scoring_parse_failures=judge_issues,
        )
        if pending:
            result["error"] = (
                score_error
                or ("; ".join(judge_issues) if judge_issues else None)
                or scores.get("error", "Local scoring API request failed")
            )
            keep_container = True
            result["artifacts"]["preserved_container"] = name
        saved_execution = json.loads(execution_file.read_text())
        saved_execution.update(container=name, scoring_status=result["status"], preserved_container=keep_container)
        _save(execution_file, saved_execution)
        result["session"] = _session(output, spec)
        result["tool_errors"] = result["session"]["tool_errors"]
        sys.path.insert(0, str(source))
        from src.utils.grading import extract_usage_from_jsonl

        result["usage"] = extract_usage_from_jsonl(output / "chat.jsonl")
        _save(output / "raw-score.json", scores)
    except Exception as exc:
        result["error"] = str(exc)
        result["session"] = _session(output, spec)
    finally:
        if not keep_container:
            _docker(["rm", "--force", name], config, timeout=60, check=False)
        result["wall_seconds"] = time.monotonic() - begin
        _save(output / "runtime-result.json", result)
    return result


def execute_task(
    task_id: str, skill_dir: str | Path, output_dir: str | Path, config: Mapping[str, Any]
) -> dict[str, Any]:
    """Thread-safe host entry point: globals/official imports live in a child."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    resume = (output / "execution.json").exists()
    if not resume and (output / "runtime-result.json").exists():
        prior = output / "attempt-history" / (str(time.time_ns()) + "-" + uuid.uuid4().hex[:8])
        prior.mkdir(parents=True)
        for file in output.iterdir():
            if file.is_file() and file.name != "runtime.log":
                shutil.copy2(file, prior / file.name)
    if resume and (output / "runtime-result.json").exists():
        prior = output / "score-attempt-history" / (str(time.time_ns()) + "-" + uuid.uuid4().hex[:8])
        prior.mkdir(parents=True)
        for name in (
            "runtime-result.json",
            "raw-score.json",
            "grade.stdout",
            "grade.stderr",
            "judge-api.jsonl",
            "grade-runner.py",
            "execution.json",
            "runtime-config.json",
        ):
            file = output / name
            if file.exists():
                shutil.copy2(file, prior / name)
    conf_file = output / "runtime-config.json"
    _save(conf_file, dict(config))
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--task",
        task_id,
        "--skill-dir",
        str(Path(skill_dir).resolve()),
        "--output",
        str(output),
        "--config",
        str(conf_file),
    ]
    if resume:
        command.append("--rescore")
    # Each task's own timeout plus warmup/grading bounds; output goes to its log.
    with (output / "runtime.log").open("a") as stream:
        proc = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False)
    result_file = output / "runtime-result.json"
    if proc.returncode == 0 and result_file.exists():
        return json.loads(result_file.read_text())
    return {
        "task_id": task_id,
        "status": "infrastructure_error",
        "score": None,
        "overall_score": None,
        "error": f"Runtime child exited {proc.returncode}; see {output / 'runtime.log'}",
        "timed_out": False,
        "tool_errors": 0,
        "wall_seconds": 0,
        "session": {},
    }


def rescore_task(task_id: str, output_dir: str | Path, config: Mapping[str, Any]) -> dict[str, Any]:
    output = Path(output_dir).resolve()
    if not (output / "execution.json").exists():
        raise RuntimeError("No completed Agent execution to rescore")
    return execute_task(task_id, Path(config.get("initial_skill_dir", output)), output, config)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--skill-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--rescore", action="store_true")
    args = parser.parse_args()
    configuration = json.loads(Path(args.config).read_text())
    outcome = _execute_child(args.task, Path(args.skill_dir), Path(args.output), configuration, args.rescore)
    print(
        json.dumps(
            {k: outcome.get(k) for k in ["task_id", "status", "score", "wall_seconds", "error"]}, ensure_ascii=False
        )
    )
