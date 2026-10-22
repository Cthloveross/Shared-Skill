"""Catalog fidelity, bundle safety, and concurrent execution isolation (no model)."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.wildclawbench import alignment_catalog as catalog
from scripts.wildclawbench.protocol import tree_hash


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {
            "runtime_dir": str(self.root / "runtime"),
            "protocol_hash": "frozen-protocol",
            "alignment": {"version": "native-night-atomic-v1"},
        }
        self.bank = self.root / "bank"
        self.skill(self.bank)

    def tearDown(self):
        self.temp.cleanup()

    def skill(
        self, bank, name="alpha", description="A useful procedure.", body="Read the input before acting.\n", extra=""
    ):
        directory = bank / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {json.dumps(description)}\n{extra}---\n{body}"
        )
        return directory

    def binding(self, bank=None, run="wcb-example", execution="execution-a"):
        return catalog.prepare_binding(self.config, run, execution, bank or self.bank)

    def test_renderer_matches_pinned_official_goldens(self):
        # Derived by executing only the seven pure catalog methods from upstream
        # source SHA256 65c8a731...; tests neither fetch nor import that server.
        full, mode = catalog.render_catalog(
            [{"name": "alpha", "description": 'Use <tag> & "quotes" and apostrophe\'s.'}]
        )
        self.assertEqual(mode, "full")
        self.assertEqual(
            hashlib.sha256(full.encode()).hexdigest(),
            "bb83ea53014276c70d0c92967c393d00af5edf95a8b2c6d6717d3cfd84b274ae",
        )
        compact, mode = catalog.render_catalog(
            [{"name": f"skill-{i:02d}", "description": "x" * 1000} for i in range(40)]
        )
        self.assertEqual(mode, "compact")
        self.assertEqual(
            hashlib.sha256(compact.encode()).hexdigest(),
            "3d02b6b433741464789a364e95d214939452770d0e5d52c84a22d53a982a892c",
        )
        self.assertEqual(compact.count("    <location>"), 40)

    def test_binding_is_read_only_and_uses_exact_protocol_tree_hash(self):
        directory = self.bank / "alpha" / "scripts"
        directory.mkdir()
        script = directory / "work.py"
        script.write_bytes(b"print('checked')\n")
        script.chmod(0o755)
        before = {
            p.relative_to(self.bank).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in self.bank.rglob("*")
            if p.is_file()
        }
        first = self.binding()
        self.assertEqual(first["bank_sha256"], tree_hash(self.bank))
        self.assertEqual(first, self.binding())
        self.assertEqual(first, catalog.load_binding(self.config, "wcb-example", "execution-a"))
        after = {
            p.relative_to(self.bank).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in self.bank.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertFalse((self.bank / "skill_stats.json").exists())

    def test_concurrent_arms_and_identical_retries_do_not_cross_banks(self):
        other = self.root / "other"
        self.skill(other, description="A different arm.")

        def prepare(index):
            return self.binding(
                self.bank if index % 2 == 0 else other, execution="old" if index % 2 == 0 else "candidate"
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            bindings = list(pool.map(prepare, range(20)))
        self.assertEqual(len({b["bank_sha256"] for b in bindings}), 2)
        for execution, bank in (("old", self.bank), ("candidate", other)):
            self.assertEqual(
                catalog.load_binding(self.config, "wcb-example", execution)["bank_sha256"], tree_hash(bank)
            )
        with self.assertRaises(catalog.CatalogError):
            self.binding(other, execution="old")

    def test_changed_content_mode_or_new_support_file_invalidates_binding(self):
        for mutation in ("content", "mode", "extra"):
            with self.subTest(mutation=mutation):
                bank = self.root / mutation
                directory = self.skill(bank)
                self.binding(bank, execution=mutation)
                if mutation == "content":
                    with (directory / "SKILL.md").open("a") as stream:
                        stream.write("Changed procedure.\n")
                elif mutation == "mode":
                    (directory / "SKILL.md").chmod(0o600)
                else:
                    (directory / "residual.txt").write_text("old image residue")
                with self.assertRaises(catalog.CatalogError):
                    catalog.load_binding(self.config, "wcb-example", mutation)

    def test_protocol_policy_and_catalog_tampering_are_rejected(self):
        binding = self.binding()
        for config in (
            {**self.config, "protocol_hash": "different"},
            {**self.config, "alignment": {"version": "different"}},
        ):
            with self.assertRaises(catalog.CatalogError):
                catalog.load_binding(config, "wcb-example", "execution-a")
        path = Path(self.config["runtime_dir"]) / "execution-bindings/wcb-example/execution-a.json"
        path.chmod(0o644)
        binding["catalog_text"] += "changed"
        path.write_text(json.dumps(binding))
        with self.assertRaises(catalog.CatalogError):
            catalog.load_binding(self.config, "wcb-example", "execution-a")

    def test_invalid_yaml_duplicate_keys_names_and_types_fail_closed(self):
        path = self.bank / "alpha" / "SKILL.md"
        for frontmatter in (
            "name: alpha\ndescription: invalid: colon",
            "name: alpha\nname: alpha\ndescription: valid",
            "name: ../escape\ndescription: valid",
            "name: alpha\ndescription: 42",
            "name: alpha\ndescription: valid\ndisable-model-invocation: 'false'",
        ):
            with self.subTest(frontmatter=frontmatter):
                path.write_text("---\n" + frontmatter + "\n---\nBody\n")
                with self.assertRaises(catalog.CatalogError):
                    self.binding()

    def test_versioned_directory_keeps_display_name_and_actual_read_path(self):
        directory = self.skill(self.bank, name="self-improving-agent-3.0.5")
        path = directory / "SKILL.md"
        path.write_text(path.read_text().replace("name: self-improving-agent-3.0.5", "name: self-improving-agent"))
        binding = self.binding()
        skill = next(s for s in binding["skills"] if s["directory"] == directory.name)
        self.assertTrue(skill["name_mismatch"])
        self.assertEqual(skill["name"], "self-improving-agent")
        self.assertIn("<name>self-improving-agent</name>", binding["catalog_text"])
        self.assertIn("/root/skills/self-improving-agent-3.0.5/SKILL.md", binding["catalog_text"])

    def test_local_resources_must_exist_and_remain_inside_bundle(self):
        directory = self.skill(self.bank, body="Run `scripts/work.py`. See [details](references/check.md#step).\n")
        with self.assertRaisesRegex(catalog.CatalogError, "Missing bundled resource"):
            catalog.inspect_bank(self.bank)
        (directory / "scripts").mkdir()
        (directory / "scripts/work.py").write_text("print('ok')\n")
        (directory / "references").mkdir()
        (directory / "references/check.md").write_text("# step\n")
        result = catalog.inspect_bank(self.bank)
        self.assertEqual(result["skills"][0]["resource_references"], ["references/check.md", "scripts/work.py"])
        for target in ("../secret", "%2e%2e/secret", "/etc/passwd", "file:///etc/passwd"):
            self.skill(self.bank, body=f"[details]({target})\n")
            with self.assertRaises(catalog.CatalogError):
                catalog.inspect_bank(self.bank)
        self.skill(self.bank, body="[external](https://example.com/guide) [section](#step)\n")
        catalog.inspect_bank(self.bank)

    def test_symlinks_special_entries_and_path_traversal_are_rejected(self):
        (self.bank / "alpha/link").symlink_to(self.root)
        with self.assertRaisesRegex(catalog.CatalogError, "Symlink"):
            self.binding()
        (self.bank / "alpha/link").unlink()
        alias = self.root / "bank-alias"
        alias.symlink_to(self.bank)
        with self.assertRaises(catalog.CatalogError):
            self.binding(alias)
        for identifier in ("../escape", "..", "/absolute", "a/b", "a\\b"):
            with self.assertRaises(catalog.CatalogError):
                self.binding(run=identifier)
        os.mkfifo(self.bank / "alpha/pipe")
        with self.assertRaises(catalog.CatalogError):
            self.binding()

    def test_task_output_links_are_unresolved_not_missing_bundled_resources(self):
        directory = self.skill(self.bank, body="Deliver [report](output/report.md) and see [contract](FORMAT.md).\n")
        (directory / "FORMAT.md").write_text("Expected task output format.\n")
        binding = self.binding()
        skill = binding["skills"][0]
        self.assertEqual(skill["resource_references"], ["FORMAT.md"])
        self.assertEqual(skill["unresolved_resource_references"], ["output/report.md"])
        self.assertFalse((directory / "output").exists())
        for target in ("scripts/missing.py", "references/missing.md", "assets/missing.bin", "templates/missing.html"):
            with self.subTest(target=target):
                self.skill(self.bank, body=f"Task output [report](output/report.md); bundled [helper]({target}).\n")
                with self.assertRaisesRegex(catalog.CatalogError, "Missing bundled resource"):
                    catalog.inspect_bank(self.bank)

    def test_disabled_skills_are_manifested_but_not_injected(self):
        self.skill(self.bank, name="disabled", extra="disable-model-invocation: true\n")
        binding = self.binding()
        self.assertEqual(len(binding["skills"]), 2)
        self.assertNotIn("<name>disabled</name>", binding["catalog_text"])

    def test_native_builtin_catalog_and_tool_records_are_preserved(self):
        self.skill(self.bank, name="video-frames")
        binding = self.binding()
        native = ("<available_skills><skill><name>video-frames</name><location>"
                  "/usr/lib/node_modules/openclaw/skills/video-frames/SKILL.md"
                  "</location></skill></available_skills>")
        original = [
            {"role": "system", "content": native},
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "function": {"name": "read", "arguments": '{"path":"/root/skills/alpha/SKILL.md"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read-1", "content": "exact tool bytes"},
        ]
        before = copy.deepcopy(original)
        output = catalog.inject_catalog(original, binding)
        self.assertEqual(original, before)
        self.assertTrue(output[0]["content"].startswith(native))
        self.assertIn("/root/skills/video-frames/SKILL.md", output[0]["content"])
        self.assertEqual(output[1:], original[1:])
        self.assertEqual(catalog.inject_catalog(output, binding), output)
        wrong = {**binding, "catalog_text": binding["catalog_text"] + "changed"}
        with self.assertRaises(catalog.CatalogError):
            catalog.inject_catalog(output, wrong)

    def test_no_system_and_structured_system_are_idempotent(self):
        binding = self.binding()
        for messages in (
            [{"role": "user", "content": "task"}],
            [{"role": "system", "content": [{"type": "text", "text": "keep"}]}],
        ):
            output = catalog.inject_catalog(messages, binding)
            self.assertEqual(output[0]["role"], "system")
            self.assertEqual(output, catalog.inject_catalog(output, binding))
        with self.assertRaises(catalog.CatalogError):
            catalog.inject_catalog([{"role": "user", "content": catalog.CATALOG_MARKER}], binding)

    def test_fallback_protocol_is_explicit_and_ignores_credentials_and_execution_fields(self):
        config = {
            "runtime_dir": self.config["runtime_dir"],
            "model_id": "local",
            "api_key": "secret-a",
            "execution_id": "one",
        }
        binding = catalog.prepare_binding(config, "fallback", "execution-a", self.bank)
        self.assertEqual(binding["protocol_hash_source"], "public-config-sha256")
        self.assertEqual(
            binding,
            catalog.load_binding({**config, "api_key": "secret-b", "execution_id": "two"}, "fallback", "execution-a"),
        )
        with self.assertRaises(catalog.CatalogError):
            catalog.load_binding({**config, "model_id": "other"}, "fallback", "execution-a")


if __name__ == "__main__":
    unittest.main()
