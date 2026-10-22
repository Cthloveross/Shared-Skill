"""Resolve exact Day identities without treating task-name abbreviations as IDs."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re


POLICY = "native-history-exact-day-id-union-v2"
POLICY_V3 = "native-history-frozen-alias-union-v3"
_TASK_LABEL = re.compile(r"\b\d{2}_[A-Za-z0-9_]+_task_\d+_[A-Za-z0-9_]+\b")
_HEX_SESSION = re.compile(r"(?<![A-Za-z0-9_-])[0-9a-f]{24}(?:-[0-9a-f]{12})?(?![A-Za-z0-9_-])")
_OPAQUE_SESSION_V3 = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[0-9a-f]{24}(?:-[0-9a-f]{12})?|[0-9a-f]{32}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![A-Za-z0-9_-])", re.I)
_DAY_TOKEN = re.compile(r"(?<![A-Za-z0-9_.-])day-[0-9][A-Za-z0-9_./-]*")
_DAY_BASE = re.compile(r"day-(\d{2})[-.](\d{2})")
_DAY_ENUM = re.compile(r"day-(\d{2})([-.])(\d{2})((?:/(?:\d{2}[-.])?\d{2})*)")
_DAY_ATTEMPT = re.compile(r"(day-\d{2}[-.]\d{2}-attempt-\d{3})(?:-|$)")
_SESSION_LABEL = re.compile(
    r"(?<![A-Za-z0-9_])(?:source_session_ids?|session[_ -]?ids?|sessions?"
    r"(?:\s+identit(?:y|ies))?|会话(?:[_ -]?ID|标识)?)"
    r"[\s`\"']*(?:[:=][\s`\"'\[]*)?$", re.I)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _day_spellings(value: str) -> set[str]:
    """Change only the registered category/task separator, never an opaque suffix."""
    match = _DAY_BASE.match(value)
    if match is None:
        return {value}
    suffix = value[match.end():]
    return {f"day-{match[1]}{separator}{match[2]}{suffix}" for separator in ("-", ".")}


def _alias_registry(config: dict, sessions: list[dict]) -> dict:
    tasks = config.get("all_tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{POLICY_V3}: frozen all_tasks registry is required")
    short_to_task, task_to_short = {}, {}
    for row in tasks:
        short, task = row.get("short_id"), row.get("task_id")
        if (not isinstance(short, str) or not re.fullmatch(r"\d{2}\.\d{2}", short)
                or not isinstance(task, str) or not task or short in short_to_task or task in task_to_short):
            raise ValueError(f"{POLICY_V3}: ambiguous or invalid frozen task registry")
        short_to_task[short], task_to_short[task] = task, short
    canonical, exact, aliases, kinds = {}, {}, {}, {}

    def register(alias, source_ids, kind):
        incoming = set(source_ids)
        if not incoming:
            return
        combined = aliases.get(alias, set()) | incoming
        if len({canonical[source]["task_id"] for source in combined}) != 1:
            raise ValueError(f"{POLICY_V3}: cross-task ambiguous alias: {alias}")
        if alias in kinds and kinds[alias] != kind:
            if {kinds[alias], kind} <= {"exact_session", "registered_session_separator"} and aliases[alias] == incoming:
                kind = "exact_session"
            else:
                raise ValueError(f"{POLICY_V3}: conflicting source alias: {alias}")
        aliases[alias], kinds[alias] = combined, kind

    for session in sessions:
        source, normalized, task = (session.get(key) for key in ("source_session_id", "session_id", "task_id"))
        if (not isinstance(source, str) or not source or not isinstance(normalized, str) or not normalized
                or task not in task_to_short or source in canonical):
            raise ValueError(f"{POLICY_V3}: missing, duplicate or out-of-registry Day identity")
        canonical[source] = session
        for identity in {source, normalized}:
            if identity in exact and exact[identity] != source:
                raise ValueError(f"{POLICY_V3}: ambiguous exact native identity: {identity}")
            exact[identity] = source
    for identity, source in sorted(exact.items()):
        for spelling in _day_spellings(identity):
            register(spelling, [source], "exact_session" if spelling == identity else "registered_session_separator")
    for source, session in sorted(canonical.items()):
        short = task_to_short[session["task_id"]]
        expected = "day-" + short
        register(expected, [source], "task_union")
        register(expected.replace(".", "-"), [source], "task_union")
        for identity in {source, session["session_id"]}:
            match = _DAY_BASE.match(identity)
            if match and f"{match[1]}.{match[2]}" != short:
                raise ValueError(f"{POLICY_V3}: session alias disagrees with its frozen task: {identity}")
            attempt = _DAY_ATTEMPT.match(identity)
            if attempt:
                for spelling in _day_spellings(attempt[1]):
                    register(spelling, [source], "registered_attempt_union")
    # Exact session aliases cannot accidentally become multi-session task aliases.
    for alias, source in exact.items():
        if aliases.get(alias) != {source}:
            raise ValueError(f"{POLICY_V3}: exact session conflicts with a task/attempt union: {alias}")
    frozen = {"policy": POLICY_V3, "tasks": sorted(short_to_task.items()),
        "sessions": [{"source_session_id": source, "session_id": session["session_id"], "task_id": session["task_id"]}
                     for source, session in sorted(canonical.items())],
        "aliases": [{"alias": alias, "kind": kinds[alias], "source_session_ids": sorted(values)}
                    for alias, values in sorted(aliases.items())]}
    return {"canonical": canonical, "exact": exact, "aliases": aliases, "kinds": kinds,
            "task_ids": set(task_to_short), "sha256": _digest(frozen)}


def _resolve_alias_history(config: dict, bundles: list[dict], bank: dict, sessions: list[dict]):
    registry = _alias_registry(config, sessions)
    canonical, aliases = registry["canonical"], registry["aliases"]
    task_ids = {row["task_id"] for row in sessions}
    selected, evidence, names = {}, [], set()
    for bundle in bundles:
        name = bundle["name"]
        if name in names:
            raise ValueError(f"{POLICY_V3}: duplicate bundle identity: {name}")
        names.add(name)
        before, found, root = bank.get(name, {}), False, Path(bundle["bundle_dir"])
        for path in sorted(root.glob("history/*_evidence.md")):
            raw = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            if before.get(relative) == raw:
                continue
            text = raw.decode("utf-8")
            matched_tasks = {task for task in task_ids if _mentioned(text, task)}
            exact_matches = {alias for alias in registry["exact"] if _mentioned(text, alias)}
            resolved = {source: row for source, row in canonical.items() if row["task_id"] in matched_tasks}
            resolved.update({registry["exact"][alias]: canonical[registry["exact"][alias]] for alias in exact_matches})
            matched_aliases = []
            for match in _DAY_TOKEN.finditer(text):
                token = match.group().rstrip(".")
                # File names and English hypothetical descriptions retain the same explicit source alias.
                citation = token.removesuffix(".json").removesuffix("-like")
                expanded = []
                for explicit in re.split(r"/(?=day-)", citation):
                    enumeration = _DAY_ENUM.fullmatch(explicit)
                    if not enumeration:
                        expanded.append(explicit)
                        continue
                    indices = [enumeration[3]]
                    for item in enumeration[4].split("/")[1:]:
                        qualified = re.fullmatch(r"(\d{2})[-.](\d{2})", item)
                        if qualified and qualified[1] != enumeration[1]:
                            raise ValueError(f"unverifiable_sources: {name} has cross-category alias enumeration: {explicit}")
                        indices.append(qualified[2] if qualified else item)
                    expanded.extend(f"day-{enumeration[1]}{enumeration[2]}{item}" for item in indices)
                for alias in expanded:
                    if alias not in aliases:
                        raise ValueError(f"unverifiable_sources: {name} has unknown/out-of-Day alias: {alias}")
                    sources = aliases[alias]
                    resolved.update({source: canonical[source] for source in sources})
                    matched_aliases.append({"citation": token, "alias": alias, "kind": registry["kinds"][alias],
                        "source_task_ids": sorted({canonical[source]["task_id"] for source in sources}),
                        "source_session_ids": sorted(sources)})
            opaque = {match.group() for match in _OPAQUE_SESSION_V3.finditer(text)
                      if _SESSION_LABEL.search(text[max(0, match.start() - 96):match.start()])}
            unknown_sessions = sorted(opaque - registry["exact"].keys())
            if unknown_sessions:
                raise ValueError(f"unverifiable_sources: {name} references non-day session identities: {unknown_sessions}")
            found = found or bool(resolved)
            selected.update(resolved)
            evidence.append({"bundle": name, "path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                "source_task_ids": sorted({row["task_id"] for row in resolved.values()}),
                "source_session_ids": sorted(resolved), "source_policy": POLICY_V3,
                "registry_sha256": registry["sha256"], "exact_task_ids_matched": sorted(matched_tasks),
                "exact_session_aliases_matched": sorted(exact_matches), "registered_aliases_matched": matched_aliases,
                "diagnostics": {"unknown_task_labels": sorted(set(_TASK_LABEL.findall(text)) - task_ids),
                                "unknown_labels_added_to_sources": False}})
        if not found:
            raise ValueError(f"unverifiable_sources: {name} has no changed native history with registered Day IDs")
    return [selected[key] for key in sorted(selected)], evidence


def resolve_history_sources_for_config(config: dict, bundles: list[dict], bank: dict, sessions: list[dict]):
    revision = config.get("alignment", {}).get("source_evidence_revision", POLICY)
    if revision == POLICY_V3:
        return _resolve_alias_history(config, bundles, bank, sessions)
    if revision != POLICY:
        raise ValueError("Unsupported explicitly configured source evidence policy: " + str(revision))
    return resolve_history_sources(bundles, bank, sessions)


def _mentioned(text: str, identity: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_./-])" + re.escape(identity)
                          + r"(?![A-Za-z0-9_./-])", text))


def resolve_history_sources(bundles: list[dict], bank: dict, sessions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Use only exact known IDs; unknown task labels never enlarge the union.

    Each changed bundle still needs verifiable new native history. Ambiguous
    identities and explicit unknown hexadecimal session IDs remain errors.
    Deployment checks, candidate content and A/B acceptance are unaffected.
    """
    aliases, canonical = {}, {}
    for session in sessions:
        source_id, normalized = session.get("source_session_id"), session.get("session_id")
        if (not isinstance(source_id, str) or not source_id or not isinstance(normalized, str)
                or not normalized or not isinstance(session.get("task_id"), str) or not session["task_id"]):
            raise ValueError(f"{POLICY}: missing explicit Day session/task identity")
        if source_id in canonical:
            raise ValueError(f"{POLICY}: ambiguous duplicate source session identity: {source_id}")
        canonical[source_id] = session
        for alias in {source_id, normalized}:
            if alias in aliases and aliases[alias] is not session:
                raise ValueError(f"{POLICY}: ambiguous native session identity: {alias}")
            aliases[alias] = session
    task_ids = {session["task_id"] for session in sessions}
    selected, evidence, bundle_names = {}, [], set()
    for bundle in bundles:
        name = bundle["name"]
        if name in bundle_names:
            raise ValueError(f"{POLICY}: duplicate bundle identity: {name}")
        bundle_names.add(name)
        before, found = bank.get(name, {}), False
        root = Path(bundle["bundle_dir"])
        for path in sorted(root.glob("history/*_evidence.md")):
            raw = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            if before.get(relative) == raw:
                continue
            text = raw.decode("utf-8")
            mentioned_tasks = {task for task in task_ids if _mentioned(text, task)}
            mentioned_sessions = {alias for alias in aliases if _mentioned(text, alias)}
            unknown_tasks = sorted(set(_TASK_LABEL.findall(text)) - task_ids)
            unknown_sessions = sorted(set(_HEX_SESSION.findall(text)) - aliases.keys())
            if unknown_sessions:
                raise ValueError(f"unverifiable_sources: {name} references non-day session identities: {unknown_sessions}")
            resolved = {aliases[alias]["source_session_id"]: aliases[alias] for alias in mentioned_sessions}
            resolved.update({session["source_session_id"]: session for session in sessions
                             if session["task_id"] in mentioned_tasks})
            found = found or bool(resolved)
            selected.update(resolved)
            evidence.append({"bundle": name, "path": relative, "sha256": hashlib.sha256(raw).hexdigest(),
                "source_task_ids": sorted({session["task_id"] for session in resolved.values()}),
                "source_session_ids": sorted(resolved), "source_policy": POLICY,
                "exact_task_ids_matched": sorted(mentioned_tasks),
                "exact_session_aliases_matched": sorted(mentioned_sessions),
                "diagnostics": {"unknown_task_labels": unknown_tasks,
                                "unknown_labels_added_to_sources": False}})
        if not found:
            raise ValueError(f"unverifiable_sources: {name} has no changed native history with real day IDs")
    return [selected[key] for key in sorted(selected)], evidence


