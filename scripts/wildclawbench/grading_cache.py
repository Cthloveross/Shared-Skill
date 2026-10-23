"""Standalone grader response replay, also copied into the private task container."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlsplit


def request_key(url: str, body: bytes, policy: str) -> str:
    payload = json.loads(body)
    endpoint = urlsplit(url).path.rsplit("/v1/", 1)[-1]
    if endpoint not in {"chat/completions", "completions"} or not isinstance(payload, dict):
        raise ValueError("Unsupported model replay request")
    value = {"endpoint": endpoint, "request": payload, "judge_policy_sha256": policy}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def successful_response(status: int, raw: bytes, request: bytes) -> bool:
    if status != 200:
        return False
    try:
        payload, asked = json.loads(raw), json.loads(request)
        choice = payload["choices"][0]
        content = choice.get("message", {}).get("content") or choice.get("text")
        if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
            return False
        if asked.get("response_format", {}).get("type") in {"json_object", "json_schema"}:
            return isinstance(json.loads(content), dict)
        return True
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return False


def install(namespace: dict, cache_file: str, captures_file: str) -> None:
    """Replay only successful exact grader requests under the same judge policy."""
    cache = json.loads(Path(cache_file).read_text())
    policy, entries = cache["judge_policy_sha256"], dict(cache["entries"])
    captures = {}
    limit = cache["max_new_requests"]
    if type(limit) is not int or limit <= 0:
        raise ValueError("Grading recovery requires a positive request limit")
    admitted = 0

    def persist():
        target = Path(captures_file)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"judge_policy_sha256": policy, "requests": admitted,
                                        "max_new_requests": limit, "entries": captures}))
        temporary.replace(target)

    def lookup(url, body):
        nonlocal admitted
        if not namespace["_is_local_model_api"](url):
            return None, None
        key = request_key(url, body, policy)
        item = entries.get(key)
        if item is not None:
            raw = item["body"].encode()
            if not successful_response(item["status"], raw, body):
                raise ValueError("Invalid cached judge response")
            if namespace.get("_completion_issue"):
                namespace["_completion_issue"](json.loads(raw), True, url, body)
            namespace["_audit"]("model_api", url, status=item["status"], body=body,
                                cached_response=True, model_request_key=key)
        else:
            if admitted >= limit:
                namespace["_audit"]("model_api", url, error="Independent grading request budget exhausted", body=body)
                raise RuntimeError("Independent grading request budget exhausted")
            admitted += 1
            persist()  # Failed or interrupted requests keep their reservation.
        return key, item

    def record(key, url, body, status, raw):
        if key is None:
            return
        item = {"status": status, "body": raw.decode(), "request": json.loads(body),
                "endpoint": urlsplit(url).path.rsplit("/v1/", 1)[-1],
                "successful": successful_response(status, raw, body)}
        captures[key] = item
        persist()
        if item["successful"]:
            entries[key] = item

    try:
        import httpx
        original = httpx.Client.send

        def send_httpx(self, request, *args, **kwargs):
            url, body = str(request.url), request.content
            key, item = lookup(url, body)
            if item is not None:
                return httpx.Response(item["status"], content=item["body"].encode(), request=request)
            response = original(self, request, *args, **kwargs)
            if key is not None:
                response.read()
                record(key, url, body, response.status_code, response.content)
            return response

        httpx.Client.send = send_httpx
    except ImportError:
        pass
    try:
        import requests
        original_requests = requests.Session.send

        def send_requests(self, request, *args, **kwargs):
            body = request.body or b""
            body = body.encode() if isinstance(body, str) else body
            key, item = lookup(request.url, body)
            if item is not None:
                response = requests.Response()
                response.status_code, response._content, response.url = item["status"], item["body"].encode(), request.url
                return response
            response = original_requests(self, request, *args, **kwargs)
            record(key, request.url, body, response.status_code, response.content)
            return response

        requests.Session.send = send_requests
    except ImportError:
        pass
    import urllib.request
    original_urlopen = urllib.request.urlopen

    class Response(io.BytesIO):
        def __init__(self, body, status, url, headers):
            super().__init__(body)
            self.status, self.url, self.headers = status, url, headers

        def getcode(self):
            return self.status

        def info(self):
            return self.headers

    def urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        body = getattr(request, "data", None) or b""
        key, item = lookup(url, body)
        if item is not None:
            return Response(item["body"].encode(), item["status"], url, {"Content-Type": "application/json"})
        response = original_urlopen(request, *args, **kwargs)
        if key is None:
            return response
        raw = response.read()
        record(key, url, body, response.status, raw)
        result = Response(raw, response.status, url, response.headers)
        response.close()
        return result

    urllib.request.urlopen = urlopen
