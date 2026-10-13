import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "evolution.py"
spec = importlib.util.spec_from_file_location("experiment_evolution", MODULE)
e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e)


class FakeClient:
    def __init__(self, **kwargs):
        pass

    async def chat(self, messages, **kwargs):
        return (
            "The observed real tool result identifies a missing procedural step, "
            "while successful cases provide invariants to preserve."
        )


e.AsyncLLMClient = FakeClient


class EvolutionTests(unittest.TestCase):
    def test_external_judge_budget_keeps_evolution_inference_on_loopback(self):
        config = {"llm_model": "qwen3.8-27b", "llm_base_url": "http://127.0.0.1:19182/v1",
                  "paid_api_budget_usd": 10, "external_judge": True}
        self.assertEqual(e._configuration(config)["llm_base_url"], config["llm_base_url"])
        with self.assertRaises(e.EvolutionError):
            e._configuration({**config, "external_judge": False})
        with self.assertRaises(e.EvolutionError):
            e._configuration({**config, "llm_base_url": "https://api.deepseek.com/v1"})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.bank = self.root / "bank"
        for name in ("alpha", "beta"):
            folder = self.bank / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Original\n---\nOriginal procedure.\n")
            (folder / "scripts").mkdir()
            script = folder / "scripts/helper.sh"
            script.write_bytes(b"#!/bin/sh\nprintf original\n")
            script.chmod(0o755)
            (folder / "assets").mkdir()
            (folder / "assets/input.bin").write_bytes(bytes(range(256)))
        self.before = e._tree_hash(self.bank)
        self.calls = 0
        self.config = dict(
            llm_model="test-local",
            llm_base_url="http://127.0.0.1:18182/v1",
            evolution_runner=self.run_agent,
            llm_max_tokens=64,
            llm_temperature=1,
            paid_api_budget_usd=0,
        )
        self.mode = "normal"

    def tearDown(self):
        self.temp.cleanup()

    def session(self, task="task-a", sid="user/one", skill="alpha"):
        return dict(
            task_id=task,
            session_id=sid,
            phase="day",
            user_id="user-01",
            skill_bank_sha256=self.before,
            outcome=dict(score=0.5, success=False, timed_out=False, tool_errors=1),
            turns=[
                dict(
                    prompt_text="Perform the actual task",
                    response_text="Observed output",
                    read_skills=[dict(skill_name=skill)] if skill else [],
                    tool_errors=True,
                )
            ],
        )

    def run_agent(self, workspace, home, message, config, sid):
        self.calls += 1
        guide = (workspace / "EVOLVE_AGENTS.md").read_text()
        tail = guide.split("# Independent experimental proposal scope\n\n")[1]
        scope, _ = json.JSONDecoder().raw_decode(tail)
        name = scope["allowed_existing_skill_edit"] or "new-procedure"
        folder = workspace / "skills" / name
        if self.mode == "wrong_scope":
            name = "beta"
            folder = workspace / "skills" / name
        folder.mkdir(exist_ok=True)
        if self.mode == "history_only":
            (folder / "history").mkdir()
            (folder / "history/v1_evidence.md").write_text("No procedural change.")
        elif self.mode == "script_only":
            (folder / "scripts/helper.sh").write_bytes(b"#!/bin/sh\nprintf corrected\n")
        else:
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Improved\n---\nCheck observed error before finishing.\n"
            )
        if self.mode == "delete_asset":
            (folder / "assets/input.bin").unlink()
        if self.mode == "failed_process":
            return subprocess.CompletedProcess([], 9, "partial edit", "process failure")
        return subprocess.CompletedProcess([], 0, json.dumps(dict(status="ok")), "")

    def test_complete_bundle_sources_and_cache(self):
        sessions = [self.session(), self.session("task-b", "user/two")]
        result = e.propose_candidates(self.root / "round", self.bank, sessions, self.config)
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(item["action"], "refine")
        self.assertEqual(item["source_task_ids"], ["task-a", "task-b"])
        self.assertEqual(item["source_session_ids"], ["user/one", "user/two"])
        exported = pathlib.Path(item["bundle_dir"])
        self.assertEqual((exported / "assets/input.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((exported / "scripts/helper.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual(e._tree_hash(exported), item["bundle_sha256"])
        self.assertEqual(e._tree_hash(self.bank), self.before)
        self.assertEqual(result, e.propose_candidates(self.root / "round", self.bank, sessions, self.config))
        self.assertEqual(self.calls, 1)
        (exported / "SKILL.md").write_text("tampered")
        with self.assertRaises(e.EvolutionError):
            e.propose_candidates(self.root / "round", self.bank, sessions, self.config)

    def test_no_skill_group_creates_independent_candidate(self):
        result = e.propose_candidates(self.root / "round", self.bank, [self.session(skill=None)], self.config)
        self.assertEqual(result[0]["action"], "create")
        self.assertIsNone(result[0]["base_bundle_sha256"])
        self.assertEqual(result[0]["source_task_ids"], ["task-a"])
        self.assertEqual(e._tree_hash(self.bank), self.before)

    def test_wrong_scope_is_rejected(self):
        self.mode = "wrong_scope"
        with self.assertRaises(e.EvolutionError):
            e.propose_candidates(self.root / "round", self.bank, [self.session()], self.config)
        summary = json.loads((self.root / "round/evolution/proposal-summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(e._tree_hash(self.bank), self.before)

    def test_failed_process_never_exports_partial_candidate(self):
        self.mode = "failed_process"
        with self.assertRaises(e.EvolutionError):
            e.propose_candidates(self.root / "round", self.bank, [self.session()], self.config)
        self.assertEqual(list((self.root / "round").rglob("candidate.json")), [])
        self.assertEqual(e._tree_hash(self.bank), self.before)

    def test_support_file_deletion_is_rejected(self):
        self.mode = "delete_asset"
        with self.assertRaises(e.EvolutionError):
            e.propose_candidates(self.root / "round", self.bank, [self.session()], self.config)

    def test_history_only_is_not_a_candidate(self):
        self.mode = "history_only"
        self.assertEqual(e.propose_candidates(self.root / "round", self.bank, [self.session()], self.config), [])

    def test_supporting_script_change_is_a_complete_candidate(self):
        self.mode = "script_only"
        result = e.propose_candidates(self.root / "round", self.bank, [self.session()], self.config)
        exported = pathlib.Path(result[0]["bundle_dir"])
        self.assertEqual((exported / "SKILL.md").read_bytes(), (self.bank / "alpha/SKILL.md").read_bytes())
        self.assertEqual((exported / "scripts/helper.sh").read_bytes(), b"#!/bin/sh\nprintf corrected\n")
        self.assertEqual([item["path"] for item in result[0]["changed_files"]], ["scripts/helper.sh"])
        self.assertEqual((exported / "scripts/helper.sh").stat().st_mode & 0o777, 0o755)

    def test_real_messages_convert_only_successful_reads(self):
        messages = [
            dict(message=dict(role="user", content="Task")),
            dict(
                message=dict(
                    role="assistant",
                    content=[
                        dict(
                            type="toolCall", id="read1", name="read", arguments=dict(path="/root/skills/alpha/SKILL.md")
                        )
                    ],
                )
            ),
            dict(
                message=dict(
                    role="toolResult", toolCallId="read1", content=[dict(type="text", text="Original procedure.")]
                )
            ),
        ]
        normalized = e._normalize_session(
            dict(task_id="task-a", session_id="s", phase="day", messages=messages), {"alpha"}
        )
        self.assertEqual(normalized["_skills_referenced"], ["alpha"])
        messages[-1]["message"]["isError"] = True
        normalized = e._normalize_session(
            dict(task_id="task-a", session_id="s", phase="day", messages=messages), {"alpha"}
        )
        self.assertEqual(normalized["_skills_referenced"], [])

    def test_environment_helpers_do_not_become_refine_groups(self):
        session = self.session()
        turn = session["turns"][0]
        turn["read_skills"] += [
            dict(skill_name="skill-creator"),
            dict(skill_name="beta", path="/usr/lib/node_modules/openclaw/skills/beta/SKILL.md"),
        ]
        turn["modified_skills"] = [dict(skill_name="frozen-helper")]
        turn["injected_skills"] = ["alpha", "skill-creator"]
        normalized = e._normalize_session(session, {"alpha", "beta"})
        self.assertEqual(normalized["_skills_referenced"], ["alpha"])
        self.assertEqual(len(normalized["turns"][0]["source_read_skills"]), 3)
        self.assertEqual(normalized["turns"][0]["source_modified_skills"], [dict(skill_name="frozen-helper")])
        self.assertEqual(session["turns"][0]["modified_skills"], [dict(skill_name="frozen-helper")])
        result = e.propose_candidates(self.root / "round", self.bank, [session], self.config)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_skill_name"], "alpha")
        self.assertEqual(self.calls, 1)

    def test_same_name_bundled_read_is_retained_as_evidence_but_not_bank_reference(self):
        messages = [
            dict(message=dict(role="user", content="Task")),
            dict(
                message=dict(
                    role="assistant",
                    content=[
                        dict(
                            type="toolCall",
                            id="r",
                            name="read",
                            arguments=dict(path="/usr/lib/node_modules/openclaw/skills/alpha/SKILL.md"),
                        )
                    ],
                )
            ),
            dict(
                message=dict(
                    role="toolResult", toolCallId="r", content=[dict(type="text", text="Image helper procedure")]
                )
            ),
        ]
        normalized = e._normalize_session(
            dict(task_id="task-a", session_id="s", phase="day", messages=messages), {"alpha"}
        )
        self.assertEqual(normalized["_skills_referenced"], [])
        provenance = normalized["turns"][0]["actual_skill_reads"]
        self.assertEqual(provenance[0]["name"], "alpha")
        self.assertEqual(provenance[0]["environment_or_shared"], "environment")
        self.assertIn(
            "/usr/lib/node_modules/openclaw/skills/alpha/SKILL.md",
            normalized["turns"][0]["tool_calls"][0]["function"]["arguments"],
        )
        self.assertEqual(normalized["turns"][0]["tool_results"][0]["content"], "Image helper procedure")

    def test_shared_bank_names_are_dynamic_and_path_traversal_is_rejected(self):
        self.assertEqual(e._bank_source_name("/root/skills/newly-created/SKILL.md", {"newly-created"}), "newly-created")
        self.assertIsNone(e._bank_source_name("/root/skills/../alpha/SKILL.md", {"alpha"}))

    def test_successful_tilde_and_parent_alias_reads_match_only_normalized_bank_paths(self):
        for path in ("~/skills/alpha/SKILL.md", "/root/skills/beta/../alpha/SKILL.md"):
            with self.subTest(path=path):
                messages = [
                    dict(message=dict(role="user", content="Task")),
                    dict(
                        message=dict(
                            role="assistant",
                            content=[dict(type="toolCall", id="r", name="read", arguments=dict(path=path))],
                        )
                    ),
                    dict(
                        message=dict(
                            role="toolResult",
                            toolCallId="r",
                            content=[dict(type="text", text="actual skill contents")],
                            isError=False,
                        )
                    ),
                ]
                normalized = e._normalize_session(
                    dict(task_id="task-a", session_id="s", phase="day", messages=messages), {"alpha", "beta"}
                )
                self.assertEqual(normalized["_skills_referenced"], ["alpha"])
                read = normalized["turns"][0]["actual_skill_reads"][0]
                self.assertEqual(read["path"], "/root/skills/alpha/SKILL.md")
                self.assertEqual(read["original_path"], path)
                self.assertEqual(read["environment_or_shared"], "shared")
                self.assertIn(path, normalized["turns"][0]["tool_calls"][0]["function"]["arguments"])
                messages[-1]["message"]["isError"] = True
                failed = e._normalize_session(
                    dict(task_id="task-a", session_id="s", phase="day", messages=messages), {"alpha", "beta"}
                )
                self.assertEqual(failed["_skills_referenced"], [])
        self.assertIsNone(
            e._bank_source_name("~/skills/../../usr/lib/node_modules/openclaw/skills/alpha/SKILL.md", {"alpha"})
        )

    def test_native_modified_and_injected_aliases_preserve_original_metadata(self):
        session = self.session(skill=None)
        turn = session["turns"][0]
        turn["modified_skills"] = [dict(skill_name="alpha", path="~/skills/beta/../alpha/SKILL.md")]
        turn["injected_skills"] = ["~/skills/alpha/SKILL.md", "/usr/lib/node_modules/openclaw/skills/beta/SKILL.md"]
        normalized = e._normalize_session(session, {"alpha", "beta"})
        self.assertEqual(normalized["_skills_referenced"], ["alpha"])
        self.assertEqual(normalized["turns"][0]["modified_skills"][0]["path"], "/root/skills/alpha/SKILL.md")
        self.assertEqual(
            normalized["turns"][0]["modified_skills"][0]["original_path"], "~/skills/beta/../alpha/SKILL.md"
        )
        self.assertEqual(normalized["turns"][0]["injected_skills"], ["alpha"])
        self.assertEqual(normalized["turns"][0]["source_injected_skills"], session["turns"][0]["injected_skills"])

    def test_external_inference_url_and_validation_logs_are_rejected(self):
        config = dict(self.config, llm_base_url="https://api.openai.com/v1")
        with self.assertRaises(e.EvolutionError):
            e.propose_candidates(self.root / "round", self.bank, [self.session()], config)
        for phase in ("validation-old", "pilot", "endpoint", "terminal", None):
            with self.subTest(phase=phase), self.assertRaises(e.EvolutionError):
                e.propose_candidates(self.root / "round", self.bank, [dict(self.session(), phase=phase)], self.config)

    def test_changed_model_and_sources_invalidate_proposal_cache(self):
        e.propose_candidates(self.root / "round", self.bank, [self.session()], self.config)
        config = dict(self.config, llm_model="different-local-model")
        e.propose_candidates(self.root / "round", self.bank, [self.session()], config)
        self.assertEqual(self.calls, 2)
        session = self.session()
        session["outcome"]["score"] = 0.75
        e.propose_candidates(self.root / "round", self.bank, [session], config)
        self.assertEqual(self.calls, 3)


if __name__ == "__main__":
    unittest.main()
