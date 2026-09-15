"""Provider-independent lifecycle exercised through a registered fake adapter.

The fake is registered by name and enters through the real provider-independent
commands: ``cmd_start`` creates the job and launches the worker, which resolves
the fake adapter, then Codex acceptance completes the job. The worker subprocess
finds the fake via a test-side ``sitecustomize`` on ``PYTHONPATH``; no product
lifecycle code knows about it. The job fixture is not hand-written here.
"""
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(os.environ["DELEGATE_SCRIPT"]).resolve()
sys.path.insert(0, str(SCRIPT.parent))
TESTDIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTDIR))

# Canonical import: the extracted lifecycle modules resolve claude_task by its
# canonical name, so an under-test copy would diverge.
import claude_task as ct  # noqa: E402
import delegate_transport  # noqa: E402
import fake_transport  # noqa: E402


class TransportSeam(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-transport-seam-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        delegate_transport.register(fake_transport.FakeTransport())
        # The detached worker is a fresh interpreter; a test-side sitecustomize
        # registers the same adapter there without touching product lifecycle.
        hook = self.root / "hook"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import fake_transport\n"
            "from delegate_transport import register\n"
            "register(fake_transport.FakeTransport())\n" % str(TESTDIR),
            encoding="utf-8")
        existing = os.environ.get("PYTHONPATH", "")
        parts = [str(hook), str(TESTDIR)] + ([existing] if existing else [])
        patcher = patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join(parts)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = ct.Context(str(self.root / "state"), owner="fake-owner", claude_bin="/bin/false")

    def tearDown(self):
        delegate_transport._REGISTRY.pop("fake", None)

    def test_fake_transport_start_worker_completion_and_acceptance(self):
        cwd = self.root / "checkout"
        cwd.mkdir()
        prompt = self.root / "task.md"
        prompt.write_text("fake task")
        args = SimpleNamespace(backend="fake", transport="cli", cwd=str(cwd),
                               prompt_file=str(prompt), timeout=None, max_revisions=None,
                               allow_tool=[], read_dir=[], require_file=[], request=None,
                               kimi_bin=None, kimi_tool=[], opencode_bin=None, opencode_tool=[])
        started = ct.cmd_start(self.ctx, args)
        job = started["job_id"]
        self.assertTrue((Path(self.ctx.job_dir(job)) / "job.json").is_file())
        self.assertNotEqual(started["phase"], ct.PHASE_ACCEPTED)

        deadline = time.monotonic() + 25
        status = None
        while time.monotonic() < deadline:
            status = self.ctx.load(job)
            if status["phase"] not in ct.BUSY_PHASES:
                break
            time.sleep(0.1)
        self.assertIsNotNone(status)
        self.assertEqual(status["phase"], ct.PHASE_AWAITING_REVIEW, status.get("attention"))
        self.assertNotEqual(status["phase"], ct.PHASE_ACCEPTED)
        self.assertEqual(status["backend"], "fake")

        notes = self.root / "review.md"
        notes.write_text("Independently reviewed the fake transport completion.")
        accepted = ct.cmd_accept(self.ctx, SimpleNamespace(
            job=job, notes_file=str(notes), evidence_dir=None, expected_round=0))
        self.assertEqual(accepted["phase"], ct.PHASE_ACCEPTED)
        final = self.ctx.load(job)
        self.assertEqual(final["phase"], ct.PHASE_ACCEPTED)
        self.assertNotIn(ct.PHASE_ACCEPTED, ct.RESERVING_PHASES)
        self.assertEqual(final["rounds"][0]["verification"]["report"], "fake-completion")


if __name__ == "__main__":
    unittest.main(verbosity=2)
