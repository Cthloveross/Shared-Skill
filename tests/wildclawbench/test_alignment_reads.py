import json
import tempfile
import unittest
from pathlib import Path

from scripts.wildclawbench.alignment_catalog import prepare_binding
from scripts.wildclawbench.alignment_reads import session


class ReadEvidenceTest(unittest.TestCase):
    def test_attempts_success_and_exact_full_document_are_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank = root / "bank"
            skill = bank / "example" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            body = "---\nname: example\ndescription: Example\n---\nFull body\n"
            skill.write_text(body)
            binding = prepare_binding({"runtime_dir": str(root / "runtime"), "protocol_hash": "f" * 64},
                                      "test", "execution", bank)
            (root / "execution-binding.json").write_text(json.dumps(binding))
            records = []
            for number, (tool, args, text, error) in enumerate([
                ("exec", {"command": "echo 'cat /root/skills/example/SKILL.md'"}, body, False),
                ("file_read", {"path": "~/skills/example/SKILL.md"}, "permission denied", True),
                ("read", {"path": "/root/skills/example/SKILL.md", "limit": 2}, body[:12], False),
                ("read_file", {"path": "/root/skills/example/SKILL.md"}, body, False),
                ("read", {"path": "/usr/lib/skills/example/SKILL.md"}, body, False),
            ]):
                call_id = str(number)
                records.extend([
                    {"message": {"role": "assistant", "content": [{"type": "toolCall", "id": call_id,
                        "name": tool, "arguments": args}]}},
                    {"message": {"role": "toolResult", "toolCallId": call_id, "isError": error,
                        "content": [{"type": "text", "text": text}]}},
                ])
            (root / "chat.jsonl").write_text("\n".join(json.dumps(row) for row in records))
            result = session(root, {"task_id": "task", "prompt": "prompt"})
            self.assertEqual(len(result["skill_read_attempts"]), 4)
            self.assertEqual(len(result["actual_skill_reads"]), 3)
            self.assertEqual(len(result["full_shared_skill_reads"]), 1)
            self.assertEqual(result["read_skills"], ["example"])
            self.assertEqual(result["tool_errors"], 1)
            self.assertEqual(result["full_shared_skill_reads"][0]["tool_call_id"], "3")
