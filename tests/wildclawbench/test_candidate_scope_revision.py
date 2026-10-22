"""An explicit revision narrows validation while preserving all proposal evidence."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.wildclawbench.operations import unattended
from scripts.wildclawbench.protocol import canonical_hash, write_json


class CandidateScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "nights/01"
        self.path = self.directory / "candidates.json"
        self.ids = [f"task-{i:02d}" for i in range(24)]
        self.selected = [self.ids[3], self.ids[7]]
        self.candidate = {"name": "video", "candidate_id": "candidate", "source_task_ids": self.ids,
            "source_session_ids": [f"session-{i:02d}" for i in range(24)],
            "source_outcomes": [{"task_id": task, "session_id": f"session-{i:02d}", "outcome": {"score": i / 24}}
                                for i, task in enumerate(self.ids)],
            "rationale": {"source_task_ids": self.selected}, "bundle_dir": "fixed-bundle",
            "bundle_sha256": "bundle-hash", "source_bank_sha256": "parent-bank",
            "native_tree_sha256": "native-tree", "base_bundle_sha256": "base-bundle"}
        self.frozen = {"tasks": [{"task_id": task} for task in self.ids + ["outside-group"]], "config": {}}
        write_json(self.root / "frozen-protocol.json", self.frozen)
        self.revision = {"policy": unattended.SCOPE_POLICY, "authorized_at": "2026-10-13T23:00:00+00:00",
                         "authorization_text": "Use explicit candidate sources", "applies_from_round": 1,
                         "frozen_protocol_sha256": canonical_hash(self.frozen)}
        write_json(self.root / "operations/validation-scope-revision.json", self.revision)
        write_json(self.path, [self.candidate])
        self.original = self.path.read_bytes()
        self.policy = unattended.Policy(self.root, self.revision["authorized_at"])

    def tearDown(self):
        self.temporary.cleanup()

    def test_cached_24_to_2_preserves_evidence_and_hashes_and_backs_up_exact_bytes(self):
        self.policy.normalize_cached_candidates(1)
        candidate = json.loads(self.path.read_text())[0]
        self.assertEqual(candidate["source_task_ids"], self.selected)
        self.assertEqual(candidate["source_session_ids"], ["session-03", "session-07"])
        self.assertEqual([o["task_id"] for o in candidate["source_outcomes"]], self.selected)
        for field in ("source_task_ids", "source_session_ids", "source_outcomes"):
            self.assertEqual(candidate["evidence_" + field], self.candidate[field])
        for field in ("bundle_dir", "bundle_sha256", "source_bank_sha256", "native_tree_sha256", "base_bundle_sha256"):
            self.assertEqual(candidate[field], self.candidate[field])
        digest = hashlib.sha256(self.original).hexdigest()
        audit = self.policy.operations / "validation-scope"
        self.assertEqual((audit / "original-candidates" / (digest + ".json")).read_bytes(), self.original)
        record = json.loads((audit / f"night-01-{digest}.json").read_text())
        self.assertEqual(record["revision"], self.revision)
        self.assertEqual(record["changes"][0]["old_source_task_ids"], self.ids)
        self.assertEqual(record["changes"][0]["new_source_task_ids"], self.selected)
        normalized = self.path.read_bytes()
        self.policy.normalize_cached_candidates(1)
        self.assertEqual(self.path.read_bytes(), normalized)
        self.assertEqual(len(list(audit.glob("night-*.json"))), 1)

    def test_future_proposal_is_normalized_only_when_revision_is_active(self):
        bank = self.root / "banks/S0"
        bank.mkdir(parents=True)
        self.policy.propose_native = Mock(return_value=[copy.deepcopy(self.candidate)])
        result = self.policy.propose(self.directory / "proposals", bank, [], {})
        self.assertEqual(result[0]["source_task_ids"], self.selected)
        self.assertEqual(len(result[0]["evidence_source_task_ids"]), 24)
        revision_path = self.root / "operations/validation-scope-revision.json"
        revision_path.unlink()
        disabled = unattended.Policy(self.root, "original-authorization")
        disabled.propose_native = Mock(return_value=[self.candidate])
        self.assertEqual(disabled.propose(self.directory / "proposals", bank, [], {}), [self.candidate])
        write_json(revision_path, {**self.revision, "policy": "unrelated-policy"})
        self.assertIsNone(unattended.Policy(self.root, "original-authorization").scope_revision)

    def test_illegal_empty_or_unmappable_reference_cannot_rewrite_cached_candidates(self):
        for selected in ([], ["missing-task"], ["outside-group"], [3]):
            with self.subTest(selected=selected):
                candidate = {**self.candidate, "rationale": {"source_task_ids": selected}}
                write_json(self.path, [candidate])
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "nonempty real task IDs"):
                    self.policy.normalize_cached_candidates(1)
                self.assertEqual(self.path.read_bytes(), before)
        malformed = {**self.candidate, "source_session_ids": []}
        with self.assertRaisesRegex(ValueError, "source sessions"):
            self.policy.normalize_candidates([malformed], self.directory)
        self.policy.scope_revision["frozen_protocol_sha256"] = "different-protocol"
        with self.assertRaisesRegex(ValueError, "different frozen protocol"):
            self.policy.normalize_candidates([self.candidate], self.directory)
        self.assertFalse((self.policy.operations / "validation-scope").exists())

    def test_existing_decision_requires_same_validation_sources(self):
        decision_path = self.directory / "validation/001-video/decision.json"
        decision = {"source_task_ids": self.ids, "accepted": True, "parent_sha256": "parent-bank"}
        write_json(decision_path, decision)
        before = decision_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "different validation source scope"):
            self.policy.normalize_cached_candidates(1)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(decision_path.read_bytes(), before)
        write_json(decision_path, {**decision, "source_task_ids": self.selected})
        before = decision_path.read_bytes()
        self.policy.normalize_cached_candidates(1)
        self.assertEqual(json.loads(self.path.read_text())[0]["source_task_ids"], self.selected)
        self.assertEqual(decision_path.read_bytes(), before)

    def enable_v2(self):
        self.revision.update(selection_version=2, restrict_to_task_ids_mentioned_in_reason=True,
                             reason_source_selection_applies_from_round=2)
        write_json(self.root / "operations/validation-scope-revision.json", self.revision)
        self.policy = unattended.Policy(self.root, self.revision["authorized_at"])

    def test_v2_reason_restricts_24_declared_tasks_to_five_and_preserves_rationale(self):
        self.enable_v2()
        selected = self.ids[:5]
        candidate = {**self.candidate, "rationale": {"source_task_ids": self.ids, "reason": ", ".join(selected)}}
        directory = self.root / "nights/02"
        result = self.policy.normalize_candidates([candidate], directory)[0]
        self.assertEqual(result["source_task_ids"], selected)
        self.assertEqual(result["rationale"], candidate["rationale"])
        self.assertEqual(result["evidence_source_task_ids"], self.ids)
        self.assertEqual(len(result["source_outcomes"]), 5)
        self.assertEqual(result["validation_scope_revision_version"], 2)
        audit = json.loads(next((self.policy.operations / "validation-scope").glob("night-02-*.json")).read_text())
        self.assertEqual(audit["revision_version"], 2)
        self.assertEqual(audit["changes"][0]["original_declared_source_task_ids"], self.ids)
        self.assertEqual(audit["changes"][0]["new_source_task_ids"], selected)

    def test_v2_matches_complete_ids_and_refuses_absent_reason_evidence(self):
        self.enable_v2()
        ids = ["category_task_1", "category_task_10", "category_task_100"]
        frozen = {"tasks": [{"task_id": task} for task in ids], "config": {}}
        write_json(self.root / "frozen-protocol.json", frozen)
        self.policy.scope_revision["frozen_protocol_sha256"] = canonical_hash(frozen)
        candidate = {"name": "skill", "source_task_ids": ids, "rationale": {
                     "source_task_ids": ids, "reason": "Only category_task_10 directly supports this."}}
        result = self.policy.normalize_candidates([candidate], self.root / "nights/02")[0]
        self.assertEqual(result["source_task_ids"], ["category_task_10"])
        candidate["rationale"]["reason"] = "category_task_100_suffix is not a complete task ID"
        with self.assertRaisesRegex(ValueError, "no complete declared task IDs"):
            self.policy.normalize_candidates([candidate], self.root / "nights/02")
        candidate["rationale"]["reason"] = "No task IDs in this reason"
        with self.assertRaisesRegex(ValueError, "scope refused"):
            self.policy.normalize_candidates([candidate], self.root / "nights/02")

    def test_v2_leaves_night1_scope_and_v1_unchanged_and_patches_future_prompt(self):
        self.enable_v2()
        candidate = {**self.candidate, "rationale": {"source_task_ids": self.ids, "reason": self.ids[0]}}
        result = self.policy.normalize_candidates([candidate], self.directory)[0]
        self.assertEqual(result["source_task_ids"], self.ids)
        self.assertFalse(result["reason_source_restriction_applied"])
        evolution = SimpleNamespace(propose_candidates=Mock(), _prompt=Mock(return_value="original prompt"))
        with self.policy.install(evolution):
            self.policy.proposal_round = 1
            self.assertEqual(evolution._prompt("group", self.ids), "original prompt")
            self.policy.proposal_round = 2
            self.assertIn("Do not copy the entire evidence group", evolution._prompt("group", self.ids))
        self.assertEqual(evolution._prompt("group", self.ids), "original prompt")
        self.policy.scope_revision.pop("selection_version")
        result = self.policy.normalize_candidates([candidate], self.root / "nights/02")[0]
        self.assertEqual(result["source_task_ids"], self.ids)
        singleton = {**self.candidate, "rationale": {"source_task_ids": [self.ids[0]], "reason": "No task IDs"}}
        self.policy.scope_revision["selection_version"] = 2
        result = self.policy.normalize_candidates([singleton], self.root / "nights/02")[0]
        self.assertEqual(result["source_task_ids"], [self.ids[0]])
        self.assertFalse(result["reason_source_restriction_applied"])


if __name__ == "__main__":
    unittest.main()
