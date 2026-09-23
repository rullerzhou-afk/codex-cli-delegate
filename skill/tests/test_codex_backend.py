import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import claude_task as ct
import codex_backend as cx
import delegate_transport
from delegate_service import DelegateService
from delegate_transport import CodexTransport
from review_evidence import EvidenceError, visible_responses

THREAD = "0a0a0a0a-bbbb-7ccc-8ddd-eeeeeeeeeeee"


class CodexBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settle = patch.object(cx, "NATIVE_SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "codex-home"
        day = self.home / "sessions" / "2026" / "01" / "01"
        day.mkdir(parents=True)
        self.rollout = day / ("rollout-2026-01-01T00-00-00-" + THREAD + ".jsonl")
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.round_dir = self.root / "r000"
        self.round_dir.mkdir()
        self.stdout = self.round_dir / "stdout.ndjson"
        self.job = dict(cwd=str(self.cwd), session_id=None, codex_home=str(self.home),
                        codex_bin="/bin/echo", codex_tools=["read"], codex_version="codex-cli 0.155.0",
                        codex_system_proxy=True, model=cx.MODEL, effort=cx.EFFORT)
        self.record = dict(run_token="round-zero", kind="initial", started_epoch=0,
                           prior_assistant_uuids=[], baseline_status="empty")
        self.native = [dict(timestamp="t0", type="session_meta", payload=dict(
            id=THREAD, session_id=THREAD, cwd=str(self.cwd), originator="codex_exec",
            cli_version="0.155.0", source="exec"))]
        self.native += self.native_turn("turn-0", "round-zero", "answer", context_message=True)
        self.stream = self.round_stream("answer")
        self.written = "answer"
        self.save()

    def native_turn(self, turn_id, token, answer, context_message=False, **context):
        settings = dict(turn_id=turn_id, cwd=str(self.cwd), model=cx.MODEL, effort=cx.EFFORT,
                        approval_policy="never", sandbox_policy=dict(type="read-only"))
        settings.update(context)
        rows = [dict(timestamp="t", type="event_msg", payload=dict(type="task_started", turn_id=turn_id))]
        if context_message:
            rows.append(self.message("user", "# AGENTS.md instructions"))
        rows += [
            dict(timestamp="t", type="turn_context", payload=settings),
            self.message("user", "[codex-delegate:" + token + "]\nscope"),
            self.message("assistant", answer),
            dict(timestamp="rate-at", type="event_msg", payload=dict(type="token_count", rate_limits=dict(
                limit_id="codex", plan_type="pro",
                primary=dict(used_percent=42.0, window_minutes=10080, resets_at=1767225600), secondary=None))),
            dict(timestamp="t", type="event_msg", payload=dict(
                type="task_complete", turn_id=turn_id, last_agent_message=answer)),
        ]
        return rows

    @staticmethod
    def message(role, text):
        kind = "output_text" if role == "assistant" else "input_text"
        return dict(timestamp="t", type="response_item",
                    payload=dict(type="message", role=role, content=[dict(type=kind, text=text)]))

    @staticmethod
    def round_stream(answer, *extra):
        return [dict(type="thread.started", thread_id=THREAD), dict(type="turn.started"), *extra,
                dict(type="item.completed", item=dict(id="item_0", type="agent_message", text=answer)),
                dict(type="turn.completed", usage=dict(input_tokens=10, cached_input_tokens=8,
                                                       output_tokens=2, reasoning_output_tokens=0))]

    def save(self):
        self.rollout.write_text("".join(json.dumps(row) + "\n" for row in self.native))
        self.stdout.write_text("".join(json.dumps(row) + "\n" for row in self.stream))
        last = self.round_dir / "codex-last-message.txt"
        if self.written is None:
            last.unlink(missing_ok=True)
        else:
            last.write_text(self.written)

    def verify(self, exit_code=0):
        return cx.verify(self.job, self.record, str(self.stdout), exit_code)

    def test_good_round_verifies_thread_model_effort_sandbox_and_report(self):
        result = self.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["native_session_id"], THREAD)
        self.assertEqual(result["assistant_models"], [cx.MODEL])
        self.assertEqual(result["efforts"], [cx.EFFORT])
        self.assertEqual(result["report"], "answer")
        self.assertEqual(result["usage"], dict(input_tokens=10, cache_read_input_tokens=8,
                                               output_tokens=2, reasoning_output_tokens=0))
        limits = result["codex_rate_limits"]
        self.assertEqual(limits["max_used_percent"], 42.0)
        self.assertFalse(limits["at_or_above_90"])
        self.assertEqual(limits["observed_at"], "rate-at")
        native = json.loads((self.round_dir / "codex-native.json").read_text())
        self.assertEqual((native["turn_id"], native["sandbox"], native["approval"]),
                         ("turn-0", "read-only", "never"))

    def test_revision_resumes_same_thread_on_immutable_prefix(self):
        self.job["session_id"] = THREAD
        prior, status = cx.baseline(self.job)
        self.record = dict(run_token="round-one", kind="revision", started_epoch=0,
                           prior_assistant_uuids=prior, baseline_status=status)
        self.native += [dict(timestamp="t", type="event_msg",
                             payload=dict(type="thread_settings_applied", thread_id=THREAD))]
        self.native += self.native_turn("turn-1", "round-one", "revised")
        self.stream = self.round_stream("revised")
        self.written = "revised\n"
        self.save()
        result = self.verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["report"], "revised")
        self.rollout.write_text(self.rollout.read_text().replace("answer", "changed", 1))
        self.assertIn("native baseline changed", " ".join(self.verify()["reasons"]))

    def test_revision_rejects_old_marker_and_a_different_thread(self):
        self.job["session_id"] = THREAD
        prior, status = cx.baseline(self.job)
        self.record = dict(run_token="round-zero", kind="revision", started_epoch=0,
                           prior_assistant_uuids=prior, baseline_status=status)
        self.assertIn("marker missing or ambiguous", " ".join(self.verify()["reasons"]))
        self.job["session_id"] = "0a0a0a0a-0000-7000-8000-000000000000"
        self.assertIn("different thread", " ".join(self.verify()["reasons"]))

    def test_profile_sandbox_approval_cwd_and_report_fail_closed(self):
        cases = [
            (lambda: self.native[3]["payload"].update(model="gpt-5.6-sol"), "model_unverified"),
            (lambda: self.native[3]["payload"].update(effort="high"), "effort_unverified"),
            (lambda: self.native[3]["payload"].update(sandbox_policy=dict(type="workspace-write")),
             "sandbox_unverified"),
            (lambda: self.native[3]["payload"].update(approval_policy="on-request"), "approval_unverified"),
            (lambda: self.native[3]["payload"].update(cwd="/"), "native turn cwd mismatch"),
            (lambda: self.native[0]["payload"].update(cwd="/"), "native session cwd mismatch"),
            (lambda: self.native[0]["payload"].update(originator="codex_work_desktop"), "not created by codex exec"),
            (lambda: self.stream[2]["item"].update(text="invented"), "final_report_mismatch"),
            (lambda: setattr(self, "written", "other"), "final_report_mismatch"),
            (lambda: setattr(self, "written", None), "final_report_file_missing"),
            (lambda: self.native.pop(), "native_completion_missing"),
        ]
        for mutate, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                mutate()
                self.save()
                result = self.verify()
                self.assertFalse(result["ok"])
                self.assertIn(reason, " ".join(result["reasons"]))

    def test_failed_turn_and_broken_stream_fail(self):
        self.stream = [dict(type="thread.started", thread_id=THREAD), dict(type="turn.started"),
                       dict(type="error", message="boom"), dict(type="turn.failed", error=dict(message="boom"))]
        self.native.pop()
        self.written = None
        self.save()
        result = self.verify(exit_code=1)
        self.assertFalse(result["ok"])
        self.assertFalse(result["needs_attention"])
        self.assertEqual(result["reasons"][0], "exit_nonzero")
        self.assertIn("turn_failed", result["reasons"])
        self.assertIn("session_error", result["reasons"])
        self.assertEqual(result["native_session_id"], THREAD)

        self.setUp()
        self.stream = self.stream[1:]
        self.save()
        result = self.verify()
        self.assertTrue(result["needs_attention"])
        self.assertIn("thread mismatch", " ".join(result["reasons"]))

    def test_foreign_later_turn_and_aborted_turn_fail(self):
        self.native += [dict(timestamp="t", type="event_msg", payload=dict(type="task_started", turn_id="later"))]
        self.save()
        self.assertIn("later foreign turn", " ".join(self.verify()["reasons"]))
        self.setUp()
        self.native.insert(-1, dict(timestamp="t", type="event_msg",
                                    payload=dict(type="turn_aborted", turn_id="turn-0")))
        self.save()
        self.assertIn("turn_aborted", self.verify()["reasons"])

    def test_read_only_file_change_is_a_profile_mismatch(self):
        change = dict(type="item.completed", item=dict(id="item_1", type="file_change", status="completed",
                                                      changes=[]))
        self.stream = self.round_stream("answer", change)
        self.save()
        result = self.verify()
        self.assertIn("tool_profile_mismatch", result["reasons"])
        self.assertEqual(result["observed_tools"], ["file_change"])
        self.job["codex_tools"] = ["read", "write"]
        self.native[3]["payload"]["sandbox_policy"] = dict(type="workspace-write", network_access=False)
        self.save()
        self.assertTrue(self.verify()["ok"])

    def test_network_requires_workspace_write_with_network_access(self):
        self.job["codex_tools"] = ["network", "read", "write"]
        self.native[3]["payload"]["sandbox_policy"] = dict(type="workspace-write", network_access=False)
        self.save()
        self.assertIn("sandbox_unverified", self.verify()["reasons"])
        self.native[3]["payload"]["sandbox_policy"]["network_access"] = True
        self.save()
        self.assertTrue(self.verify()["ok"])

    def test_setup_exec_and_resume_arguments(self):
        prompt = self.root / "task.md"
        prompt.write_text("scope")
        prepared = dict(codex_home=str(self.home), codex_version="codex-cli 0.155.1",
                        codex_compatibility={"cli_options": "checked"}, codex_system_proxy=True)
        env = dict(os.environ, CODEX_THREAD_ID="coordinator", CODEX_SQLITE_HOME="/elsewhere")
        with patch.object(cx, "prepare", return_value=prepared), patch.dict(os.environ, env, clear=True):
            argv, child_env, saved = cx.setup(self.job, self.record, False, str(prompt), self.round_dir)
        self.assertEqual(argv[1:3], ["exec", "--json"])
        self.assertEqual(argv[argv.index("-m") + 1], cx.MODEL)
        self.assertEqual(argv[argv.index("-C") + 1], str(self.cwd))
        self.assertEqual(argv[-1], "-")
        for flag in ("--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
            self.assertIn(flag, argv)
        configs = [argv[i + 1] for i, value in enumerate(argv) if value == "-c"]
        self.assertIn('model_reasoning_effort="xhigh"', configs)
        self.assertIn('approval_policy="never"', configs)
        self.assertIn('sandbox_mode="read-only"', configs)
        self.assertNotIn("sandbox_workspace_write.network_access=true", configs)
        self.assertEqual(argv[argv.index("--enable") + 1], "respect_system_proxy")
        self.assertEqual(argv[argv.index("-o") + 1], str(self.round_dir / "codex-last-message.txt"))
        self.assertNotIn("scope", " ".join(argv))
        self.assertEqual(Path(saved).read_text(), "[codex-delegate:round-zero]\nscope")
        self.assertEqual(child_env["PWD"], str(self.cwd))
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertNotIn("CODEX_SQLITE_HOME", child_env)
        self.assertEqual(self.job["codex_version"], "codex-cli 0.155.1")

        self.job.update(session_id=THREAD, codex_tools=["network", "read", "write"])
        prepared["codex_system_proxy"] = False
        with patch.object(cx, "prepare", return_value=prepared):
            resumed, _, _ = cx.setup(self.job, self.record, True, str(prompt), self.round_dir)
        self.assertEqual(resumed[1:4], ["exec", "resume", "--json"])
        self.assertEqual(resumed[-2:], [THREAD, "-"])
        self.assertNotIn("-C", resumed)
        self.assertNotIn("--enable", resumed)
        configs = [resumed[i + 1] for i, value in enumerate(resumed) if value == "-c"]
        self.assertIn('sandbox_mode="workspace-write"', configs)
        self.assertIn("sandbox_workspace_write.network_access=true", configs)

    def test_setup_refuses_changed_profile_or_codex_home(self):
        prompt = self.root / "task.md"
        prompt.write_text("scope")
        with patch.object(cx, "prepare", return_value=dict(codex_home="/other")):
            with self.assertRaises(ct.CliError) as got:
                cx.setup(self.job, self.record, False, str(prompt), self.round_dir)
        self.assertEqual(got.exception.code, "codex_home_changed")
        self.job["model"] = "gpt-5.6-sol"
        with self.assertRaises(ct.CliError) as got:
            cx.setup(self.job, self.record, False, str(prompt), self.round_dir)
        self.assertEqual(got.exception.code, "codex_profile")
        with self.assertRaises(ct.CliError) as got:
            CodexTransport().revision_config(None, self.job, [], [])
        self.assertEqual(got.exception.code, "codex_profile")

    def catalog(self, slug=cx.MODEL, efforts=("low", "xhigh")):
        levels = [dict(effort=effort, description="") for effort in efforts]
        return json.dumps(dict(models=[dict(slug=slug, supported_reasoning_levels=levels)]))

    def test_preflight_checks_flags_and_exact_model_catalog(self):
        exec_help = "\n".join("--" + flag for flag in cx.EXEC_FLAGS)
        resume_help = "\n".join("--" + flag for flag in cx.RESUME_FLAGS)
        (self.home / "config.toml").write_text('model = "x"\n[features]\nrespect_system_proxy = true\n')
        with patch.object(cx.sys, "platform", "darwin"), patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), \
                patch.object(cx.subprocess, "check_output",
                             side_effect=["codex-cli 0.155.0", exec_help, resume_help, self.catalog()]):
            result = cx.prepare("/bin/echo", None, [])
        self.assertEqual(result["codex_tools"], ["read"])
        self.assertEqual(result["codex_home"], str(self.home))
        self.assertTrue(result["codex_system_proxy"])
        self.assertEqual(result["codex_version"], "codex-cli 0.155.0")
        for catalog in (self.catalog(slug="gpt-5.6-sol"), self.catalog(efforts=("low", "high"))):
            with self.subTest(catalog=catalog), patch.object(cx.sys, "platform", "darwin"), patch.object(
                    cx.subprocess, "check_output",
                    side_effect=["codex-cli 0.154.0", exec_help, resume_help, catalog]):
                with self.assertRaises(ct.CliError) as got:
                    cx.prepare("/bin/echo", None, [])
                self.assertEqual(got.exception.code, "codex_model_missing")
        with patch.object(cx.sys, "platform", "darwin"), patch.object(
                cx.subprocess, "check_output", side_effect=["codex-cli 0.1.0", "--json", resume_help]):
            with self.assertRaises(ct.CliError) as got:
                cx.prepare("/bin/echo", None, [])
        self.assertEqual(got.exception.code, "codex_incompatible")

    def test_preflight_validates_tools_permissions_and_platform(self):
        for tools, code in ((["network"], "codex_tools"), (["bash"], "unsupported_tools"),
                            ("read", "bad_tools")):
            with self.subTest(tools=tools), patch.object(cx.sys, "platform", "darwin"):
                with self.assertRaises(ct.CliError) as got:
                    cx.prepare("/bin/echo", tools, [])
                self.assertEqual(got.exception.code, code)
        with patch.object(cx.sys, "platform", "darwin"):
            with self.assertRaises(ct.CliError) as got:
                cx.prepare("/bin/echo", None, ["Bash(ls)"])
        self.assertEqual(got.exception.code, "codex_permissions")
        with patch.object(cx.sys, "platform", "linux"):
            with self.assertRaises(ct.CliError) as got:
                cx.prepare("/bin/echo", None, [])
        self.assertEqual(got.exception.code, "codex_platform")

    def test_system_proxy_mirrors_only_the_features_flag(self):
        config = self.home / "config.toml"
        for text, expected in (('[features]\nrespect_system_proxy = true\n', True),
                               ('[features]\nrespect_system_proxy = false\n', False),
                               ('[other]\nrespect_system_proxy = true\n', False),
                               ('[features]\nhooks = true\n\n[x]\nrespect_system_proxy = true\n', False)):
            with self.subTest(text=text):
                config.write_text(text)
                self.assertEqual(cx.system_proxy(str(self.home)), expected)
        config.unlink()
        self.assertFalse(cx.system_proxy(str(self.home)))

    def test_monitor_tracks_thread_tools_and_failures(self):
        monitor = cx.CodexMonitor(self.round_dir, None, 0, ct.write_json, clock=lambda: 1.0)
        monitor.stream(dict(type="thread.started", thread_id=THREAD))
        monitor.stream(dict(type="item.started", item=dict(id="c1", type="command_execution")))
        self.assertEqual(monitor.tools, {"c1": "command_execution"})
        monitor.stream(dict(type="item.completed", item=dict(id="c1", type="command_execution", exit_code=2,
                                                              status="failed")))
        monitor.stream(dict(type="item.completed", item=dict(id="w", type="error", message="warning")))
        monitor.stream(dict(type="turn.completed", usage=dict(input_tokens=5, output_tokens=3)))
        snap = monitor.snapshot()
        self.assertEqual(snap["session_id"], THREAD)
        self.assertEqual((snap["input_tokens"], snap["output_tokens"]), (5, 3))
        self.assertEqual([event["error"]["source"] for event in monitor.events if event["kind"] == "error"],
                         ["tool_result"])
        monitor.stream(dict(type="turn.failed", error=dict(message="x")))
        self.assertTrue(monitor.events[-1]["error"]["fatal"])

    def test_export_includes_agent_messages_from_exactly_one_thread(self):
        data = "".join(json.dumps(row) + "\n" for row in self.stream).encode()
        self.assertEqual([item["text"] for item in visible_responses(data, "codex", THREAD)], ["answer"])
        with self.assertRaises(EvidenceError):
            visible_responses(data, "codex", "0a0a0a0a-0000-7000-8000-000000000000")
        doubled = "".join(json.dumps(row) + "\n" for row in [self.stream[0], *self.stream]).encode()
        with self.assertRaises(EvidenceError):
            visible_responses(doubled, "codex", THREAD)

    def test_codex_tools_only_enter_codex_request_digests(self):
        service = DelegateService(str(self.root / "state"), "/bin/echo")
        captured = []

        def collect(_owner, _request, _operation, spec, _callback):
            captured.append(spec)
            return spec
        with patch.object(service, "dispatch", side_effect=collect):
            service.start("owner", "old-shape", str(self.cwd), "scope", backend="kimi")
            service.start("owner", "codex-shape", str(self.cwd), "scope", backend="codex")
        self.assertNotIn("codex_tools", captured[0])
        self.assertEqual(captured[1]["codex_tools"], [])

    def test_other_backends_reject_codex_options(self):
        parser = ct.build_parser()
        for backend in ("claude", "kimi", "opencode", "pi"):
            args = parser.parse_args(["start", "--cwd", "/tmp", "--prompt-file", "/tmp/task",
                                      "--backend", backend, "--codex-tool", "write"])
            with self.subTest(backend=backend), self.assertRaises(ct.CliError) as got:
                delegate_transport.for_name(backend).start_config(None, args, "/tmp/task", [], [])
            self.assertEqual(got.exception.code, "wrong_backend")
        args = parser.parse_args(["start", "--cwd", "/tmp", "--prompt-file", "/tmp/task",
                                  "--backend", "codex", "--pi-tool", "read"])
        with self.assertRaises(ct.CliError) as got:
            CodexTransport().start_config(None, args, "/tmp/task", [], [])
        self.assertEqual(got.exception.code, "wrong_backend")


if __name__ == "__main__":
    unittest.main()
