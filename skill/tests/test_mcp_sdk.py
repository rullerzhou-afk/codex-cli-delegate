import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_task as ct
from delegate_service import DelegateService


class SDKLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cwd = self.root / "checkout"
        self.cwd.mkdir()
        fake = self.root / "fake-claude"
        shutil.copyfile(Path(__file__).with_name("fake_sdk_cli.py"), fake)
        fake.chmod(0o700)
        self.home = self.root / "account"
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        self.env.start()
        self.service = DelegateService(str(self.root / "state"), str(fake))
        self.owner = "sdk-fixture-owner"

    def tearDown(self):
        for job in self.service.list(self.owner):
            self.service.stop(self.owner, job["job_id"])
        self.env.stop()
        self.tmp.cleanup()

    def start(self, request="initial", **task):
        return self.service.start(self.owner, request, str(self.cwd), json.dumps(task), timeout=20)

    def settle(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.service.read(self.owner, job)
            if status["phase"] not in ct.BUSY_PHASES:
                if status["phase"] != "awaiting_review":
                    for name in ("sdk-errors.ndjson", "sdk-stderr.ndjson"):
                        errors = self.root / "state" / "jobs" / job / name
                        if errors.exists():
                            status[name] = errors.read_text()
                return status
            time.sleep(0.1)
        self.fail("worker did not settle: " + json.dumps(status))

    def test_same_process_rounds_hooks_and_accept(self):
        first = self.start(tag="first")
        job = first["job_id"]
        settled = self.settle(job)
        self.assertEqual(settled["phase"], "awaiting_review", settled)
        self.service.revise(self.owner, "fix", job, 0, json.dumps({"tag": "second"}))
        second = self.settle(job)
        self.assertEqual(second["phase"], "awaiting_review", second)
        calls = [json.loads(s) for s in (self.home / "calls.ndjson").read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["pid"], calls[1]["pid"])
        self.assertEqual(calls[0]["session"], calls[1]["session"])
        for index in (0, 1):
            hook = self.root / "state" / "jobs" / job / "rounds" / ("r%03d" % index) / "hooks.ndjson"
            self.assertEqual(json.loads(hook.read_text())["hook"], "Stop")
        import review_evidence
        ctx = self.service.context(self.owner)
        evidence = review_evidence.export(ctx, ctx.load_owned(job), 1, str(self.root / "export"),
                                         "Fixture native SDK round 1", ct.iso())
        provenance = json.loads(Path(evidence["provenance"]).read_text())
        self.assertIsNone(provenance["exit_code"])
        self.assertEqual(provenance["completion_basis"], "sdk_result")
        result = self.service.accept(self.owner, job, 1, "Independently checked the two fixture rounds.",
                                     evidence_dir=evidence["directory"])
        self.assertEqual(result["phase"], "accepted")

    def test_replay_survives_service_restart_and_collisions_fail(self):
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda _: self.start(tag="once"), range(2)))
        self.assertEqual(replies[0]["job_id"], replies[1]["job_id"])
        job = replies[0]["job_id"]
        self.assertEqual(self.settle(job)["phase"], "awaiting_review")
        restarted = DelegateService(self.service.state_dir, self.service.claude_bin)
        # A controller can die after committing the job but before replying.
        for receipt in (Path(self.service.state_dir) / "requests").glob("*.json"):
            receipt.unlink()
        replay = restarted.start(self.owner, "initial", str(self.cwd), json.dumps({"tag": "once"}), timeout=20)
        self.assertTrue(replay["replayed"])
        with self.assertRaisesRegex(ct.CliError, "different inputs"):
            self.start(tag="different")
        with self.assertRaises(ct.CliError):
            self.service.read("another-owner", job)
        self.assertEqual(len((self.home / "calls.ndjson").read_text().splitlines()), 1)

    def test_stop_and_recover_original_session(self):
        first = self.start(tag="before")
        job = first["job_id"]
        self.assertEqual(self.settle(job)["phase"], "awaiting_review")
        self.assertTrue(self.service.stop(self.owner, job)["stopped"])
        self.service.revise(self.owner, "recover", job, 0, json.dumps({"tag": "after"}), recover=True)
        result = self.settle(job)
        self.assertEqual(result["phase"], "awaiting_review", result)
        self.assertEqual(result["session_id"], first["session_id"])
        calls = [json.loads(s) for s in (self.home / "calls.ndjson").read_text().splitlines()]
        self.assertNotEqual(calls[0]["pid"], calls[1]["pid"])
        self.assertEqual(calls[1]["resume"], first["session_id"])

    def test_round_timeout_keeps_evidence_and_stops_child(self):
        first = self.service.start(self.owner, "timeout", str(self.cwd), json.dumps({"delay": 10}), timeout=1)
        result = self.settle(first["job_id"])
        self.assertEqual(result["phase"], "needs_attention")
        self.assertIn("timeout", result["verified"]["reasons"])
        stopped = self.service.stop(self.owner, first["job_id"])
        self.assertTrue(stopped["stopped"], stopped)
        self.assertTrue(Path(result["evidence"]["stream"]).exists())

    def test_spawn_failure_is_saved_and_retry_does_not_redispatch(self):
        with patch.object(ct.subprocess, "Popen", side_effect=OSError(11, "fixture spawn failure")):
            with self.assertRaises(ct.CliError) as error:
                self.start(tag="spawn")
        self.assertEqual(error.exception.code, "worker_spawn_failed")
        replay = self.start(tag="spawn")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["phase"], "failed")
        self.assertFalse((self.home / "calls.ndjson").exists())

    def test_quota_finishes_current_and_blocks_revision(self):
        first = self.start(tag="quota", quota=0.91)
        result = self.settle(first["job_id"])
        self.assertEqual(result["phase"], "awaiting_review", result)
        with self.assertRaises(ct.CliError) as error:
            self.service.revise(self.owner, "next", first["job_id"], 0, "{}")
        self.assertEqual(error.exception.code, "quota_paused")
        self.assertEqual(len((self.home / "calls.ndjson").read_text().splitlines()), 1)

    def test_wrong_model_never_passes_and_wait_is_read_only(self):
        first = self.start(tag="wrong", model="wrong-model")
        result = self.settle(first["job_id"])
        self.assertEqual(result["phase"], "needs_attention", result)
        waited = asyncio.run(self.service.wait(self.owner, first["job_id"], timeout=0))
        self.assertEqual(waited["event"]["kind"], "settled")
        again = asyncio.run(self.service.wait(self.owner, first["job_id"], waited["cursor"], timeout=0))
        self.assertIsNone(again["event"])
        self.assertEqual(len((self.home / "calls.ndjson").read_text().splitlines()), 1)

    def test_stale_review_and_revision_cannot_affect_new_round(self):
        first = self.start(tag="first")
        job = first["job_id"]
        self.settle(job)
        self.service.revise(self.owner, "r1", job, 0, json.dumps({"tag": "second"}))
        self.settle(job)
        for fn in (lambda: self.service.accept(self.owner, job, 0, "old review"),
                   lambda: self.service.revise(self.owner, "stale", job, 0, "{}")):
            with self.assertRaises(ct.CliError) as error:
                fn()
            self.assertEqual(error.exception.code, "stale_round")


