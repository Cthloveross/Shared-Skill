"""Read-only SkillClaw catalog rendering and immutable execution bindings.

The renderer follows SkillClaw 61983b0788010b3ab0f69899b3e22360758bf55d,
skillclaw/skill_manager.py (source SHA256 65c8a7311f8b872f82cc0b0dfb6142ff4b74e61c37a2e368cfb5574392d85cde).
Validation deliberately fails closed instead of silently skipping malformed bundles.
No SkillManager statistics, reload, embedding, or model methods are invoked.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import yaml

UPSTREAM_COMMIT = "61983b0788010b3ab0f69899b3e22360758bf55d"
UPSTREAM_SOURCE_SHA256 = "65c8a7311f8b872f82cc0b0dfb6142ff4b74e61c37a2e368cfb5574392d85cde"
POLICY_VERSION = "skillclaw-61983b-catalog-v1"
PUBLIC_ROOT = "/root/skills"
MAX_CATALOG_CHARS = 30_000
CATALOG_MARKER = "<!-- sharedskill-execution-catalog:"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


class CatalogError(ValueError):
    """Invalid skill, changed execution binding, or unsafe filesystem path."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, (str, int, float, bool, type(None))) or key in result:
            raise CatalogError("Duplicate or non-scalar YAML mapping key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _identifier(value, field):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CatalogError(f"Unsafe {field}: {value!r}")
    return value


def _safe_path(value):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise CatalogError(f"Parent traversal: {path}")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise CatalogError(f"Symlink is not allowed: {item}")
    return path


def _resource_refs(body, files, name):
    """Check bundled resources; keep ambiguous task-output links as diagnostics."""
    refs, unresolved = set(), set()
    targets = re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", body)
    targets += re.findall(r"(?<![\w/])(?:scripts|references|assets|templates)/[\w./%+-]+", body)
    for target in targets:
        target = target.strip()
        if not target:
            continue
        target = target.split()[0].strip("<>").rstrip(".")
        parsed = urlsplit(target)
        if parsed.scheme == "file":
            raise CatalogError(f"Resource escapes bundle {name}: {target}")
        if parsed.scheme or parsed.netloc or target.startswith("#"):
            continue
        value = unquote(parsed.path)
        if not value:
            continue
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise CatalogError(f"Resource escapes bundle {name}: {target}")
        relative = path.as_posix().removeprefix("./")
        exists = relative in files or any(p.startswith(relative.rstrip("/") + "/") for p in files)
        bundled = path.parts[0] in {"scripts", "references", "assets", "templates"}
        if bundled and not exists:
            raise CatalogError(f"Missing bundled resource in {name}: {relative}")
        (refs if exists else unresolved).add(relative)
    return sorted(refs), sorted(unresolved)


def _skill(name, data, files):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CatalogError(f"Non-UTF-8 SKILL.md: {name}") from error
    match = _FRONTMATTER.match(text)
    if not match or len(match[1]) > 131_072:
        raise CatalogError(f"Missing or oversized YAML frontmatter: {name}")
    try:
        fm = yaml.load(match[1], Loader=_UniqueLoader)
    except yaml.YAMLError as error:
        raise CatalogError(f"Invalid YAML in {name}: {error}") from error
    if not isinstance(fm, dict) or not isinstance(fm.get("name"), str) or not fm["name"].strip():
        raise CatalogError(f"Missing frontmatter name: {name}")
    declared_name = _identifier(fm["name"].strip(), "frontmatter name")
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise CatalogError(f"Description must be a nonempty string of at most 1024 characters: {name}")
    disabled = fm.get("disable-model-invocation", False)
    if not isinstance(disabled, bool):
        raise CatalogError(f"disable-model-invocation must be boolean: {name}")
    body = text[match.end() :]
    references, unresolved = _resource_refs(body, files, name)
    return {
        "name": declared_name,
        "directory": name,
        "name_mismatch": declared_name != name,
        "location": PUBLIC_ROOT + "/" + name + "/SKILL.md",
        "description": description.strip(),
        "disabled": disabled,
        "skill_sha256": hashlib.sha256(data).hexdigest(),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "resource_references": references,
        "unresolved_resource_references": unresolved,
    }


def inspect_bank(bank_dir):
    """Read every exact bank file, mode and directory; reject links and stray files."""
    root = _safe_path(bank_dir)
    if not root.is_dir():
        raise CatalogError(f"Missing bank directory: {root}")
    files, directories, payloads = [], [], {}
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode):
            raise CatalogError(f"Symlink in bank: {relative}")
        if stat.S_ISDIR(value.st_mode):
            directories.append({"path": relative, "mode": stat.S_IMODE(value.st_mode)})
            continue
        if not stat.S_ISREG(value.st_mode) or len(Path(relative).parts) < 2:
            raise CatalogError(f"Unexpected bank entry: {relative}")
        data = path.read_bytes()
        after = path.stat()
        if any(
            getattr(after, field) != getattr(value, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
        ):
            raise CatalogError(f"Bank changed while reading: {relative}")
        encoded = relative.encode()
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(data).to_bytes(8, "big") + data)
        files.append(
            {
                "path": relative,
                "size": len(data),
                "mode": stat.S_IMODE(value.st_mode),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        payloads[relative] = data
    names = sorted(p.name for p in root.iterdir())
    if not names:
        raise CatalogError("Empty shared bank")
    skills = []
    for name in names:
        _identifier(name, "skill directory")
        local = {path[len(name) + 1 :]: data for path, data in payloads.items() if path.startswith(name + "/")}
        if "SKILL.md" not in local:
            raise CatalogError(f"Missing SKILL.md: {name}")
        skill = _skill(name, local["SKILL.md"], local)
        skill["files"] = [
            {**row, "path": row["path"][len(name) + 1 :]} for row in files if row["path"].startswith(name + "/")
        ]
        skills.append(skill)
    return {
        "bank_dir": str(root),
        "bank_sha256": digest.hexdigest(),
        "files": files,
        "directories": directories,
        "skills": skills,
    }


def _escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def render_catalog(skills, max_chars=MAX_CATALOG_CHARS):
    """Return the pinned official prompt and full/compact mode, without any I/O."""
    eligible = [skill for skill in skills if not skill.get("disabled", False)]
    if not eligible:
        return "", "empty"

    def catalog(compact):
        lines = [
            "\n\nThe following skills provide specialized instructions for specific tasks.",
            "Use the read tool to load a skill's file when the task matches its "
            + ("name." if compact else "description."),
            "When a skill file references a relative path, resolve it against the skill directory "
            "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
            "",
            "<available_skills>",
        ]
        for skill in eligible:
            lines.extend(["  <skill>", f"    <name>{_escape(skill['name'])}</name>"])
            if not compact:
                lines.append(f"    <description>{_escape(skill['description'])}</description>")
            location = skill.get("location", PUBLIC_ROOT + "/" + skill["name"] + "/SKILL.md")
            lines.extend([f"    <location>{_escape(location)}</location>", "  </skill>"])
        return "\n".join([*lines, "</available_skills>"])

    prompt = catalog(False)
    mode = "full" if len(prompt) <= max_chars else "compact"
    if mode == "compact":
        prompt = catalog(True)
    return "\n".join(
        [
            "## Skills (mandatory)",
            "Before replying: scan <available_skills> <description> entries.",
            "- If exactly one skill clearly applies: read its SKILL.md at <location> with `read`, then follow it.",
            "- If multiple could apply: choose the most specific one, then read/follow it.",
            "- If none clearly apply: do not read any SKILL.md.",
            "Constraints: never read more than one skill up front; only read after selecting.",
            "- When a skill drives external API writes, assume rate limits: prefer fewer larger writes, "
            "avoid tight one-item loops, serialize bursts when possible, and respect 429/Retry-After.",
            prompt.strip(),
            "",
        ]
    ), mode


def _protocol(config):
    if config.get("protocol_hash"):
        return config["protocol_hash"], "frozen-protocol"
    ephemeral = {"protocol_hash", "execution_id", "task", "phase", "round", "user_id"}
    public = {
        k: v
        for k, v in config.items()
        if k not in ephemeral
        and (
            not any(t in k.lower() for t in ("api_key", "token", "password"))
            or k in {"max_tokens", "llm_max_tokens", "judge_max_tokens"}
        )
    }
    return _hash(public), "public-config-sha256"


def _document(config, run_id, execution_id, bank_dir, purpose):
    if purpose != "agent":
        raise CatalogError("Catalog bindings are only valid for purpose=agent")
    bank = inspect_bank(bank_dir)
    text, mode = render_catalog(bank["skills"])
    protocol_hash, source = _protocol(config)
    document = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_source_sha256": UPSTREAM_SOURCE_SHA256,
        "alignment_version": config.get("alignment", {}).get("version"),
        "protocol_hash": protocol_hash,
        "protocol_hash_source": source,
        "run_id": _identifier(run_id, "run_id"),
        "execution_id": _identifier(execution_id, "execution_id"),
        "purpose": purpose,
        "public_root": PUBLIC_ROOT,
        **bank,
        "path_policy": "actual-bundle-directory-preserving-frontmatter-name-v1",
        "catalog_mode": mode,
        "catalog_max_chars": MAX_CATALOG_CHARS,
        "catalog_text": text,
        "catalog_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    return {**document, "binding_sha256": _hash(document)}


def _binding_path(config, run_id, execution_id):
    runtime = _safe_path(config["runtime_dir"])
    return _safe_path(
        runtime
        / "execution-bindings"
        / _identifier(run_id, "run_id")
        / (_identifier(execution_id, "execution_id") + ".json")
    )


def prepare_binding(config: Mapping, run_id, execution_id, bank_dir, purpose="agent") -> dict:
    """Atomically create a binding; identical retries are safe, replacements fail."""
    path = _binding_path(config, run_id, execution_id)
    document = _document(config, run_id, execution_id, bank_dir, purpose)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(temporary, 0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                if load_binding(config, run_id, execution_id) != document:
                    raise CatalogError("Refusing replacement of immutable execution binding")
        finally:
            temporary.unlink(missing_ok=True)
    return document


def load_binding(config: Mapping, run_id, execution_id) -> dict:
    """Verify the stored identity, full bank manifest, and re-rendered catalog."""
    path = _binding_path(config, run_id, execution_id)
    try:
        document = json.loads(path.read_text())
        expected = _document(config, run_id, execution_id, document["bank_dir"], document["purpose"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise CatalogError(f"Invalid or missing execution binding: {path}") from error
    if document != expected:
        raise CatalogError(f"Execution binding, protocol, bank, or catalog changed: {path}")
    return document


def inject_catalog(messages, binding) -> list:
    """Append one marked shared catalog; preserve native catalogs and tool records."""
    if binding.get("purpose") != "agent" or binding.get("policy_version") != POLICY_VERSION:
        raise CatalogError("Invalid agent catalog binding")
    text = binding["catalog_text"]
    if hashlib.sha256(text.encode()).hexdigest() != binding["catalog_sha256"]:
        raise CatalogError("Catalog hash mismatch")
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise CatalogError("messages must be a list of message objects")
    output = copy.deepcopy(messages)
    marker = f"{CATALOG_MARKER}{binding['catalog_sha256']} -->"
    block = marker + "\n" + text
    marked = [m for m in output if CATALOG_MARKER in str(m.get("content", ""))]
    if marked:
        if len(marked) != 1 or marked[0].get("role") != "system":
            raise CatalogError("Conflicting catalog marker")
        content = marked[0]["content"]
        valid = isinstance(content, str) and content.count(CATALOG_MARKER) == 1 and content.endswith(block)
        if isinstance(content, list):
            valid = str(content).count(CATALOG_MARKER) == 1 and content[-1] == {"type": "text", "text": "\n\n" + block}
        if not valid:
            raise CatalogError("Conflicting catalog marker or contents")
        return output
    if not text:
        return output
    system = next((m for m in output if m.get("role") == "system"), None)
    if system is None:
        output.insert(0, {"role": "system", "content": block})
    elif isinstance(system.get("content"), str):
        system["content"] += "\n\n" + block
    elif isinstance(system.get("content"), list):
        system["content"].append({"type": "text", "text": "\n\n" + block})
    else:
        raise CatalogError("Unsupported system content")
    return output
