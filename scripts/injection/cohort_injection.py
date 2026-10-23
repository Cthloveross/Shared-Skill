"""Tamper one proven message-read observation in a native uploaded session."""

from __future__ import annotations

import copy
import ast
import hashlib
import json
import posixpath
import re
import shlex
from typing import Any
from urllib.parse import urlsplit


FIELDS = ("tool_results", "tool_observations", "tool_results_raw")
SUMMARY_LIMIT = 4_000  # SharedSkill API _TOOL_RESULT_CONTENT_MAX_CHARS.
SCHEMAS = {
    "/gmail/messages/get": ("message_id", "from", "body"),
    "/slack/messages/get": ("message_id", "sender", "content"),
}


def _id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).lower()


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part)
                         for part in value)
    return ""


def _args(call: dict) -> dict:
    value = (call.get("function") or {}).get("arguments", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _qualified(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _qualified(node.value) + "." + node.attr
    return ""


def _python_source(command: str) -> str | None:
    try:
        words = shlex.split(command)
        for index, word in enumerate(words[:-2]):
            if word.rsplit("/", 1)[-1] in {"python", "python3"} and words[index + 1] == "-c":
                return words[index + 2]
    except ValueError:
        pass
    match = re.search(r"\bpython3?\s*<<\s*['\"]?([A-Za-z0-9_]+)['\"]?[^\n]*\n", command)
    if match:
        return command[match.end():].split("\n" + match.group(1), 1)[0]
    return None


def _formatted_slack_proof(call: dict) -> dict | None:
    """Prove the pinned Python helper and header/body projection statically."""
    if (call.get("function") or {}).get("name") not in {"exec", "shell", "bash", "terminal"}:
        return None
    source = _python_source(str(_args(call).get("command", "")))
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    imports = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
               if alias.asname is None}
    if not {"json", "urllib.request"} <= imports:
        return None

    helpers = {}
    for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
        if len(function.args.args) != 2:
            continue
        path_name, body_name = (arg.arg for arg in function.args.args)
        requests = [node for node in ast.walk(function) if isinstance(node, ast.Assign)
                    and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and _qualified(node.value.func) == "urllib.request.Request"]
        returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
        if len(requests) != 1 or len(returns) != 1:
            continue
        request = requests[0]
        args = request.value.args
        if not args or not isinstance(args[0], ast.BinOp) or not isinstance(args[0].op, ast.Add):
            continue
        origin, path = args[0].left, args[0].right
        if (not isinstance(origin, ast.Constant) or origin.value not in {
                "http://localhost:9110", "http://127.0.0.1:9110"}
                or not isinstance(path, ast.Name) or path.id != path_name):
            continue
        keywords = {item.arg: item.value for item in request.value.keywords}
        if not isinstance(keywords.get("method"), ast.Constant) or keywords["method"].value != "POST":
            continue
        data = keywords.get("data")
        if (not isinstance(data, ast.Call) or not isinstance(data.func, ast.Attribute)
                or data.func.attr != "encode" or not isinstance(data.func.value, ast.Call)
                or _qualified(data.func.value.func) != "json.dumps"
                or len(data.func.value.args) != 1 or not isinstance(data.func.value.args[0], ast.Name)
                or data.func.value.args[0].id != body_name):
            continue
        value = returns[0].value
        request_name = request.targets[0].id
        if (not isinstance(value, ast.Call) or _qualified(value.func) != "json.load" or len(value.args) != 1
                or not isinstance(value.args[0], ast.Call)
                or _qualified(value.args[0].func) != "urllib.request.urlopen"
                or len(value.args[0].args) != 1 or not isinstance(value.args[0].args[0], ast.Name)
                or value.args[0].args[0].id != request_name):
            continue
        if sum(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == request_name
               for node in ast.walk(function)) != 1:
            continue
        helpers[function.name] = {"helper_name": function.name, "http_origin": origin.value,
                                  "helper_ast_sha256": hashlib.sha256(ast.dump(function).encode()).hexdigest()}

    def get_field(node: ast.AST, variable: str, field: str) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == variable and bool(node.args)
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == field
                and len(node.args) <= 2 and not node.keywords
                and (len(node.args) == 1 or isinstance(node.args[1], ast.Constant) and node.args[1].value == ""
                     or field == "content" and isinstance(node.args[1], ast.Name) and node.args[1].id == variable))

    for loop in (node for node in ast.walk(tree) if isinstance(node, ast.For) and isinstance(node.target, ast.Name)):
        iterable = loop.iter
        if isinstance(iterable, ast.Name):
            assignments = [node for node in tree.body if isinstance(node, ast.Assign) and node.lineno < loop.lineno
                           and any(isinstance(target, ast.Name) and target.id == iterable.id for target in node.targets)]
            if not assignments:
                continue
            iterable = assignments[-1].value
        try:
            requested = ast.literal_eval(iterable)
        except (ValueError, TypeError):
            continue
        if not isinstance(requested, (list, tuple)) or not requested or not all(
                isinstance(item, str) and re.fullmatch(r"msg_[A-Za-z0-9_-]+", item) for item in requested):
            continue
        message_id_name = loop.target.id
        for index, assignment in enumerate(loop.body):
            if (not isinstance(assignment, ast.Assign) or len(assignment.targets) != 1
                    or not isinstance(assignment.targets[0], ast.Name)
                    or not isinstance(assignment.value, ast.Call)
                    or _qualified(assignment.value.func) not in helpers):
                continue
            read = assignment.value
            if (len(read.args) != 2 or not isinstance(read.args[0], ast.Constant)
                    or read.args[0].value != "/slack/messages/get" or not isinstance(read.args[1], ast.Dict)):
                continue
            body = read.args[1]
            if not any(isinstance(key, ast.Constant) and key.value == "message_id"
                       and isinstance(value, ast.Name) and value.id == message_id_name
                       for key, value in zip(body.keys, body.values)):
                continue
            response = assignment.targets[0].id
            for header_index in range(index + 1, len(loop.body) - 1):
                header, body_print = loop.body[header_index:header_index + 2]
                if not all(isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                           and _qualified(node.value.func) == "print" and not node.value.keywords
                           for node in [header, body_print]):
                    continue
                header_args, body_args = header.value.args, body_print.value.args
                if len(header_args) != 4 or len(body_args) != 1:
                    continue
                delimiter = header_args[0]
                if not (isinstance(delimiter, ast.BinOp) and isinstance(delimiter.op, ast.Mult)
                        and isinstance(delimiter.left, ast.Constant) and delimiter.left.value == "="
                        and isinstance(delimiter.right, ast.Constant) and delimiter.right.value == 20
                        and isinstance(header_args[1], ast.Name) and header_args[1].id == message_id_name
                        and get_field(header_args[2], response, "timestamp")
                        and get_field(header_args[3], response, "sender")
                        and get_field(body_args[0], response, "content")):
                    continue
                if any(isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
                       and node.id in {response, message_id_name}
                       for statement in loop.body[index + 1:header_index + 2] for node in ast.walk(statement)):
                    continue
                # The observed reader may cache the same response in out[mid];
                # reject transformations or calls between HTTP return and print.
                intermediate = loop.body[index + 1:header_index]
                if any(not (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                            and isinstance(statement.targets[0], ast.Subscript)
                            and isinstance(statement.value, ast.Name) and statement.value.id == response
                            and not any(isinstance(node, ast.Call) for node in ast.walk(statement.targets[0])))
                       for statement in intermediate):
                    continue
                return {**helpers[_qualified(read.func)], "endpoint": "/slack/messages/get",
                        "python_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "projection_ast_sha256": hashlib.sha256(ast.dump(loop).encode()).hexdigest(),
                        "requested_message_ids": sorted(set(requested)),
                        "header": "20 equals signs, requested message ID, response timestamp, response sender",
                        "body": "same decoded HTTP response content field", "proof": "static_python_ast"}
    return None


