"""Malformed declared evidence is quarantined before native IDs exist, never guessed or graded."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.wildclawbench import run
from . import test_unattended as fixtures
from scripts.wildclawbench.operations import unattended
from scripts.wildclawbench.protocol import canonical_hash, tree_hash, write_json


class SourceQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "nights/03"
        frozen = {"tasks": [{"task_id": "task-1"}, {"task_id": "task-2"}], "config": {}}
        write_json(self.root / "frozen-protocol.json", frozen)
        revision = {"policy": unattended.SCOPE_POLICY, "frozen_protocol_sha256": canonical_hash(frozen),
                    "reject_invalid_source_candidates": True, "invalid_source_quarantine_applies_from_round": 3}
        write_json(self.root / "operations/validation-scope-revision.json", revision)
        self.policy = unattended.Policy(self.root, "2026-10-14T05:00:00+00:00")
        self.valid = {"name": "valid", "candidate_id": "good-id", "source_task_ids": ["task-1"],
                      "rationale": {"source_task_ids": ["task-1"]}, "bundle_sha256": "good-bundle"}
        self.bad = {"name": "github", "candidate_id": "bad-id", "source_task_ids": ["task-1", "task-2"],
                    "rationale": None, "bundle_sha256": "bad-bundle", "bundle_dir": "/unchanged/original"}

    def tearDown(self):
        self.temporary.cleanup()

    def test_raw_preservation_skip_without_score_and_replay_are_durable(self):
        raw = json.dumps([self.bad, self.valid], separators=(",", ":")).encode()
        with patch.object(run, "event"):
            valid = self.policy.normalize_or_quarantine([self.bad, self.valid], self.directory, raw)
        self.assertEqual([c["name"] for c in valid], ["valid"])
        self.assertEqual(valid[0]["source_task_ids"], ["task-1"])
        digest = hashlib.sha256(raw).hexdigest()
        archive = self.policy.operations / "source-quarantine" / (digest + ".json")
        self.assertEqual(archive.read_bytes(), raw)
        rejection = next((self.policy.operations / "source-quarantine").glob("source-invalid-*.json"))
        data = json.loads(rejection.read_text())
        self.assertEqual(data["original_candidate"], self.bad)
        self.assertEqual(data["original_ordinal"], 1)
        self.assertIsNone(data["score"])
        self.assertFalse(data["validated"])
        self.assertFalse(data["accepted"])
        self.assertEqual(json.loads(Path(data["raw_candidate"]).read_text()), self.bad)
        self.assertEqual(hashlib.sha256(Path(data["raw_candidate"]).read_bytes()).hexdigest(),
                         data["raw_candidate_sha256"])
        skip = next((self.policy.operations / "skips").glob("*.json"))
        self.assertFalse(json.loads(skip.read_text())["score_available"])
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (archive, rejection, skip)}
        with patch.object(run, "event"):
            self.assertEqual(self.policy.normalize_or_quarantine([self.bad, self.valid], self.directory, raw), valid)
        for path, snapshot in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot)

    def test_strict_without_flag_and_validation_or_integrity_errors_are_fatal(self):
        with self.assertRaises(unattended.InvalidCandidateSources):
            self.policy.normalize_candidates([self.bad], self.directory)
        self.policy.scope_revision["reject_invalid_source_candidates"] = False
        with self.assertRaises(unattended.InvalidCandidateSources):
            self.policy.normalize_or_quarantine([self.bad], self.directory)
        self.policy.scope_revision["reject_invalid_source_candidates"] = True
        with self.assertRaises(unattended.InvalidCandidateSources):
            self.policy.normalize_or_quarantine([self.bad], self.root / "nights/02")
        artifact = self.root / "executions/validation/03/001-valid/old/05.01/config.json"
        write_json(artifact, {"started": True})
        with self.assertRaisesRegex(ValueError, "ordinal identities"):
            self.policy.normalize_or_quarantine([self.bad, self.valid], self.directory)
        shutil.rmtree(self.root / "executions")
        with self.assertRaisesRegex(ValueError, "Unsafe candidate"):
            self.policy.normalize_or_quarantine([{**self.valid, "name": "../escape"}], self.directory)
        write_json(self.directory / "validation/001-valid/decision.json", {"source_task_ids": ["task-2"]})
        with self.assertRaisesRegex(ValueError, "different validation source scope"):
            self.policy.normalize_or_quarantine([self.valid], self.directory)
        self.policy.scope_revision["frozen_protocol_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "different frozen protocol"):
            self.policy.normalize_or_quarantine([self.bad], self.directory)
        self.assertFalse((self.policy.operations / "skips").exists())

    def test_other_native_candidate_and_appended_operational_candidate_run_real_native_ab(self):
        fixture = fixtures.UnattendedControllerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        frozen = json.loads((fixture.directory / "frozen-protocol.json").read_text())
        write_json(fixture.directory / "operations/validation-scope-revision.json", {
            "policy": unattended.SCOPE_POLICY, "frozen_protocol_sha256": canonical_hash(frozen),
            "reject_invalid_source_candidates": True, "invalid_source_quarantine_applies_from_round": 3})

        def propose(*args):
            valid = fixture.propose(*args)[0]
            valid["candidate_id"] = "native-good"
            valid["rationale"] = {"source_task_ids": valid["source_task_ids"]}
            bad = {**valid, "name": "github", "candidate_id": "native-bad", "rationale": None}
            return [bad, valid]

        def append_operational(candidates, directory, bank):
            bundle = fixture.directory / "operations/video-bundle"
            shutil.copytree(Path(candidates[0]["bundle_dir"]), bundle)
            return candidates + [{**candidates[0], "name": "ops-video", "action": "create",
                                  "bundle_dir": str(bundle), "bundle_sha256": tree_hash(bundle)}]

        with fixture.native_mocks(propose=propose) as (adapter, evolution):
            policy = unattended.Policy(fixture.directory, "2026-10-14T05:00:00+00:00")
            policy.metadata_repair.apply = Mock(side_effect=append_operational)
            with policy.install(evolution):
                published = run.night(3, fixture.bank, [], fixture.tasks, fixture.directory, fixture.config)
            self.assertTrue(published.is_dir())
            self.assertEqual(adapter.execute_task.call_count, 4)
            decisions = list((fixture.directory / "nights/03/validation").glob("*/decision.json"))
            self.assertEqual({p.parent.name for p in decisions}, {"001-skill", "002-ops-video"})
            self.assertEqual(len(list((fixture.directory / "operations/unattended/skips").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