@contextmanager
def scoped_history_sources(evolution, controller, scope_root: Path, *, imported_bundle_dirs=(), config=None):
    """Install only within an explicitly versioned candidate tree; always restore.

    The caller owns versioning and persistence of the returned evidence. Existing
    artifacts and the frozen source modules are never changed by this hook.
    """
    root = Path(scope_root).resolve()
    if not root.is_dir():
        raise ValueError("Source-policy scope must be an existing versioned directory")
    imports = {Path(path).resolve() for path in imported_bundle_dirs}
    if any(not path.is_dir() for path in imports):
        raise ValueError("An explicitly imported candidate bundle does not exist")
    original = evolution._history_sources
    if getattr(original, "source_policy", None) in {POLICY, POLICY_V3}:
        raise ValueError("A source-policy hook is already installed")
    alias = getattr(controller, "_history_sources", None) if controller is not None else None
    if alias is not None and alias is not original:
        raise ValueError("Controller source resolver is not the native resolver being scoped")

    def scoped(bundles, bank, sessions):
        paths = [Path(bundle["bundle_dir"]).resolve() for bundle in bundles]
        inside = [path.is_relative_to(root) or path in imports for path in paths]
        if any(inside) and not all(inside):
            raise ValueError("A candidate changeset crosses the versioned source-policy scope")
        return resolve_history_sources_for_config(config or {}, bundles, bank, sessions) if inside and all(inside) else original(bundles, bank, sessions)

    scoped.source_policy = (config or {}).get("alignment", {}).get("source_evidence_revision", POLICY)
    scoped.scope_root = str(root)
    scoped.imported_bundle_dirs = sorted(map(str, imports))
    evolution._history_sources = scoped
    if alias is not None:
        controller._history_sources = scoped
    try:
        yield scoped
    finally:
        evolution._history_sources = original
        if alias is not None:
            controller._history_sources = alias
