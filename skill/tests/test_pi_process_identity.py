import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import delegate_process
from delegate_transport import PiTransport


PROCESS_IDENTITY = True


class PiProcessIdentity(unittest.TestCase):
    def test_node_title_change_keeps_kernel_identity_terminable(self):
        self.assertEqual(sys.platform, "darwin")
        node = shutil.which("node")
        self.assertTrue(node, "macOS process-identity CI requires Node.js")
        node = os.path.realpath(node)
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "fake-pi.mjs"
            script.write_text("process.title = 'pi'; process.stdin.resume();\n")
            token = "codex-pi-title-change-token"
            proc = subprocess.Popen([node, str(script), token], stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                probe = delegate_process.ps_probe(proc.pid)
                while probe is not None and token in probe["command"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                    probe = delegate_process.ps_probe(proc.pid)
                self.assertIsNotNone(probe)
                self.assertNotIn(token, probe["command"])
                identity = PiTransport().capture_child({"pi_runtime": node}, proc.pid, token)
                self.assertTrue(identity["identity_verified"], identity)
                self.assertEqual(identity["identity_method"], "darwin_proc")
                self.assertEqual(delegate_process.identity_state(identity), "alive")
                stopped = delegate_process.terminate_recorded(identity, grace=1)
                self.assertTrue(stopped["signalled"], stopped)
                proc.wait(timeout=3)
                self.assertEqual(delegate_process.identity_state(identity), "exited")
            finally:
                if proc.stdin:
                    proc.stdin.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