class MCPProtocol(unittest.IsolatedAsyncioTestCase):
    async def test_server_disconnect_and_reconnect_preserve_running_job(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cwd = root / "checkout"
            cwd.mkdir()
            fake = root / "fake-claude"
            shutil.copyfile(Path(__file__).with_name("fake_sdk_cli.py"), fake)
            fake.chmod(0o700)
            account = root / "account"
            params = StdioServerParameters(command=sys.executable, args=[
                str(Path(__file__).resolve().parents[1] / "scripts" / "delegate_mcp.py"),
                "--state-dir", str(root / "state"), "--claude-bin", str(fake)],
                env={**os.environ, "CLAUDE_CONFIG_DIR": str(account)})
            spec = dict(owner="wire-owner", request_id="wire-request", cwd=str(cwd),
                        task=json.dumps({"tag": "wire", "delay": 1}), timeout=20)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    started = await client.call_tool("delegate_start", spec)
                    self.assertFalse(started.is_error, started)
                    job = started.structured_content["job_id"]
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        replay = await client.call_tool("delegate_start", spec)
                        self.assertEqual(replay.structured_content["job_id"], job)
                        self.assertTrue(replay.structured_content["replayed"])
                        cursor = "-1:0"
                        for _ in range(12):
                            result = await client.call_tool("delegate_wait", dict(owner="wire-owner", job_id=job,
                                                                                cursor=cursor, timeout=5))
                            self.assertFalse(result.is_error, result)
                            data = result.structured_content
                            cursor = data["cursor"]
                            if data["phase"] not in ct.BUSY_PHASES:
                                break
                        self.assertEqual(data["phase"], "awaiting_review", data)
                        accepted = await client.call_tool("delegate_accept", dict(owner="wire-owner", job_id=job,
                            expected_round=0, notes="Checked the original job, one native invocation, and successful SDK verification."))
                        self.assertFalse(accepted.is_error, accepted)
                        self.assertEqual(accepted.structured_content["phase"], "accepted")
                self.assertEqual(len((account / "calls.ndjson").read_text().splitlines()), 1)
            finally:
                DelegateService(str(root / "state"), str(fake)).stop("wire-owner", job)

    async def test_stdio_initialize_tools_and_validation(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        with tempfile.TemporaryDirectory() as directory:
            params = StdioServerParameters(command=sys.executable, args=[
                str(Path(__file__).resolve().parents[1] / "scripts" / "delegate_mcp.py"), "--state-dir", directory])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertEqual(len(tools.tools), 8)
                    notification = await client.call_tool("delegate_notify", {
                        "owner": "protocol-fixture", "job_id": "not-a-job", "expected_round": 0})
                    self.assertTrue(notification.is_error)
                    result = await client.call_tool("delegate_list", {"owner": "protocol-fixture"})
                    self.assertFalse(result.is_error)
                    bad = await client.call_tool("delegate_start", {"owner": "protocol-fixture", "request_id": "bad",
                        "cwd": directory, "task": "test", "backend": "pi"})
                    self.assertTrue(bad.is_error)
                    self.assertIn("unsupported_backend", str(bad))


if __name__ == "__main__":
    unittest.main()
