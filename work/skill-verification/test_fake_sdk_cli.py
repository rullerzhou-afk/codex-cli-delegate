"""The fake SDK CLI answers the pinned SDK's version probe deterministically.

Fixture fidelity/test-speed only: a real CLI answers ``-v``, so this avoids a
wasted stdin-reading child during connect(). It is unrelated to the
restricted-sandbox process-identity refusal.
"""
from pathlib import Path
import subprocess
import sys
import unittest

FAKE = Path(__file__).resolve().parents[2] / "skill" / "tests" / "fake_sdk_cli.py"


class FakeSdkCliVersion(unittest.TestCase):
    def test_version_probe_forms(self):
        for flag in ("-v", "--version"):
            with self.subTest(flag=flag):
                proc = subprocess.run([sys.executable, str(FAKE), flag],
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(proc.stdout.startswith("2.1.261"), proc.stdout)
        malformed = subprocess.run([sys.executable, str(FAKE), "-v", "--unexpected"],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("unsupported simulated version-probe", malformed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
