"""Frozen black-box contract suite for the public entry points.

This suite drives only:
* ``skill/scripts/delegate.py`` as a subprocess, and
* ``skill/scripts/delegate_mcp.py`` over a real MCP stdio connection,

and asserts the public JSON contract plus on-disk state. It must not import
``claude_task`` or call private worker commands. Phase 2 (runtime separation)
keeps this file unchanged; a required public contract change stops the refactor
and ships as a separate versioned change instead. See docs/CONTRACTS.md.

PROCESS_IDENTITY marks that this module launches real detached workers and
observes local process state, so CI runs it in the macOS process-identity job
(the intended required check; enforcement is not configured by this repo).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

PROCESS_IDENTITY = True
BLACKBOX_FROZEN = True

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skill" / "scripts" / "delegate.py"
MCP_ENTRY = ROOT / "skill" / "scripts" / "delegate_mcp.py"
FAKE = Path(__file__).with_name("fake_claude.py").resolve()
FAKE_SDK = ROOT / "skill" / "tests" / "fake_sdk_cli.py"
FIXTURES = Path(__file__).with_name("fixtures")


class PublicCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-delegate-blackbox-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                        PYTHONDONTWRITEBYTECODE="1")
        self.jobs = []

    def call(self, *args, owner="owner-a", ok=True):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(self.state),
                               "--owner", owner, "--claude-bin", str(FAKE), *args],
                              env=self.env, capture_output=True, text=True, timeout=30)
        self.assertTrue(proc.stdout.strip(), proc.stderr)
        data = json.loads(proc.stdout)
        if ok:
            self.assertEqual(proc.returncode, 0, data)
            self.assertTrue(data.get("ok"), data)
        else:
            self.assertNotEqual(proc.returncode, 0, data)
            self.assertFalse(data.get("ok"), data)
        return data

    def prompt(self, **task):
        path = self.root / (uuid.uuid4().hex + ".json")
        path.write_text(json.dumps(task))
        return str(path)

    def start(self, owner="owner-a", **task):
        cwd = self.root / uuid.uuid4().hex
        cwd.mkdir()
        data = self.call("start", "--cwd", str(cwd), "--prompt-file", self.prompt(**task),
                         "--timeout", "15", owner=owner)
        self.jobs.append((data["job_id"], owner))
        return data

    def settle(self, job, owner="owner-a"):
        data = self.call("wait", job, "--seconds", "12", owner=owner)
        self.assertNotIn(data["phase"], ("starting", "running"), data)
        return data

    def tearDown(self):
        for job, owner in self.jobs:
            try:
                self.call("stop", job, owner=owner)
            except Exception:
                pass

    def test_public_lifecycle_json_and_disk_state(self):
        started = self.start(tag="round-one")
        for key in ("ok", "job_id", "phase", "round", "session_id", "owner",
                    "backend", "cwd", "reservation", "timeout", "state_dir"):
            self.assertIn(key, started)
        self.assertEqual(started["backend"], "claude")
        job = started["job_id"]
        granted = self.settle(job)
        self.assertEqual(granted["phase"], "awaiting_review", granted)
        self.assertTrue(granted["verified"]["ok"], granted)
        self.assertTrue(granted["reservation_held"], granted)
        self.assertIn("artifacts", granted)
        self.assertTrue(Path(granted["artifacts"]["job_state"]).is_file())
        persisted = json.loads(Path(granted["artifacts"]["job_state"]).read_text())
        fixture = json.loads((FIXTURES / "job_state_v1_current.json").read_text())
        self.assertEqual(set(persisted), set(fixture))
        self.assertEqual(set(persisted["rounds"][0]), set(fixture["rounds"][0]))

        listed = self.call("list")
        self.assertEqual([item["job_id"] for item in listed["jobs"]], [job])
        self.assertEqual(listed["jobs"][0]["backend"], "claude")

        revised = self.call("revise", job, "--prompt-file", self.prompt(tag="round-two"))
        self.assertEqual(revised["round"], 1)
        self.assertTrue(revised["resume"])
        second = self.settle(job)
        self.assertEqual(second["final_report"], "round-two")

        notes = self.root / "review.md"
        notes.write_text("Independently reviewed the public black-box fixture.")
        accepted = self.call("accept", job, "--notes-file", str(notes))
        self.assertEqual(accepted["phase"], "accepted")
        self.assertTrue(accepted["reservation_released"])

        saved = json.loads((self.state / "jobs" / job / "job.json").read_text())
        self.assertEqual(saved["schema"], 1)
        self.assertEqual(saved["schema_namespace"], "codex-cli-delegate/job-state")
        self.assertEqual(saved["phase"], "accepted")
        self.assertEqual(saved["current_round"], 1)

    def test_public_error_vocabulary(self):
        missing = self.call("status", str(uuid.uuid4()), ok=False)
        self.assertEqual(missing["error"], "not_found")
        bad_timeout = self.call("start", "--cwd", str(self.root), "--prompt-file", self.prompt(),
                                "--timeout", "not-a-number", ok=False)
        self.assertEqual(bad_timeout["error"], "bad_timeout")
        bad_id = self.call("status", "not-a-uuid", ok=False)
        self.assertEqual(bad_id["error"], "bad_id")

        a = self.start(tag="holds")
        stale = self.call("revise", a["job_id"], "--expected-round", "9",
                          "--prompt-file", self.prompt(tag="stale"), ok=False)
        self.assertEqual(stale["error"], "stale_round")

        cwd = self.root / "busy"
        cwd.mkdir()
        holder = self.call("start", "--cwd", str(cwd), "--prompt-file", self.prompt(tag="hold"),
                           "--timeout", "15")
        self.jobs.append((holder["job_id"], "owner-a"))
        conflict = self.call("start", "--cwd", str(cwd), "--prompt-file", self.prompt(),
                             owner="owner-b", ok=False)
        self.assertEqual(conflict["error"], "checkout_conflict")

    def test_accepted_fixture_status_is_reservation_free(self):
        job_id = "22222222-2222-2222-2222-222222222222"
        target = self.state / "jobs" / job_id
        target.mkdir(parents=True)
        (target / "job.json").write_bytes((FIXTURES / "job_state_v1_accepted.json").read_bytes())
        status = self.call("status", job_id, owner="fixture-owner")
        self.assertEqual(status["phase"], "accepted")
        self.assertFalse(status["reservation_held"])
        self.assertEqual(status["attention"]["reason"], "stop_incomplete")

    def test_unknown_schema_fails_closed_through_public_cli(self):
        job_id = str(uuid.uuid4())
        target = self.state / "jobs" / job_id
        target.mkdir(parents=True)
        (target / "job.json").write_text(json.dumps({"schema": 999, "owner": "owner-a",
                                                     "job_id": job_id, "phase": "failed"}))
        denied = self.call("status", job_id, ok=False)
        self.assertEqual(denied["error"], "unsupported_schema")
        self.assertEqual(denied["namespace"], "codex-cli-delegate/job-state")

    def test_foreign_namespace_fails_closed_through_public_cli(self):
        job_id = str(uuid.uuid4())
        target = self.state / "jobs" / job_id
        target.mkdir(parents=True)
        (target / "job.json").write_text(json.dumps({
            "schema": 1, "schema_namespace": "someone-else/job-state",
            "owner": "owner-a", "job_id": job_id, "phase": "failed"}))
        denied = self.call("status", job_id, ok=False)
        self.assertEqual(denied["error"], "unsupported_schema")
        self.assertEqual(denied["expected"], "codex-cli-delegate/job-state")


class PublicMCP(unittest.IsolatedAsyncioTestCase):
    """A real stdio MCP connection against the public server entry point."""

    @staticmethod
    def sdk_diagnostics(state, job):
        """Read the on-disk diagnostics a failed SDK round leaves behind.

        This stays black-box: it only reads files under the public state root.
        """
        root = Path(state) / "jobs" / job
        parts = []
        for name in ("sdk-errors.ndjson", "sdk-stderr.ndjson"):
            path = root / name
            if path.is_file():
                parts.append("%s: %s" % (name, path.read_text(errors="replace")[-2000:]))
        for name in ("rounds/r000/stdout.ndjson", "rounds/r000/stderr.log", "rounds/r000/worker.log"):
            path = root / name
            if path.is_file():
                parts.append("%s: %s" % (name, path.read_text(errors="replace")[-2000:]))
        return "\n".join(parts) or "(no sdk diagnostics found)"

    async def test_stdio_tools_and_public_job_lifecycle(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="codex-delegate-mcp-") as directory:
            root = Path(directory).resolve()
            cwd = root / "checkout"
            cwd.mkdir()
            fake = root / "fake-sdk-cli"
            shutil.copyfile(FAKE_SDK, fake)
            fake.chmod(0o700)
            account = root / "account"
            params = StdioServerParameters(command=sys.executable, args=[
                str(MCP_ENTRY), "--state-dir", str(root / "state"), "--claude-bin", str(fake)],
                env={**os.environ, "CLAUDE_CONFIG_DIR": str(account)})
            spec = dict(owner="wire-owner", request_id="wire-request", cwd=str(cwd),
                        task=json.dumps({"tag": "wire", "delay": 1}), timeout=20)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertEqual(len(tools.tools), 8)
                    listed = await client.call_tool("delegate_list", {"owner": "wire-owner"})
                    self.assertFalse(listed.is_error, listed)
                    bad = await client.call_tool("delegate_start", {"owner": "wire-owner",
                        "request_id": "bad", "cwd": str(cwd), "task": "x", "backend": "pi"})
                    self.assertTrue(bad.is_error)
                    self.assertEqual(bad.structured_content["error"], "unsupported_backend")
                    started = await client.call_tool("delegate_start", spec)
                    self.assertFalse(started.is_error, started)
                    job = started.structured_content["job_id"]
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        replay = await client.call_tool("delegate_start", spec)
                        self.assertTrue(replay.structured_content["replayed"])
                        self.assertEqual(replay.structured_content["job_id"], job)
                        cursor = "-1:0"
                        data = None
                        for _ in range(12):
                            result = await client.call_tool("delegate_wait", dict(
                                owner="wire-owner", job_id=job, cursor=cursor, timeout=5))
                            self.assertFalse(result.is_error, result)
                            data = result.structured_content
                            cursor = data["cursor"]
                            if data["phase"] not in ("starting", "running"):
                                break
                        if data["phase"] != "awaiting_review":
                            self.fail("SDK round did not reach awaiting_review: %s\n%s"
                                      % (data, self.sdk_diagnostics(root / "state", job)))
                        self.assertEqual(data["next_action"], "independently_review_then_accept_or_revise")
                        accepted = await client.call_tool("delegate_accept", dict(
                            owner="wire-owner", job_id=job, expected_round=0,
                            notes="Independently reviewed the public MCP black-box fixture."))
                        self.assertFalse(accepted.is_error, accepted)
                        self.assertEqual(accepted.structured_content["phase"], "accepted")
                        self.assertEqual(accepted.structured_content["next_action"], "done")
                saved = json.loads((root / "state" / "jobs" / job / "job.json").read_text())
                self.assertEqual(saved["schema"], 1)
                self.assertEqual(saved["schema_namespace"], "codex-cli-delegate/job-state")
                self.assertEqual(saved["phase"], "accepted")
            finally:
                subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(root / "state"),
                                "--owner", "wire-owner", "--claude-bin", str(fake),
                                "stop", job], capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