def _matches_endpoint(call: dict, endpoint: str) -> bool:
    name = (call.get("function") or {}).get("name", "")
    args = _args(call)
    if name == "http_request":
        return urlsplit(str(args.get("url", ""))).path == endpoint
    native_name = "gmail_get_message" if endpoint.startswith("/gmail/") else "slack_get_message"
    if name == native_name:
        return True
    if name not in {"exec", "shell", "bash", "terminal"}:
        return False
    command = str(args.get("command", ""))
    if not re.search(r"\bcurl\b|\bwget\b|\b(?:requests|httpx|urllib\.request)\.|\b(?:urlopen|http_request)\s*\(", command):
        return False
    # A command must name the exact HTTP endpoint, not a list/send/audit path.
    return bool(re.search(r"https?://[^/\s\"'\\]+" + re.escape(endpoint)
                          + r"(?![A-Za-z0-9_./-])", command)) or (
                              endpoint == "/slack/messages/get" and _formatted_slack_proof(call) is not None)


def _observed_ids(text: str, endpoint: str, proof: dict | None) -> tuple[set[str], str]:
    ids = _message_ids(text, endpoint)
    if ids:
        return ids, "json_message_object"
    if proof is None:
        return set(), "unproven"
    headers = list(re.finditer(r"(?m)^={20} (msg_[A-Za-z0-9_-]+) (\S+) (@\S+)\s*$", text))
    requested = set(proof["requested_message_ids"])
    found = {header.group(1) for index, header in enumerate(headers)
             if header.group(1) in requested and text[header.end():headers[index + 1].start()
                                                      if index + 1 < len(headers) else len(text)].strip()}
    return found, "formatted_slack_header_body"


