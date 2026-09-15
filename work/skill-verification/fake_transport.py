"""Deterministic fake transport adapter for the Phase 2 seam test.

No provider and no real CLI: the child is a no-op Python process and the
adapter returns a synthetic completion. It is registered by name only; the
lifecycle is not modified to know about it.
"""
import os
import sys
import uuid

from delegate_transport import PreparedRound, Transport


class FakeMonitor:
    def tick(self, complete=False):
        pass


class FakeTransport(Transport):
    name = "fake"
    native_session = False

    def new_session_id(self):
        return str(uuid.uuid4())

    def start_config(self, ctx, args, prompt, read_dirs, required_files):
        fields, extra, session_id = super().start_config(ctx, args, prompt, read_dirs, required_files)
        fields.update(model="fake-model", effort="max", claude_bin=None,
                      claude_config_dir=None, claude_config_env=None)
        return fields, extra, session_id

    def prepare(self, state_dir, job, record, resume, prompt_file, directory):
        return PreparedRound(argv=[sys.executable, "-c", "pass"], env=dict(os.environ),
                             prompt_file=prompt_file, monitor=FakeMonitor())

    def capture_child(self, job, pid, token):
        return {"pid": pid, "run_token": token, "lstart": "fixture",
                "identity_verified": True, "recorded_at": "fixture"}

    def verify(self, job, record, stdout_path, exit_code):
        return {"ok": exit_code == 0, "reasons": [], "needs_attention": False,
                "model_verified": True, "effort_verified": True, "session_ok": True,
                "completion_basis": "process_exit", "assistant_models": ["fake-model"],
                "efforts": ["max"], "report": "fake-completion"}
