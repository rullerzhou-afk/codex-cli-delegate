import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import claude_sdk_backend as backend

# Shaped like the entries Clawd writes: a command entry that runs clawd-hook.js.
CLAWD_STOP = {"type": "command",
              "command": '"/opt/node" "/Applications/clawd-on-desk/hooks/clawd-hook.js" Stop',
              "timeout": 5, "async": True}
CLAWD_NOTIFY = {"type": "command",
                "command": '"/opt/node" "/Applications/clawd-on-desk/hooks/clawd-hook.js" Notification',
                "timeout": 5}
# The permission hook is http, so it must never be carried over.
CLAWD_PERMISSION = {"type": "http", "url": "http://127.0.0.1:23333/permission"}
# Another program's hook (for example orca) must keep the default isolation.
OTHER_HOOK = {"type": "command",
              "command": "/Users/fixture/.orca/agent-hooks/claude-hook.sh", "timeout": 5}


class ClawdStatusHooks(unittest.TestCase):
    """The restricted SDK path borrows only Clawd's status command hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.settings = self.config / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def isolate(self):
        return patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.config)})

    def test_keeps_only_clawd_status_command_hooks(self):
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls*)"]},
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [CLAWD_STOP, OTHER_HOOK]}],
                "Notification": [{"matcher": "idle_prompt", "hooks": [CLAWD_NOTIFY]}],
                "PermissionRequest": [{"matcher": "", "hooks": [CLAWD_PERMISSION]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [OTHER_HOOK]}]}}))
        with self.isolate():
            hooks = backend.clawd_status_hooks()
        self.assertEqual(set(hooks), {"Stop", "Notification"})
        # Event grouping, matcher, timeout and async survive byte-for-byte.
        self.assertEqual(hooks["Stop"], [{"matcher": "", "hooks": [CLAWD_STOP]}])
        self.assertEqual(hooks["Notification"], [{"matcher": "idle_prompt", "hooks": [CLAWD_NOTIFY]}])

    def test_non_clawd_or_missing_hooks_contribute_nothing(self):
        self.settings.write_text(json.dumps({
            "hooks": {"Stop": [{"matcher": "", "hooks": [OTHER_HOOK, CLAWD_PERMISSION]}]}}))
        with self.isolate():
            self.assertEqual(backend.clawd_status_hooks(), {})
        self.settings.write_text(json.dumps({"permissions": {"allow": []}}))
        with self.isolate():
            self.assertEqual(backend.clawd_status_hooks(), {})

    def test_missing_bad_json_and_wrong_shapes_are_silent(self):
        cases = [None, "not json", json.dumps(["a"]), json.dumps("str"),
                 json.dumps({"hooks": ["nope"]}), json.dumps({"hooks": "nope"}),
                 json.dumps({"hooks": {"Stop": "nope"}}),
                 json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": "nope"}]}})]
        for case in cases:
            if case is None:
                self.settings.unlink(missing_ok=True)
            else:
                self.settings.write_text(case)
            with self.isolate():
                self.assertEqual(backend.clawd_status_hooks(), {}, case)

    def test_home_settings_used_only_without_config_dir(self):
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"Stop": [{"matcher": "", "hooks": [CLAWD_STOP]}]}}))
        env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
        env["HOME"] = str(home)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(backend.clawd_status_hooks(),
                             {"Stop": [{"matcher": "", "hooks": [CLAWD_STOP]}]})

    def test_unresolvable_home_is_silent(self):
        env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(backend.Path, "home", side_effect=RuntimeError("no home")):
                self.assertEqual(backend.clawd_status_hooks(), {})

    def test_non_string_command_is_ignored(self):
        self.settings.write_text(json.dumps({"hooks": {"Stop": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": ["clawd-hook.js"], "timeout": 5}]}]}}))
        with self.isolate():
            self.assertEqual(backend.clawd_status_hooks(), {})

    def test_options_writes_only_clawd_hooks_and_keeps_permissions(self):
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls*)"]},
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [CLAWD_STOP]}],
                "PermissionRequest": [{"matcher": "", "hooks": [CLAWD_PERMISSION]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [OTHER_HOOK]}]}}))
        worker = backend.SDKWorker.__new__(backend.SDKWorker)
        worker.ctx = SimpleNamespace(state_dir=str(self.root / "state"))
        worker.root = self.root / "job"
        worker.root.mkdir(parents=True)
        worker.job = {"job_id": "clawd-hook-fixture", "claude_bin": "/bin/claude",
                      "cwd": str(self.root), "session_id": "session-fixture",
                      "read_dirs": [], "required_files": [], "allow_tools": []}
        worker.args = SimpleNamespace(run_token="token-fixture", resume=False)
        with self.isolate():
            worker.options({"round": 0, "run_token": "token-fixture"})
        written = json.loads((worker.root / "sdk-settings.json").read_text())
        self.assertEqual(set(written["hooks"]), {"Stop"})
        self.assertEqual(written["hooks"]["Stop"], [{"matcher": "", "hooks": [CLAWD_STOP]}])
        self.assertIn("Edit(/" + str(self.root).rstrip("/") + "/**)",
                      written["permissions"]["allow"])


if __name__ == "__main__":
    unittest.main()
