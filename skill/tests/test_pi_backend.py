import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claude_task as ct
import pi_backend as pi
from review_evidence import EvidenceError, visible_responses


class PiBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.sid = "0a0a0a0a-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.session = self.sessions / ("2026-01-01T00-00-00-000Z_" + self.sid + ".jsonl")
        self.stdout = self.root / "stdout.ndjson"
        self.job = dict(cwd=str(self.root), session_id=self.sid,
                        pi_session_dir=str(self.sessions), pi_version="0.85.1",
                        pi_bin="/bin/echo", pi_tools=["read"],
                        model=pi.MODEL, effort=pi.EFFORT)
        self.record = dict(run_token="round-zero", kind="initial", started_epoch=0,
                           prior_assistant_uuids=[], baseline_status="empty")
        self.native = [
            dict(type="session", version=3, id=self.sid, cwd=str(self.root)),
            dict(type="session_info", id="marker-0", parentId=None,
                 name="codex-delegate:round-zero"),
            dict(type="model_change", id="model-0", parentId="marker-0",
                 provider=pi.PROVIDER, modelId=pi.RAW_MODEL),
            dict(type="thinking_level_change", id="thinking-0", parentId="model-0",
                 thinkingLevel=pi.EFFORT),
            self.message("user-0", "thinking-0", "user", "[codex-delegate:round-zero]\nscope"),
            self.message("assistant-0", "user-0", "assistant", "answer"),
        ]
        self.stream = self.round_stream("round-zero", "answer")
        self.save()

    @staticmethod
    def message(ident, parent, role, text):
        message = dict(role=role, content=[dict(type="text", text=text)], timestamp=1)
        if role == "assistant":
            message.update(provider=pi.PROVIDER, model=pi.RAW_MODEL,
                           stopReason="stop", responseId=ident,
                           usage=dict(input=1, output=1, reasoning=0))
        return dict(type="message", id=ident, parentId=parent, message=message)

    def round_stream(self, token, answer):
        user = dict(role="user", content=[dict(type="text", text="[codex-delegate:" + token + "]\nscope")])
        assistant = dict(role="assistant", content=[dict(type="text", text=answer)],
                         provider=pi.PROVIDER, model=pi.RAW_MODEL, stopReason="stop",
                         responseId="response-" + token, usage=dict(input=1, output=1, reasoning=0))
        return [dict(type="session", version=3, id=self.sid, cwd=str(self.root)),
                dict(type="message_end", message=user),
                dict(type="message_end", message=assistant),
                dict(type="agent_end", messages=[user, assistant], willRetry=False),
                dict(type="agent_settled")]

    def save(self):
        self.session.write_text("".join(json.dumps(row) + "\n" for row in self.native))
        self.stdout.write_text("".join(json.dumps(row) + "\n" for row in self.stream))

    def verify(self):
        return pi.verify(self.job, self.record, str(self.stdout), 0)

    def test_good_round_verifies_exact_session_model_effort_and_report(self):
        result = self.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["assistant_models"], [pi.MODEL])
        self.assertEqual(result["efforts"], ["off"])
        self.assertEqual(result["report"], "answer")

    def test_revision_uses_immutable_prefix_and_same_native_session(self):
        prior, status = pi.baseline(self.job)
        self.record = dict(run_token="round-one", kind="revision", started_epoch=0,
                           prior_assistant_uuids=prior, baseline_status=status)
        self.native += [
            dict(type="session_info", id="marker-1", parentId="assistant-0",
                 name="codex-delegate:round-one"),
            self.message("user-1", "marker-1", "user", "[codex-delegate:round-one]\nscope"),
            self.message("assistant-1", "user-1", "assistant", "revised"),
        ]
        self.stream = self.round_stream("round-one", "revised")
        self.save()
        result = self.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["new_assistant_entries"], 1)
        self.session.write_text(self.session.read_text().replace("answer", "changed", 1))
        self.assertIn("native baseline changed", " ".join(self.verify()["reasons"]))

    def test_wrong_model_effort_cwd_report_and_incomplete_session_fail_closed(self):
        mutations = [
            lambda: self.native[-1]["message"].update(model="fallback"),
            lambda: self.native[3].update(thinkingLevel="high"),
            lambda: self.native[0].update(cwd="/"),
            lambda: self.stream[2]["message"]["content"][0].update(text="invented"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp()
                mutate()
                self.save()
                self.assertFalse(self.verify()["ok"])
        self.setUp()
        self.session.write_text(self.session.read_text().rstrip("\n"))
        self.assertFalse(self.verify()["ok"])

    def test_setup_isolated_session_stdin_prompt_and_no_extensions(self):
        prompt = self.root / "task.md"
        prompt.write_text("scope")
        with patch.object(pi, "prepare", return_value=dict(
                pi_version="0.86.0", pi_compatibility={"cli_options": "checked"})):
            argv, env, saved = pi.setup(self.job, self.record, False, str(prompt), self.root)
        self.assertEqual(argv[argv.index("--provider") + 1], pi.PROVIDER)
        self.assertEqual(argv[argv.index("--model") + 1], pi.RAW_MODEL)
        self.assertEqual(argv[argv.index("--thinking") + 1], "off")
        self.assertEqual(argv[argv.index("--session-id") + 1], self.sid)
        self.assertIn("--no-extensions", argv)
        self.assertIn("--no-skills", argv)
        self.assertNotIn("scope", " ".join(argv))
        self.assertIn("[codex-delegate:round-zero]", Path(saved).read_text())
        self.assertEqual(env["PWD"], str(self.root))

    def test_setup_refuses_changed_saved_profile(self):
        prompt = self.root / "task.md"
        prompt.write_text("scope")
        self.job["model"] = "openrouter/another-model"
        with self.assertRaises(ct.CliError) as got:
            pi.setup(self.job, self.record, False, str(prompt), self.root)
        self.assertEqual(got.exception.code, "pi_profile")

    def test_preflight_checks_auth_exact_model_and_required_flags(self):
        help_text = "\n".join("--" + flag for flag in pi.REQUIRED_FLAGS)
        ready = json.dumps(dict(status="ready", provider="openrouter", authType="api_key"))
        model = "provider model context\nopenrouter stealth/union-alpha 262.1K"
        with patch.object(pi.subprocess, "check_output",
                          side_effect=["0.86.0", help_text, ready, model]):
            result = pi.prepare("/bin/echo", ["read"], [])
        self.assertEqual(result["pi_version"], "0.86.0")
        self.assertEqual(result["pi_tools"], ["read"])
        with patch.object(pi.subprocess, "check_output",
                          side_effect=["0.86.0", help_text, ready, "provider model"]):
            with self.assertRaises(ct.CliError) as got:
                pi.prepare("/bin/echo", ["read"], [])
        self.assertEqual(got.exception.code, "pi_model_missing")

    def test_export_includes_visible_text_only(self):
        private = dict(type="message_update", assistantMessageEvent=dict(
            type="thinking_delta", delta="private"))
        data = ("".join(json.dumps(row) + "\n" for row in self.stream + [private])).encode()
        self.assertEqual([item["text"] for item in visible_responses(data, "pi", self.sid)], ["answer"])
        wrong = list(self.stream)
        wrong[0] = dict(wrong[0], id="different")
        with self.assertRaises(EvidenceError):
            visible_responses(("".join(json.dumps(row) + "\n" for row in wrong)).encode(), "pi", self.sid)


if __name__ == "__main__":
    unittest.main()