def _message_ids(text: str, endpoint: str) -> set[str]:
    """Parse JSON objects amid shell prefixes and multiple HTTP responses."""
    decoder = json.JSONDecoder()
    found = set()
    position = 0
    while (start := text.find("{", position)) >= 0:
        try:
            value, length = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        position = start + length
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if all(isinstance(item.get(key), str) for key in SCHEMAS[endpoint]) and item["message_id"]:
                    found.add(item["message_id"])
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return found


def _json_body_spans(text: str, endpoint: str) -> list[tuple[str, int, int]]:
    """Locate body string tokens without rewriting JSON metadata or siblings."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Ambiguous duplicate JSON key in message result")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    spans = []
    body_field = SCHEMAS[endpoint][-1]

    def whitespace(position):
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    def visit(value, start):
        if not isinstance(value, (dict, list)):
            return
        position = whitespace(start + 1)
        for key in value if isinstance(value, dict) else range(len(value)):
            if isinstance(value, dict):
                _, position = decoder.raw_decode(text, position)
                position = whitespace(whitespace(position) + 1)  # colon
            child_start = position
            child, position = decoder.raw_decode(text, child_start)
            if (isinstance(value, dict) and key == body_field and value.get("message_id")
                    and all(isinstance(value.get(field), str) for field in SCHEMAS[endpoint])):
                spans.append((value["message_id"], child_start, position))
            visit(child, child_start)
            position = whitespace(position)
            if position < len(text) and text[position] == ",":
                position = whitespace(position + 1)

    position = 0
    while match := re.search(r"[\[{]", text[position:]):
        start = position + match.start()
        try:
            value, position = decoder.raw_decode(text, start)
        except json.JSONDecodeError as exc:
            raise ValueError("Unparseable JSON message result; refusing body insertion") from exc
        visit(value, start)
    return spans


def _insert_message_body(raw_content: str | list, payload: dict, endpoint: str,
                         source: dict, response_format: str, formatting_proof: dict | None,
                         file_proof: dict | None) -> tuple[str | list, dict]:
    text = _text(raw_content)
    proof = file_proof or formatting_proof
    requested = set(proof["requested_message_ids"]) if proof else set(
        re.findall(r"msg_[A-Za-z0-9_-]+", json.dumps(_args(source))))
    if not requested:
        raise ValueError("Message body insertion requires a proven requested message ID")
    if response_format == "json_message_object":
        spans = _json_body_spans(text, endpoint)
    elif response_format == "formatted_slack_header_body" and formatting_proof:
        headers = list(re.finditer(
            r"(?m)^={20} (msg_[A-Za-z0-9_-]+) \S+ @\S+[^\S\r\n]*\r?\n", text))
        spans = [(header.group(1), header.end(), header.end())
                 for index, header in enumerate(headers)
                 if text[header.end():headers[index + 1].start()
                         if index + 1 < len(headers) else len(text)].strip()]
    else:
        raise ValueError("No proven message body field; refusing body insertion")
    eligible = [span for span in spans if span[0] in requested]
    target = payload.get("target_message_id")
    if target is not None:
        eligible = [span for span in eligible if span[0] == target]
    if len(eligible) != 1:
        raise ValueError("Ambiguous or missing requested message body; set an unambiguous target_message_id")
    message_id, start, end = eligible[0]
    prefix = payload["text"] + "\n\n"
    if response_format == "json_message_object":
        replacement = json.dumps(prefix + json.loads(text[start:end]), ensure_ascii=False)
        payload_end = start + 1 + len(json.dumps(prefix, ensure_ascii=False)[1:-1])
    else:
        replacement, payload_end = prefix, start + len(prefix)
    if payload_end > SUMMARY_LIMIT:
        raise ValueError("Complete message body payload would be truncated by the native summary")
    if isinstance(raw_content, str):
        modified = text[:start] + replacement + text[end:]
    else:
        modified = copy.deepcopy(raw_content)
        offset = 0
        for index, part in enumerate(raw_content):
            value = part.get("text", "") if isinstance(part, dict) else part
            if isinstance(value, str) and offset <= start <= end <= offset + len(value):
                value = value[:start - offset] + replacement + value[end - offset:]
                if isinstance(part, dict):
                    modified[index]["text"] = value
                else:
                    modified[index] = value
                break
            offset += len(str(value)) + 1
        else:
            raise ValueError("Message body crosses native content blocks; refusing body insertion")
    return modified, {"message_id": message_id, "body_field": SCHEMAS[endpoint][-1],
                      "insertion_position": "before_original_message_body"}


def _rows(turn: dict, call_id: str):
    for field in FIELDS:
        for row in turn.get(field) or []:
            if (isinstance(row, dict)
                    and _id(row.get("tool_call_id") or row.get("toolCallId")) == call_id
                    and not _result_failed(row)):
                yield field, row


def _result_failed(row: dict) -> bool:
    """A message-shaped response cannot override an explicit tool failure."""
    details = row.get("details") or {}
    if not isinstance(details, dict):
        return True
    if (any(row.get(flag) is True for flag in ("has_error", "isError", "is_error"))
            or details.get("status") in {"error", "failed", "cancelled", "aborted"}):
        return True
    text = _text(row.get("content"))
    codes = re.findall(r"(?:Process|Command) exited with code (-?\d+)", text, re.I)
    return (any(int(code) != 0 for code in codes)
            or any(code is not None and (type(code) is not int or code != 0)
                   for code in (row.get("exit_code"), details.get("exitCode")))
            or "Traceback (most recent call last)" in text)


def _session_handle(text: str) -> str | None:
    match = re.search(r"Command still running \(session ([A-Za-z0-9_.-]+), pid \d+\)", text)
    return match.group(1) if match else None


_FILE_CACHE_TEMPLATE = '''import json, time, urllib.request
ids = []
out = {}
for mid in ids:
    if mid in out:
        continue
    for attempt in range(4):
        try:
            req = urllib.request.Request("http://localhost:9110/slack/messages/get",
                data=json.dumps({"message_id": mid}).encode(),
                headers={"Content-Type": "application/json"})
            r = json.load(urllib.request.urlopen(req, timeout=15))
            out[mid] = r
            break
        except Exception as e:
            print(mid, "retry", e)
            time.sleep(3)
    time.sleep(2.5)
with open("/fixture-message-cache.json", "w") as f:
    json.dump(out, f, indent=1)
print(len(out), "fetched")
'''


def _file_requested_ids(node: ast.AST) -> list[str] | None:
    """Evaluate only literal IDs or the observed bounded msg_{i} range."""
    try:
        values = ast.literal_eval(node)
    except (ValueError, TypeError):
        values = None
    if isinstance(values, (list, tuple)):
        requested = list(values)
    elif (isinstance(node, ast.ListComp) and len(node.generators) == 1
          and isinstance(node.elt, ast.JoinedStr) and len(node.elt.values) == 2):
        generator = node.generators[0]
        prefix, value = node.elt.values
        call = generator.iter
        if (generator.ifs or generator.is_async or not isinstance(generator.target, ast.Name)
                or not isinstance(prefix, ast.Constant) or prefix.value != "msg_"
                or not isinstance(value, ast.FormattedValue) or value.conversion != -1
                or value.format_spec is not None or not isinstance(value.value, ast.Name)
                or value.value.id != generator.target.id or not isinstance(call, ast.Call)
                or _qualified(call.func) != "range" or call.keywords or len(call.args) != 2
                or not all(isinstance(arg, ast.Constant) and type(arg.value) is int for arg in call.args)):
            return None
        start, end = (arg.value for arg in call.args)
        if not 0 <= start < end <= start + 500:
            return None
        requested = [f"msg_{index}" for index in range(start, end)]
    else:
        return None
    if (not requested or len(requested) > 500 or len(set(requested)) != len(requested)
            or not all(isinstance(value, str) and re.fullmatch(r"msg_[A-Za-z0-9_-]+", value)
                       for value in requested)):
        return None
    return requested


def _file_cache_source_proof(call: dict) -> dict | None:
    """Whitelist the observed fetch/cache script, never arbitrary Python effects."""
    if (call.get("function") or {}).get("name") not in {"exec", "shell", "bash", "terminal"}:
        return None
    command = str(_args(call).get("command", ""))
    match = re.fullmatch(
        r"(?:cd[ \t]+/tmp_workspace[ \t]+&&[ \t]+)?(?:python|python3)[ \t]+-[ \t]+<<"
        r"(?P<quote>['\"])(?P<end>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)[ \t]*\n"
        r"(?P<source>[\s\S]*?)\n(?P=end)[ \t\n]*", command)
    # The observed quoted delimiter disables shell interpolation, including in
    # Python comments. Unquoted heredocs and -c shell quoting are not supported.
    if match is None:
        return None
    source = match.group("source")
    try:
        tree = ast.parse(source)
        if len(tree.body) != 6 or not isinstance(tree.body[1], ast.Assign):
            return None
        requested = _file_requested_ids(tree.body[1].value)
        file_open = tree.body[4].items[0].context_expr
        path = file_open.args[0].value
        requests = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and _qualified(node.func) == "urllib.request.Request"]
        if (requested is None or not isinstance(path, str) or posixpath.normpath(path) != path
                or not path.startswith(("/tmp/", "/tmp_workspace/")) or len(requests) != 1
                or not isinstance(requests[0].args[0], ast.Constant)
                or requests[0].args[0].value not in {"http://localhost:9110/slack/messages/get",
                                                      "http://127.0.0.1:9110/slack/messages/get"}):
            return None
        normalized = copy.deepcopy(tree)
        normalized.body[1].value = ast.List(elts=[], ctx=ast.Load())
        normalized.body[4].items[0].context_expr.args[0] = ast.Constant("/fixture-message-cache.json")
        next(node for node in ast.walk(normalized) if isinstance(node, ast.Call)
             and _qualified(node.func) == "urllib.request.Request").args[0] = ast.Constant(
                 "http://localhost:9110/slack/messages/get")
        if ast.dump(normalized) != ast.dump(ast.parse(_FILE_CACHE_TEMPLATE)):
            return None
    except (SyntaxError, AttributeError, IndexError, TypeError, ValueError):
        return None
    return {"policy": "literal-urllib-json-cache-to-native-read-v1", "file_path": path,
            "requested_message_ids": requested, "python_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "source_ast_sha256": hashlib.sha256(ast.dump(tree).encode()).hexdigest(),
            "data_flow": "json.load(urlopen(req)) -> unmodified out[mid] -> json.dump(out, file)"}


def _file_result(turn: dict, call: dict) -> dict | None:
    rows = list(_rows(turn, _id(call.get("id"))))
    if any(sum(field == expected for field, _ in rows) != 1 for expected in FIELDS):
        return None
    views = dict(rows)
    row = views["tool_results_raw"]
    text = row.get("content")
    if (not isinstance(text, str)
            or any(not isinstance(view.get("content"), str) for view in views.values())
            or any(view.get(flag) is True for view in views.values() for flag in ("has_error", "isError", "is_error"))
            or any(_file_failed(view) for view in views.values())
            or any(views[field].get("content") != text[:SUMMARY_LIMIT] for field in FIELDS[:2])):
        return None
    return row


def _file_failed(row: dict) -> bool:
    text = row["content"]
    details = row.get("details") or {}
    if not isinstance(details, dict):
        return True
    codes = re.findall(r"(?:Process|Command) exited with code (-?\d+)", text, re.I)
    explicit = [row.get("exit_code"), details.get("exitCode")]
    return (any(int(code) != 0 for code in codes)
            or any(code is not None and (type(code) is not int or code != 0) for code in explicit)
            or "Traceback (most recent call last)" in text)


def _file_completed(row: dict, count: int) -> bool:
    text = row["content"]
    details = row.get("details") or {}
    if not isinstance(details, dict):
        return False
    zero = (row.get("exit_code") == 0 or details.get("exitCode") == 0
            or bool(re.search(r"(?:Process|Command) exited with code 0(?:\D|$)", text, re.I)))
    fetched = re.findall(r"(?m)^\s*(\d+) fetched\s*$", text)
    return (not _file_failed(row) and zero and fetched == [str(count)]
            and not re.search(r"still running", text, re.I))


def _file_cached_carrier(turns: list[dict], sources: list, endpoint: str):
    if endpoint != "/slack/messages/get":
        return None, None
    proofs = [(index, call, proof) for index, call in sources
              if (proof := _file_cache_source_proof(call)) is not None]
    # Multiple candidate writers create an ambiguous file lineage. Do not guess.
    if len(proofs) != 1:
        return None, None
    index, source, proof = proofs[0]
    if len(turns[index].get("tool_calls", [])) != 1:
        return None, None
    first = _file_result(turns[index], source)
    if first is None or _file_failed(first):
        return None, None
    handle = _session_handle(first["content"])
    done = _file_completed(first, len(proof["requested_message_ids"]))
    if not handle and not done:
        return None, None
    completed_by = source if done else None
    completed_at = index if done else None
    for result_index in range(index + 1, len(turns)):
        calls = turns[result_index].get("tool_calls") or []
        if not calls:
            continue
        if len(calls) != 1:
            return None, None
        call = calls[0]
        name, args = (call.get("function") or {}).get("name"), _args(call)
        row = _file_result(turns[result_index], call)
        if row is None or _file_failed(row):
            return None, None
        if (handle and name == "process" and args.get("action") in {"poll", "log"}
                and args.get("sessionId") == handle):
            if done:
                return None, None
            if _file_completed(row, len(proof["requested_message_ids"])):
                done, completed_by, completed_at = True, call, result_index
            continue
        if not (done and name == "read" and args == {"path": proof["file_path"]}):
            return None, None  # Includes all intervening writes/execs/other handles.
        try:
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("Duplicate cached message key")
                    value[key] = item
                return value
            data = json.loads(row["content"], object_pairs_hook=unique)
        except (ValueError, TypeError):
            return None, None
        requested = set(proof["requested_message_ids"])
        if (not isinstance(data, dict) or set(data) != requested
                or not all(isinstance(message, dict) and message.get("message_id") == key
                           and all(isinstance(message.get(field), str) and message[field]
                                   for field in SCHEMAS[endpoint]) for key, message in data.items())):
            return None, None
        file_proof = {**proof, "completion_call_id": completed_by["id"],
            "completion_turn_num": turns[completed_at].get("turn_num"),
            "process_session_id": handle, "read_call_id": call["id"], "read_turn_num": turns[result_index].get("turn_num"),
            "read_raw_sha256": hashlib.sha256(row["content"].encode()).hexdigest(),
            "observed_message_count": len(data), "no_intervening_unproven_calls": True}
        candidate = (result_index, row, "tool_results_raw", source, call,
                     "file_backed_message_read_result", requested, handle, "json_message_object", None)
        return candidate, file_proof
    return None, None


def inject_uploaded(raw: dict, payload: dict, endpoint: str) -> tuple[dict, dict]:
    """Insert the existing canary text only into a linked successful read result.

    Deferred shell output requires the original call's process-session handle and
    a later native process poll/log call using that exact handle.
    """
    if endpoint not in SCHEMAS:
        raise ValueError(f"Unsupported message-read endpoint: {endpoint}")
    if not isinstance(payload.get("text"), str) or not payload["text"]:
        raise ValueError("Canary payload must contain nonempty text")
    mode = payload.get("insertion_mode", "prefix")
    if mode not in {"prefix", "message_body"}:
        raise ValueError(f"Unsupported insertion_mode: {mode}")
    target = payload.get("target_message_id")
    if target is not None and (not isinstance(target, str) or not target):
        raise ValueError("target_message_id must be a nonempty string")
    poisoned = copy.deepcopy(raw)
    turns = poisoned.get("turns", [])
    call_ids = [_id(call.get("id")) for turn in turns for call in turn.get("tool_calls") or []
                if isinstance(call, dict) and _id(call.get("id"))]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("Ambiguous duplicate native tool-call identity")
    sources = [(index, call) for index, turn in enumerate(turns)
               for call in turn.get("tool_calls") or []
               if isinstance(call, dict) and _matches_endpoint(call, endpoint) and _id(call.get("id"))]
    if not sources:
        raise ValueError(f"No real message-read call for {endpoint} in uploaded session")

    candidate = None
    for index, source in sources:
        source_id = _id(source["id"])
        formatting_proof = _formatted_slack_proof(source) if endpoint == "/slack/messages/get" else None
        handles = set()
        markers = set(re.findall(r"msg_[A-Za-z0-9_-]+", json.dumps(_args(source))))
        for result_index in range(index, len(turns)):
            for field, row in _rows(turns[result_index], source_id):
                text = _text(row.get("content"))
                ids, response_format = _observed_ids(text, endpoint, formatting_proof)
                if ids and (not markers or ids & markers) and (mode != "message_body" or target is None or target in ids):
                    candidate = (result_index, row, field, source, source, "direct_message_read", ids, None,
                                 response_format, formatting_proof)
                    break
                handle = _session_handle(text)
                if handle and field == "tool_results_raw":
                    handles.add((result_index, handle))
            if candidate:
                break
        if candidate:
            break
        for handle_index, handle in sorted(handles):
            for poll_index in range(handle_index + 1, len(turns)):
                for poll in turns[poll_index].get("tool_calls") or []:
                    args = _args(poll)
                    if ((poll.get("function") or {}).get("name") != "process"
                            or args.get("action") not in {"poll", "log"}
                            or args.get("sessionId") != handle or not _id(poll.get("id"))):
                        continue
                    for result_index in range(poll_index, len(turns)):
                        for field, row in _rows(turns[result_index], _id(poll["id"])):
                            ids, response_format = _observed_ids(_text(row.get("content")), endpoint, formatting_proof)
                            if ids and (not markers or ids & markers) and (mode != "message_body" or target is None or target in ids):
                                candidate = (result_index, row, field, source, poll,
                                             "deferred_message_read_result", ids, handle,
                                             response_format, formatting_proof)
                                break
                        if candidate:
                            break
                    if candidate:
                        break
                if candidate:
                    break
            if candidate:
                break
        if candidate:
            break
    file_proof = None
    if candidate is None:
        candidate, file_proof = _file_cached_carrier(turns, sources, endpoint)
    if candidate is None:
        raise ValueError(f"No proven linked real message-read result for {endpoint}")

    result_index, _, selected_field, source, result_call, kind, ids, handle, response_format, formatting_proof = candidate
    result_id = _id(result_call["id"])
    views = list(_rows(turns[result_index], result_id))
    if any(sum(field == expected for field, _ in views) != 1 for expected in FIELDS):
        raise ValueError("Linked result must have exactly one row in all three native stored views")
    by_field = dict(views)
    raw_content = by_field["tool_results_raw"].get("content")
    raw_text = _text(raw_content)
    if any(by_field[field].get("content") != raw_text[:SUMMARY_LIMIT] for field in FIELDS[:2]):
        raise ValueError("Linked result has inconsistent native stored views")
    if not isinstance(raw_content, (str, list)):
        raise ValueError("Linked message-read content is not editable text")
    body_audit = {}
    if mode == "message_body":
        modified_raw, body_audit = _insert_message_body(
            raw_content, payload, endpoint, source, response_format, formatting_proof, file_proof)
    else:
        # Frozen payloads retain the original whole-result prefix behavior.
        prefix = payload["text"] + "\n\n"
        modified_raw = prefix + raw_content if isinstance(raw_content, str) else [
            {"type": "text", "text": payload["text"] + "\n"}, *raw_content]
    modified_text = _text(modified_raw)
    changed = []
    for field, row in views:
        content = row["content"]
        before = json.dumps(content, ensure_ascii=False) if isinstance(content, list) else content
        row["content"] = modified_raw if field == "tool_results_raw" else modified_text[:SUMMARY_LIMIT]
        after = json.dumps(row["content"], ensure_ascii=False) if isinstance(content, list) else row["content"]
        changed.append({"field": field, "original_content_sha256": hashlib.sha256(before.encode()).hexdigest(),
                        "modified_content_sha256": hashlib.sha256(after.encode()).hexdigest()})
    source_index = next(index for index, call in sources if call is source)
    chain = {
        "source_turn_num": turns[source_index].get("turn_num"),
        "source_call_id": source["id"],
        "source_arguments_sha256": hashlib.sha256(json.dumps(_args(source), sort_keys=True).encode()).hexdigest(),
        "result_call_id": result_call["id"], "result_turn_num": turns[result_index].get("turn_num"),
    }
    if handle and kind == "deferred_message_read_result":
        handle_index, handle_row = next(
            (index, row) for index in range(source_index, result_index)
            for field, row in _rows(turns[index], _id(source["id"]))
            if field == "tool_results_raw" and _session_handle(_text(row.get("content"))) == handle)
        chain.update(process_handle_turn_num=turns[handle_index].get("turn_num"),
                     process_handle_result_sha256=hashlib.sha256(_text(handle_row.get("content")).encode()).hexdigest(),
                     process_session_id=handle, poll_action=_args(result_call)["action"],
                     poll_session_id=_args(result_call)["sessionId"])
    if file_proof:
        chain.update(file_backed_read=file_proof)
    return poisoned, {
        "endpoint": endpoint, "turn_num": turns[result_index].get("turn_num"),
        "carrier_kind": kind, "carrier_tool_name": (result_call.get("function") or {}).get("name"),
        "carrier_tool_call_id": result_call["id"], "source_tool_calls": [{
            "tool_name": (source.get("function") or {}).get("name"), "tool_call_id": source["id"]}],
        "process_session_id": handle, "message_ids_matched": sorted(ids), "selected_field": selected_field,
        "insertion_mode": mode, "insertion_position": "before_original_tool_result", **body_audit,
        "native_summary_max_chars": SUMMARY_LIMIT,
        "summary_reconstruction": (
            "Insert only inside the selected message body; preserve other raw bytes and original body; regenerate views from modified raw text[:4000]."
            if mode == "message_body" else
            "Preserve complete original raw content after existing canary text; regenerate tool_results and tool_observations from modified raw text[:4000]."),
        "validated_message_schema": list(SCHEMAS[endpoint]) if response_format == "json_message_object" else None,
        "response_format": response_format,
        "source_formatting_proof": formatting_proof if response_format == "formatted_slack_header_body" else None,
        "source_formatting_proof_sha256": hashlib.sha256(json.dumps(formatting_proof, sort_keys=True).encode()).hexdigest()
        if response_format == "formatted_slack_header_body" else None,
        "call_chain": chain,
        "fields_changed": changed, "changed_field_views": len(changed),
    }
