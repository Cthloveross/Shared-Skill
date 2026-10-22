"""A staged transcript contract is explicit, body-changing and limited to operational video candidates."""

import json
import shutil
import unittest
from pathlib import Path

from . import test_metadata_repair as fixtures
from scripts.wildclawbench.operations import metadata_repair, unattended
from scripts.wildclawbench.protocol import canonical_hash, tree_hash, write_json

from sharedskill.skill_bundle import read_skill_bundle_with_meta

TEXT = ("Export transcript text as plain recognized speech only in the requested language. "
        "Do not include titles, timestamps, scene notes, Markdown, diagnostics or tool-output wrappers. "
        "Put timestamps and diagnostics in separate files. Do not fabricate, embellish or infer missing speech. "
        "Write only the speech transcript value; do not copy helper stdout.")


class TranscriptContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MetadataRepairTests()
        self.fixture.setUp()
        self.root, self.repair, self.bank = self.fixture.root, self.fixture.repair, self.fixture.bank
        self.directory = self.root / "nights/03"
        self.bundle = self.root / "original/video-transcribe"
        shutil.copytree(self.fixture.bundle, self.bundle)
        self.raw = self.fixture.raw.replace(b"name: video\n", b"name: video-transcribe\n")
        (self.bundle / "SKILL.md").write_bytes(self.raw)
        _, files, native_hash = read_skill_bundle_with_meta(self.bundle)
        self.candidate = {**self.fixture.candidate, "name": "video-transcribe", "action": "refine",
            "bundle_dir": str(self.bundle), "bundle_sha256": tree_hash(self.bundle), "files": files,
            "native_tree_sha256": native_hash, "evidence_group": "video-transcribe",
            "rationale": {"source_task_ids": ["task-1"], "reason": "Operational metadata quote for task-1"},
            "operational_origin": {"policy": metadata_repair.POLICY}}
        evidence = "audits/s2-video-repair-publication-integrity-20261014.json"
        write_json(self.root / evidence, {"deployment_gap": "speech transcript export contains presentation text"})
        self.repair.revision["transcript_export_contract"] = {
            "policy": metadata_repair.TRANSCRIPT_POLICY, "applies_from_round": 3,
            "skill": "video-transcribe", "text": TEXT, "source_evidence": evidence}

    def tearDown(self):
        self.fixture.tearDown()

    def test_metadata_then_body_revision_preserves_both_originals_and_tells_the_truth(self):
        result = self.repair.apply([self.candidate], self.directory, self.bank)[0]
        final = Path(result["bundle_dir"])
        record = result["transcript_export_contract"]
        quoted = Path(record["original_metadata_bundle_dir"])
        self.assertIn("operations/semantic-repair", str(final))
        self.assertIn("operations/metadata-repair", str(quoted))
        self.assertEqual((self.bundle / "SKILL.md").read_bytes(), self.raw)
        self.assertEqual((quoted / "SKILL.md").read_bytes().split(b"\n---", 1)[1],
                         self.raw.split(b"\n---", 1)[1])
        self.assertTrue((final / "SKILL.md").read_bytes().startswith((quoted / "SKILL.md").read_bytes()))
        self.assertIn(TEXT, (final / "SKILL.md").read_text())
        self.assertEqual((final / "asset.txt").read_bytes(), (self.bundle / "asset.txt").read_bytes())
        self.assertTrue(record["body_changed"])
        self.assertTrue(record["supporting_files_unchanged"])
        self.assertNotEqual(record["original_body_sha256"], record["revised_body_sha256"])
        self.assertFalse(result["metadata_repair"]["body_and_supporting_files_unchanged"])
        self.assertTrue(result["metadata_repair"]["body_changed"])
        self.assertEqual(result["operational_origin"]["prior_validation_evidence"],
                         record["contract"]["source_evidence"])
        self.assertEqual(result["engine"], "user-directed-operational-candidate-revision")
        self.assertEqual(result["bundle_sha256"], tree_hash(final))
        self.assertEqual(result["native_tree_sha256"], read_skill_bundle_with_meta(final)[2])
        metadata_audit = json.loads((quoted.parent / "repair.json").read_text())
        self.assertTrue(metadata_audit["repaired_candidate"]["metadata_repair"]["body_and_supporting_files_unchanged"])

    def test_resume_does_not_append_twice_and_scored_or_started_arm_cannot_change(self):
        result = self.repair.apply([self.candidate], self.directory, self.bank)
        before = (Path(result[0]["bundle_dir"]) / "SKILL.md").read_bytes()
        self.assertEqual(self.repair.apply(result, self.directory, self.bank), result)
        self.assertEqual(before.count(metadata_repair.TRANSCRIPT_POLICY.encode()), 1)
        quoted = self.repair.apply([self.candidate], self.root / "nights/02", self.bank)[0]
        decision = self.directory / "validation/001-video-transcribe/decision.json"
        write_json(decision, {"accepted": False})
        with self.assertRaisesRegex(ValueError, "after a decision"):
            self.repair.apply([quoted], self.directory, self.bank)
        decision.unlink()
        write_json(self.root / "executions/validation/03/001-video-transcribe/candidate/05.01/config.json",
                   {"started": True})
        with self.assertRaisesRegex(ValueError, "candidate-arm artifact"):
            self.repair.apply([quoted], self.directory, self.bank)

    def test_night2_normal_native_video_and_pdf_do_not_receive_operational_body_change(self):
        old = self.repair.apply([self.candidate], self.root / "nights/02", self.bank)[0]
        self.assertNotIn("transcript_export_contract", old)
        valid = metadata_repair.description_repair(self.raw, "video-transcribe")
        (self.bundle / "SKILL.md").write_bytes(valid)
        native = {**self.candidate, "action": "create", "bundle_sha256": tree_hash(self.bundle)}
        native.pop("operational_origin")
        self.assertEqual(self.repair.apply([native], self.directory, self.bank), [native])
        pdf_bundle = self.root / "original/arxiv-pdf-processing"
        shutil.copytree(self.fixture.bundle, pdf_bundle)
        (pdf_bundle / "SKILL.md").write_bytes(
            self.fixture.raw.replace(b"name: video\n", b"name: arxiv-pdf-processing\n"))
        pdf = {**self.fixture.candidate, "name": "arxiv-pdf-processing", "bundle_dir": str(pdf_bundle),
               "bundle_sha256": tree_hash(pdf_bundle), "operational_origin": {"policy": metadata_repair.POLICY}}
        result = self.repair.apply([pdf], self.directory, self.bank)[0]
        self.assertNotIn("transcript_export_contract", result)
        self.assertTrue(result["metadata_repair"]["body_and_supporting_files_unchanged"])
        self.assertNotIn(TEXT, self.repair.prompt_for_round(2))
        self.assertIn(TEXT, self.repair.prompt_for_round(3))
        self.repair.revision.pop("transcript_export_contract")
        self.assertNotIn(TEXT, self.repair.prompt_for_round(3))

    def test_policy_metadata_declares_user_directed_markdown_change(self):
        write_json(self.root / "operations/skill-metadata-revision.json", self.repair.revision)
        policy = unattended.Policy(self.root, "2026-10-14T04:00:00+00:00")
        self.assertIn("user-directed candidate Markdown transcript export contract change", policy.metadata["scope"])
        self.assertIn("frozen execution and acceptance gates unchanged", policy.metadata["scope"])

    def test_published_candidate_json_bytes_and_mtime_stay_unchanged(self):
        write_json(self.root / "operations/skill-metadata-revision.json", self.repair.revision)
        write_json(self.root / "operations/validation-scope-revision.json", {
            "policy": unattended.SCOPE_POLICY, "frozen_protocol_sha256": canonical_hash(self.fixture.frozen),
            "applies_from_round": 1, "selection_version": 2, "restrict_to_task_ids_mentioned_in_reason": True})
        directory = self.root / "nights/02"
        write_json(directory / "publication.json", {"round": 2})
        path = directory / "candidates.json"
        write_json(path, [self.candidate])
        before, stamp = path.read_bytes(), path.stat().st_mtime_ns
        policy = unattended.Policy(self.root, "2026-10-14T04:00:00+00:00")
        policy.normalize_cached_candidates(2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)


if __name__ == "__main__":
    unittest.main()
