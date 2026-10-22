"""Opt-in, metadata-only candidate repairs; published banks and historical evidence stay immutable."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import yaml
from scripts.wildclawbench.protocol import execution_root, canonical_hash, tree_hash, write_json

from sharedskill.skill_bundle import read_skill_bundle_with_meta

POLICY = "quote-invalid-description-v1"
TRANSCRIPT_POLICY = "speech-only-dub-transcripts-v1"
PROMPT = ("\nEvery proposed SKILL.md must have valid YAML frontmatter with string name and description. "
          "Quote description as one JSON string, especially when it contains colons or quotes.\n")
_SOURCE = Path(__file__).read_bytes()
SOURCE_SHA256 = hashlib.sha256(_SOURCE).hexdigest()


def description_repair(raw: bytes, name: str | None) -> bytes:
    match = re.match(rb"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", raw, re.S)
    if not match:
        raise ValueError("Skill must have YAML frontmatter")
    front = match.group(1)
    try:
        metadata = yaml.safe_load(front)
    except yaml.YAMLError:
        lines = list(re.finditer(rb"(?m)^description:[ \t]*(.*?)(\r?)$", front))
        if len(lines) != 1 or not lines[0].group(1):
            raise ValueError("Only a single-line description can be repaired")
        line = lines[0]
        description = line.group(1).decode()
        quoted = b"description: " + json.dumps(description, ensure_ascii=False).encode() + line.group(2)
        repaired_front = front[:line.start()] + quoted + front[line.end():]
        metadata = yaml.safe_load(repaired_front)
        if not isinstance(metadata, dict) or metadata.get("description") != description:
            raise ValueError("Description repair changed its text")
        repaired = raw[:match.start(1)] + repaired_front + raw[match.end(1):]
    else:
        repaired = raw
    if (not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"].strip()
            or (name is not None and metadata["name"] != name)
            or not isinstance(metadata.get("description"), str) or not metadata["description"].strip()):
        raise ValueError("Skill frontmatter requires matching name and nonempty string description")
    return repaired


class MetadataRepair:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        path = run_dir / "operations/skill-metadata-revision.json"
        revision = json.loads(path.read_text()) if path.exists() else {}
        self.revision = revision if revision.get("policy") == POLICY else None
        self.source_hash = SOURCE_SHA256

    def transcript_contract(self, number):
        contract = (self.revision or {}).get("transcript_export_contract", {})
        if contract.get("policy") != TRANSCRIPT_POLICY:
            return None
        if (contract.get("skill") != "video-transcribe" or int(contract.get("applies_from_round", 3)) < 3
                or not isinstance(contract.get("text"), str) or not contract["text"].strip()):
            raise ValueError("Transcript contract requires the authorized video skill, text and Night3+ scope")
        if number < int(contract.get("applies_from_round", 3)):
            return None
        evidence = Path(contract.get("source_evidence", ""))
        if (evidence.is_absolute() or ".." in evidence.parts or not evidence.parts
                or evidence.parts[0] != "audits" or not (self.run_dir / evidence).is_file()):
            raise ValueError("Transcript contract requires its recorded audit evidence")
        return contract

    def prompt_for_round(self, number):
        contract = self.transcript_contract(number)
        return PROMPT if not contract else (PROMPT + "\nFrom Night3 onward, video-transcribe proposals must follow "
            "the user-authorized transcript export contract: " + contract["text"] + "\n")

    def check(self):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != self.source_hash:
            raise ValueError("Metadata repair module changed after loading")
        frozen = json.loads((self.run_dir / "frozen-protocol.json").read_text())
        if self.revision and canonical_hash(frozen) != self.revision.get("frozen_protocol_sha256"):
            raise ValueError("Metadata repair revision has a different frozen protocol")
        return frozen

    def save_source(self, operations):
        self.check()
        snapshot = operations / "policies" / (self.source_hash + ".metadata-repair.py")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.exists() and snapshot.read_bytes() != _SOURCE:
            raise ValueError("Metadata repair source snapshot changed")
        if not snapshot.exists():
            snapshot.write_bytes(_SOURCE)
        write_json(snapshot.with_suffix(".json"), {"policy": POLICY, "source_sha256": self.source_hash,
                                                 "revision": self.revision})

    def validate(self, paths, name):
        validator = self.revision.get("actual_frontmatter_validator", {})
        path = Path(validator.get("path", ""))
        if (validator.get("runtime") != "node" or not path.is_file()
                or tree_hash(path.parent) != validator.get("tree_sha256")):
            raise ValueError("Actual OpenClaw frontmatter validator provenance differs")
        result = subprocess.run(["node", str(path), *(str(p) for p in paths)], capture_output=True, text=True,
                                timeout=validator.get("timeout_seconds", 20), check=False)
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if len(rows) != len(paths) or result.returncode not in {0, 1}:
            raise ValueError("Actual OpenClaw frontmatter validator did not produce complete evidence")
        final = rows[-1]
        if (not final.get("valid") or final.get("name") != name or final.get("description_type") != "string"
                or not 0 < final.get("description_chars", 0) <= 1024
                or any(row.get("path") != str(p.resolve())
                       or row.get("skill_sha256") != hashlib.sha256(p.read_bytes()).hexdigest()
                       for row, p in zip(rows, paths))):
            raise ValueError("Candidate fails actual OpenClaw name/description parser gate")
        return {"validator": validator, "command": ["node", str(path), *(str(p) for p in paths)],
                "returncode": result.returncode, "results": rows, "stderr": result.stderr}

    def repair_candidate(self, candidate, directory):
        name, original = candidate["name"], Path(candidate["bundle_dir"])
        if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("Unsafe metadata repair skill name")
        raw = (original / "SKILL.md").read_bytes()
        expected_name = name
        frozen_entry = self.run_dir / "banks/S0" / name / "SKILL.md"
        if candidate.get("action") == "refine" and frozen_entry.is_file():
            frozen_raw = frozen_entry.read_bytes()
            description_repair(frozen_raw, None)
            front = re.match(rb"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", frozen_raw, re.S).group(1)
            expected_name = yaml.safe_load(front)["name"]
        fixed = description_repair(raw, expected_name)
        if fixed == raw:
            self.validate([original / "SKILL.md"], expected_name)
            return self.transcript_candidate(candidate, directory, expected_name)
        original_hash = tree_hash(original)
        if original_hash != candidate["bundle_sha256"]:
            raise ValueError("Original candidate bundle changed before metadata repair")
        index = candidate["_metadata_index"]
        decision = directory / "validation" / f"{index:03d}-{name}" / "decision.json"
        scored = execution_root(self.run_dir) / "validation" / directory.name / f"{index:03d}-{name}" / "candidate"
        if decision.exists() or (scored.exists() and any(scored.rglob("*"))):
            raise ValueError("Refusing metadata repair after a candidate decision or candidate-arm artifact")
        destination = (self.run_dir / "operations/metadata-repair" / f"night{directory.name}"
                       / name / original_hash / "bundle")
        if not destination.exists():
            shutil.copytree(original, destination)
            (destination / "SKILL.md").write_bytes(fixed)
        elif (destination / "SKILL.md").read_bytes() != fixed:
            raise ValueError("Existing repaired bundle differs from the exact description fix")
        expected = {p.relative_to(original).as_posix(): p.read_bytes() for p in original.rglob("*") if p.is_file()}
        actual = {p.relative_to(destination).as_posix(): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
        expected["SKILL.md"] = fixed
        if actual != expected:
            raise ValueError("Metadata repair changed supporting files or Markdown content")
        parser = self.validate([original / "SKILL.md", destination / "SKILL.md"], expected_name)
        _, files, native_hash = read_skill_bundle_with_meta(destination)
        new_hash = tree_hash(destination)
        changed = [dict(item) for item in candidate.get("changed_files", [])]
        entry = next((item for item in changed if item["path"] == "SKILL.md"), None)
        if entry is None:
            entry = {"path": "SKILL.md", "change": "modified", "before_sha256": hashlib.sha256(raw).hexdigest()}
            changed.append(entry)
        entry["after_sha256"] = hashlib.sha256(fixed).hexdigest()
        repaired = {**candidate, "bundle_dir": str(destination), "bundle_sha256": new_hash,
                    "native_tree_sha256": native_hash, "files": files, "changed_files": changed,
                    "candidate_id": canonical_hash({"group": candidate["evidence_group"], "name": name,
                        "bank": candidate["source_bank_sha256"], "bundle": new_hash})[:24],
                    "metadata_repair": {"policy": POLICY, "source_sha256": self.source_hash,
                        "original_bundle_dir": str(original), "original_bundle_sha256": original_hash,
                        "repaired_bundle_sha256": new_hash, "body_and_supporting_files_unchanged": True}}
        write_json(destination.parent / "repair.json", {"revision": self.revision,
                    "original_candidate": candidate, "repaired_candidate": repaired, "actual_parser": parser})
        return self.transcript_candidate(repaired, directory, expected_name)

    def transcript_candidate(self, candidate, directory, expected_name):
        contract = self.transcript_contract(int(directory.name))
        origin = candidate.get("operational_origin", {})
        metadata = candidate.get("metadata_repair", {})
        if (not contract or candidate["name"] != contract["skill"]
                or not (origin.get("policy") == POLICY or metadata.get("policy") == POLICY)):
            return candidate
        original = Path(candidate["bundle_dir"])
        raw = (original / "SKILL.md").read_bytes()
        marker = ("<!-- " + TRANSCRIPT_POLICY + " -->").encode()
        addition = b"\n\n" + marker + b"\n## Transcript export contract (operational revision)\n\n"
        addition += contract["text"].encode() + b"\n"
        if marker in raw:
            if addition not in raw:
                raise ValueError("Existing transcript contract marker differs from the authorized text")
            return candidate
        index, name = candidate["_metadata_index"], candidate["name"]
        decision = directory / "validation" / f"{index:03d}-{name}" / "decision.json"
        arm = execution_root(self.run_dir) / "validation" / directory.name / f"{index:03d}-{name}" / "candidate"
        if decision.exists() or (arm.exists() and any(arm.rglob("*"))):
            raise ValueError("Refusing transcript body revision after a decision or candidate-arm artifact")
        original_hash = tree_hash(original)
        if original_hash != candidate["bundle_sha256"]:
            raise ValueError("Metadata bundle changed before transcript body revision")
        destination = (self.run_dir / "operations/semantic-repair" / f"night{directory.name}" / name
                       / (original_hash + "-" + canonical_hash(contract)[:12]) / "bundle")
        fixed = raw + addition
        if not destination.exists():
            shutil.copytree(original, destination)
            (destination / "SKILL.md").write_bytes(fixed)
        expected = {p.relative_to(original).as_posix(): p.read_bytes() for p in original.rglob("*") if p.is_file()}
        actual = {p.relative_to(destination).as_posix(): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
        expected["SKILL.md"] = fixed
        if actual != expected:
            raise ValueError("Transcript body revision changed undeclared bytes or supporting assets")
        parser = self.validate([original / "SKILL.md", destination / "SKILL.md"], expected_name)
        _, files, native_hash = read_skill_bundle_with_meta(destination)
        new_hash = tree_hash(destination)
        body = lambda data: data[re.match(rb"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", data, re.S).end():]
        record = {"policy": TRANSCRIPT_POLICY, "contract": contract, "source_sha256": self.source_hash,
                  "original_metadata_bundle_dir": str(original), "original_metadata_bundle_sha256": original_hash,
                  "body_changed": True, "supporting_files_unchanged": True,
                  "original_body_sha256": hashlib.sha256(body(raw)).hexdigest(),
                  "revised_body_sha256": hashlib.sha256(body(fixed)).hexdigest(),
                  "original_skill_sha256": hashlib.sha256(raw).hexdigest(),
                  "revised_skill_sha256": hashlib.sha256(fixed).hexdigest(), "bundle_sha256": new_hash}
        changed = [dict(item) for item in candidate.get("changed_files", [])]
        entry = next((item for item in changed if item["path"] == "SKILL.md"), None)
        if entry is None:
            entry = {"path": "SKILL.md", "change": "modified", "before_sha256": hashlib.sha256(raw).hexdigest()}
            changed.append(entry)
        entry["after_sha256"] = record["revised_skill_sha256"]
        rationale = dict(candidate.get("rationale", {}))
        rationale["reason"] = rationale.get("reason", "") + (
            " Additional user-authorized speech-only transcript export contract changes the Markdown body; "
            "this combined candidate is not a metadata-only correction.")
        repaired = {**candidate, "bundle_dir": str(destination), "bundle_sha256": new_hash, "files": files,
                    "native_tree_sha256": native_hash, "changed_files": changed, "rationale": rationale,
                    "engine": "user-directed-operational-candidate-revision",
                    "operational_origin": {**origin, "policy": POLICY,
                        "candidate_revision_origin": "user-directed Markdown transcript export contract",
                        "prior_validation_evidence": contract["source_evidence"]},
                    "candidate_id": canonical_hash({"group": candidate["evidence_group"], "name": name,
                        "bank": candidate["source_bank_sha256"], "bundle": new_hash})[:24],
                    "metadata_repair": {**metadata, "body_and_supporting_files_unchanged": False,
                        "body_changed": True, "supporting_files_unchanged": True,
                        "metadata_only_bundle_dir": str(original), "metadata_only_bundle_sha256": original_hash,
                        "combined_candidate_bundle_sha256": new_hash,
                        "metadata_only_step_body_unchanged": True, "superseded_by_body_revision": TRANSCRIPT_POLICY},
                    "transcript_export_contract": record}
        write_json(destination.parent / "repair.json", {"revision": self.revision,
                   "original_metadata_candidate": candidate, "repaired_candidate": repaired, "actual_parser": parser})
        return repaired

    def published_candidate(self, name, bank, number, frozen):
        ids = self.revision.get("published_skill_source_task_ids", {}).get(name, [])
        task_map = {task["task_id"]: task for task in frozen["tasks"]}
        if not ids or any(task not in task_map for task in ids):
            raise ValueError("Published metadata repair needs explicit real daytime task sources")
        bank_hash, outcomes = tree_hash(bank), []
        for task_id in sorted(set(ids)):
            task = task_map[task_id]
            path = execution_root(self.run_dir) / "day" / f"{number:02d}" / "main/shared" / task["short_id"] / "result.json"
            row = json.loads(path.read_text())
            expected = canonical_hash({"phase": "day", "round": number, "arm": "", "candidate_id": "",
                "task_id": task_id, "bank": bank_hash, "config": canonical_hash(frozen),
                "input": task.get("input_sha256") or task["source_sha256"], "seed": frozen["config"]["seed"]})
            score = row.get("score")
            if (row.get("execution_hash") != expected or row.get("status") != "completed"
                    or not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score)
                    or not 0 <= score <= 1 or not row.get("session", {}).get("session_id")):
                raise ValueError("Published metadata repair requires actual valid matching daytime evidence")
            outcomes.append({"task_id": task_id, "session_id": row["session"]["session_id"],
                             "outcome": {"score": score, "timed_out": row.get("timed_out"),
                                         "tool_errors": row.get("tool_errors")}})
        bundle = bank / name
        _, files, native_hash = read_skill_bundle_with_meta(bundle)
        source_ids = [o["task_id"] for o in outcomes]
        return {"name": name, "skill_name": name, "action": "refine", "bundle_dir": str(bundle),
            "bundle_sha256": tree_hash(bundle), "base_bundle_sha256": tree_hash(bundle),
            "native_tree_sha256": native_hash, "source_bank_sha256": bank_hash,
            "source_skill_name": name, "evidence_group": name, "source_task_ids": source_ids,
            "source_session_ids": [o["session_id"] for o in outcomes], "source_outcomes": outcomes,
            "evidence_source_task_ids": source_ids, "evidence_source_session_ids": [o["session_id"] for o in outcomes],
            "evidence_source_outcomes": outcomes, "files": files, "changed_files": [],
            "rationale": {"source_task_ids": source_ids, "reason": "Operational YAML description quoting only; "
                "the original Markdown body and assets stay identical. Validate actual daytime sources: "
                + ", ".join(source_ids)},
            "operational_origin": {"policy": POLICY, "revision": self.revision}, "validation_status": "not_validated"}

    def apply(self, candidates, directory, bank):
        if not self.revision or int(directory.name) < int(self.revision.get("applies_from_round", 2)):
            return candidates
        frozen = self.check()
        if (directory / "publication.json").exists():
            return candidates
        result = []
        for index, candidate in enumerate(candidates, 1):
            repaired = self.repair_candidate({**candidate, "_metadata_index": index}, directory)
            repaired.pop("_metadata_index", None)
            result.append(repaired)
        if self.revision.get("repair_published_skills") is True:
            existing = {c["name"] for c in result}
            for bundle in sorted(bank.iterdir()):
                if not bundle.is_dir() or bundle.name in existing:
                    continue
                raw = (bundle / "SKILL.md").read_bytes()
                if description_repair(raw, None) != raw:
                    candidate = self.published_candidate(bundle.name, bank, int(directory.name), frozen)
                    candidate["_metadata_index"] = len(result) + 1
                    repaired = self.repair_candidate(candidate, directory)
                    repaired.pop("_metadata_index", None)
                    result.append(repaired)
        return result
