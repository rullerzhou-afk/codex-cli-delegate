"""Pure tests for the repository test runner's mode selection.

These do not spawn suites; they pin the contract that the process-identity-only
mode skips portable Python and portable JavaScript, while the other modes keep
the portable fixtures. The runner itself is exercised by every repository run.
"""
from pathlib import Path
import io
import json
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skill" / "scripts"))

import run_tests  # noqa: E402


def sample():
    return [
        {"directory": "work/portable", "files": [
            {"path": "work/portable/test_a.py", "process_identity": False},
        ]},
        {"directory": "work/identity", "files": [
            {"path": "work/identity/test_b.py", "process_identity": True},
        ]},
    ]


class Selection(unittest.TestCase):
    def test_only_process_identity_keeps_identity_and_skips_javascript(self):
        by_directory, excluded = run_tests.select_python(sample(), "only-process-identity")
        self.assertEqual(list(by_directory), ["work/identity"])
        self.assertEqual(excluded, ["work/portable/test_a.py"])
        self.assertFalse(run_tests.javascript_selected("only-process-identity"))

    def test_exclude_process_identity_keeps_portable_and_javascript(self):
        by_directory, excluded = run_tests.select_python(sample(), "exclude-process-identity")
        self.assertEqual(list(by_directory), ["work/portable"])
        self.assertEqual(excluded, ["work/identity/test_b.py"])
        self.assertTrue(run_tests.javascript_selected("exclude-process-identity"))

    def test_all_mode_selects_everything_and_javascript(self):
        by_directory, excluded = run_tests.select_python(sample(), "all")
        self.assertEqual(sorted(by_directory), ["work/identity", "work/portable"])
        self.assertEqual(excluded, [])
        self.assertTrue(run_tests.javascript_selected("all"))

    def test_repository_markers(self):
        discovered = run_tests.discover_python()
        marked = {entry["path"]: entry["process_identity"]
                  for suite in discovered for entry in suite["files"]}
        self.assertTrue(marked.get("work/skill-verification/test_blackbox_contract.py"))
        self.assertTrue(marked.get("work/skill-verification/test_transport_seam.py"))
        self.assertFalse(marked.get("work/skill-verification/test_schema_contract.py"))

    def test_javascript_discovery_finds_all_repository_files(self):
        names = {Path(path).name for path in run_tests.discover_javascript()}
        self.assertEqual(names, {"test_remote_codex.cjs", "test_remote_codex_runner.cjs",
                                 "test_opencode_hook.mjs"})

    def test_javascript_plan_reports_excluded_filenames(self):
        all_js = ["skill/tests/test_a.cjs", "skill/tests/test_b.mjs"]
        selected, excluded = run_tests.javascript_plan("only-process-identity", all_js)
        self.assertEqual(selected, [])
        self.assertEqual(excluded, all_js)
        selected, excluded = run_tests.javascript_plan("exclude-process-identity", all_js)
        self.assertEqual(selected, all_js)
        self.assertEqual(excluded, [])

    def test_all_portable_sample_makes_only_mode_vacuous(self):
        by_directory, excluded = run_tests.select_python(sample()[:1], "only-process-identity")
        self.assertEqual(by_directory, {})
        self.assertEqual(run_tests.python_selection_error(by_directory, 0), "no_python_tests_selected")

    def test_vacuous_selection_is_an_error(self):
        self.assertEqual(run_tests.python_selection_error({}, 0), "no_python_tests_selected")
        self.assertEqual(run_tests.python_selection_error({"d": ["f"]}, 0), "no_python_tests_selected")
        self.assertIsNone(run_tests.python_selection_error({"d": ["f"]}, 3))

    def test_python_nonzero_return_with_ok_text_fails(self):
        ok_output = ("-" * 70 + "\nRan 2 tests in 0.001s\n\nOK\n")
        self.assertTrue(run_tests.unittest_verdict(0, ok_output)["ok"])
        failed = run_tests.unittest_verdict(7, ok_output)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["returncode"], 7)
        self.assertEqual(failed["tests"], 2)

    def test_node_nonzero_return_with_passing_tap_fails(self):
        tap = "TAP version 13\n# tests 2\n# pass 2\n# fail 0\n"
        self.assertTrue(run_tests.tap_verdict(0, tap)["ok"])
        failed = run_tests.tap_verdict(3, tap)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["returncode"], 3)
        self.assertEqual(failed["fail"], 0)
        self.assertEqual(failed["tests"], 2)

    def test_only_process_identity_runtime_skip_fails_mode(self):
        self.assertEqual(run_tests.process_identity_skip_error("only-process-identity", 1),
                         "process_identity_tests_skipped")
        self.assertIsNone(run_tests.process_identity_skip_error("only-process-identity", 0))
        self.assertIsNone(run_tests.process_identity_skip_error("all", 1))
        self.assertIsNone(run_tests.process_identity_skip_error("exclude-process-identity", 1))
        skipped = run_tests.unittest_verdict(0, "Ran 3 tests in 0.001s\n\nOK (skipped=1)\n")
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["skipped"], 1)

    def test_missing_test_runtimes_return_structured_failures(self):
        with patch.object(run_tests.subprocess, "run", side_effect=FileNotFoundError("missing")):
            python = run_tests.run_unittest("missing-python", ["test_a.py"], {})
            node = run_tests.run_node("missing-node", ["test_a.cjs"], {})
        for result in (python, node):
            self.assertFalse(result["ok"])
            self.assertIsNone(result["returncode"])
            self.assertEqual(result["error"], "runner_unavailable")

    def test_main_still_emits_json_when_node_is_unavailable(self):
        passing = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0,
                   "ok": True, "returncode": 0}
        unavailable = {"tests": 0, "pass": 0, "fail": 0, "skipped": 0,
                       "ok": False, "returncode": None,
                       "error": "runner_unavailable", "detail": "missing"}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(run_tests, "discover_python", return_value=sample()[:1]), \
             patch.object(run_tests, "run_unittest", return_value=passing), \
             patch.object(run_tests, "discover_javascript", return_value=["test_a.cjs"]), \
             patch.object(run_tests, "run_node", return_value=unavailable), \
             patch.object(run_tests.sys, "stdout", stdout), \
             patch.object(run_tests.sys, "stderr", stderr):
            code = run_tests.main([])
        aggregate = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(aggregate["ok"])
        self.assertTrue(aggregate["python"]["ok"])
        self.assertEqual(aggregate["javascript"]["error"], "runner_unavailable")
        self.assertIn("javascript_suite_failed", aggregate["errors"])

    def test_missing_python_is_not_mislabelled_as_empty_selection(self):
        unavailable = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
                       "ok": False, "returncode": None,
                       "error": "runner_unavailable", "detail": "missing"}
        passing = {"tests": 1, "pass": 1, "fail": 0, "skipped": 0,
                   "ok": True, "returncode": 0}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(run_tests, "discover_python", return_value=sample()[:1]), \
             patch.object(run_tests, "run_unittest", return_value=unavailable), \
             patch.object(run_tests, "discover_javascript", return_value=["test_a.cjs"]), \
             patch.object(run_tests, "run_node", return_value=passing), \
             patch.object(run_tests.sys, "stdout", stdout), \
             patch.object(run_tests.sys, "stderr", stderr):
            code = run_tests.main([])
        aggregate = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertNotIn("no_python_tests_selected", aggregate["errors"])
        self.assertIn("python_suite_failed", aggregate["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
