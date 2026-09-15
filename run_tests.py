#!/usr/bin/env python3
"""Run every Python and JavaScript test in this repository.

Emits exactly one machine-readable JSON aggregate on stdout (and, with
``--json-out``, to a file). A short human summary goes to stderr so stdout
stays parseable.

Usage::

    /path/to/.venv/bin/python run_tests.py
    /path/to/.venv/bin/python run_tests.py --exclude-process-identity

Modules that launch detached workers or inspect local process identity declare
``PROCESS_IDENTITY = True``. The portable Linux CI job excludes them; the
separately labelled macOS CI job runs only them. Together they cover the full
suite, and the aggregate records exactly what was excluded.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".github", ".pytest_cache"}
PROCESS_IDENTITY_RE = re.compile(r"^\s*PROCESS_IDENTITY\s*=\s*True\s*$", re.M)
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)
COUNT_RE = re.compile(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")


def _is_skipped(parts):
    return any(part in SKIP_DIRS or part.startswith(".") for part in parts)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_python():
    grouped = {}
    for path in ROOT.rglob("test_*.py"):
        relative = path.relative_to(ROOT)
        if _is_skipped(relative.parent.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        grouped.setdefault(str(relative.parent), []).append({
            "path": str(relative),
            "process_identity": bool(PROCESS_IDENTITY_RE.search(text)),
        })
    return [{"directory": directory, "files": sorted(grouped[directory], key=lambda f: f["path"])}
            for directory in sorted(grouped)]


def discover_javascript():
    found = set()
    for pattern in ("test_*.cjs", "test_*.mjs"):
        for path in ROOT.rglob(pattern):
            relative = path.relative_to(ROOT)
            if _is_skipped(relative.parent.parts):
                continue
            found.add(str(relative))
    return sorted(found)


def select_python(discovered, mode):
    """Split discovered Python modules into selected and excluded paths.

    Pure, so the runner's selection can be tested without spawning a suite.
    """
    selected, excluded = [], []
    for suite in discovered:
        for entry in suite["files"]:
            if mode == "exclude-process-identity" and entry["process_identity"]:
                excluded.append(entry["path"])
            elif mode == "only-process-identity" and not entry["process_identity"]:
                excluded.append(entry["path"])
            else:
                selected.append((suite["directory"], entry))
    by_directory = {}
    for directory, entry in selected:
        by_directory.setdefault(directory, []).append(entry["path"])
    return by_directory, excluded


def javascript_selected(mode):
    """The current JavaScript tests are portable fixtures.

    They run in the default and exclude-process-identity modes and are skipped
    in the process-identity-only job so the two CI jobs do not duplicate them.
    """
    return mode != "only-process-identity"


def javascript_plan(mode, all_js):
    """Return (selected, excluded) JavaScript files for the mode."""
    if javascript_selected(mode):
        return list(all_js), []
    return [], list(all_js)


def python_selection_error(by_directory, total_tests):
    """A mode that selects no Python suites is a vacuous green and must fail."""
    if not by_directory or total_tests <= 0:
        return "no_python_tests_selected"
    return None


def process_identity_skip_error(mode, skipped):
    """The process-identity-only job must never go green with runtime skips.

    A test that skips at runtime (for example an unavailable platform or a
    missing capability) is not evidence that process identity passed, so the
    mode fails with a machine-readable error instead of a silent green.
    """
    if mode == "only-process-identity" and skipped > 0:
        return "process_identity_tests_skipped"
    return None


def parse_unittest_output(output):
    ran = RAN_RE.search(output)
    tests = int(ran.group(1)) if ran else 0
    counts = {name: int(value) for name, value in COUNT_RE.findall(output)}
    ok = bool(re.search(r"^OK\b", output, re.M))
    return {
        "tests": tests,
        "failures": counts.get("failures", 0),
        "errors": counts.get("errors", 0),
        "skipped": counts.get("skipped", 0),
        "ok": ok,
    }


def parse_tap(output):
    def field(name):
        match = re.search(r"^(?:#|\u2139)\s*%s\s+(\d+)\s*$" % name, output, re.M)
        return int(match.group(1)) if match else 0
    tests, passed, failed = field("tests"), field("pass"), field("fail")
    return {"tests": tests, "pass": passed, "fail": failed,
            "skipped": field("skipped"), "ok": failed == 0 and tests > 0}


def unittest_verdict(returncode, output):
    """A subprocess's exit status participates in the verdict.

    Parseable "OK" text is not enough: a Python suite that exits nonzero must
    fail even if its captured output happens to contain "OK".
    """
    result = parse_unittest_output(output)
    result["ok"] = bool(result["ok"]) and returncode == 0
    result["returncode"] = returncode
    return result


def tap_verdict(returncode, output):
    """Node's exit status participates in the verdict, like the Python path."""
    result = parse_tap(output)
    result["ok"] = bool(result["ok"]) and returncode == 0
    result["returncode"] = returncode
    return result


def run_unittest(python, files, env):
    command = [python, "-m", "unittest"] + list(files)
    try:
        proc = subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True, text=True)
    except OSError as exc:
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
                "ok": False, "returncode": None, "error": "runner_unavailable",
                "detail": str(exc)}
    output = proc.stdout + proc.stderr
    result = unittest_verdict(proc.returncode, output)
    if not result["ok"]:
        result["output_tail"] = output[-4000:]
    return result


def run_node(node, files, env):
    command = [node, "--test", "--test-reporter=tap"] + list(files)
    try:
        proc = subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True, text=True)
    except OSError as exc:
        return {"tests": 0, "pass": 0, "fail": 0, "skipped": 0,
                "ok": False, "returncode": None, "error": "runner_unavailable",
                "detail": str(exc)}
    output = proc.stdout + proc.stderr
    result = tap_verdict(proc.returncode, output)
    if not result["ok"]:
        result["output_tail"] = output[-4000:]
    return result


def build_env():
    env = dict(os.environ)
    scripts = str(ROOT / "skill" / "scripts")
    env["PYTHONPATH"] = scripts + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["DELEGATE_SCRIPT"] = str(ROOT / "skill" / "scripts" / "claude_task.py")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=os.environ.get("CLAUDE_DELEGATE_PYTHON") or sys.executable)
    parser.add_argument("--node", default=os.environ.get("CLAUDE_DELEGATE_NODE") or "node")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--exclude-process-identity", action="store_true",
                      help="run portable fixture Python modules and portable JavaScript (the Linux CI job)")
    mode.add_argument("--only-process-identity", action="store_true",
                      help="run only process-identity Python modules; portable JavaScript is skipped (the macOS CI job)")
    parser.add_argument("--json-out", default=None, help="also write the aggregate JSON here")
    args = parser.parse_args(argv)

    started = time.monotonic()
    started_at = utcnow()
    env = build_env()
    discovered = discover_python()
    mode_name = ("exclude-process-identity" if args.exclude_process_identity
                 else "only-process-identity" if args.only_process_identity else "all")
    by_directory, excluded = select_python(discovered, mode_name)

    python_suites = []
    python_totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for directory in sorted(by_directory):
        files = by_directory[directory]
        result = run_unittest(args.python, files, env)
        result["directory"] = directory
        result["files"] = files
        for key in python_totals:
            python_totals[key] += result.get(key, 0)
        python_suites.append(result)

    all_js = discover_javascript()
    js_files, js_excluded = javascript_plan(mode_name, all_js)
    js_run = bool(js_files)
    javascript = {"tests": 0, "pass": 0, "fail": 0, "skipped": 0, "ok": True,
                  "returncode": None, "error": None,
                  "files": [], "ran": js_run, "excluded": not javascript_selected(mode_name),
                  "excluded_files": js_excluded}
    if js_run:
        result = run_node(args.node, js_files, env)
        for key in ("tests", "pass", "fail", "skipped"):
            javascript[key] = result.get(key, 0)
        javascript["files"] = js_files
        javascript["ok"] = result["ok"]
        javascript["returncode"] = result.get("returncode")
        javascript["error"] = result.get("error")
        if result.get("detail"):
            javascript["detail"] = result["detail"]
        if not result["ok"]:
            javascript["output_tail"] = result.get("output_tail", "")

    python_ok = all(suite["ok"] for suite in python_suites)
    python_runner_unavailable = any(suite.get("error") == "runner_unavailable"
                                    for suite in python_suites)
    selection_error = (None if python_runner_unavailable else
                       python_selection_error(by_directory, python_totals["tests"]))
    skip_error = process_identity_skip_error(mode_name, python_totals["skipped"])
    errors = [error for error in (selection_error, skip_error) if error]
    if not python_ok:
        errors.append("python_suite_failed")
    if not javascript["ok"]:
        errors.append("javascript_suite_failed")
    aggregate_ok = python_ok and javascript["ok"] and not errors
    total_tests = python_totals["tests"] + javascript["tests"]
    total_failed = python_totals["failures"] + python_totals["errors"] + javascript["fail"]

    aggregate = {
        "ok": aggregate_ok,
        "started_at": started_at,
        "duration_s": round(time.monotonic() - started, 3),
        "command_mode": mode_name,
        "python": {
            "ok": python_ok and selection_error is None and skip_error is None,
            "selected": bool(by_directory),
            "tests": python_totals["tests"],
            "failures": python_totals["failures"],
            "errors": python_totals["errors"],
            "skipped": python_totals["skipped"],
            "suites": python_suites,
        },
        "javascript": javascript,
        "total": {"tests": total_tests, "failed": total_failed, "ok": aggregate_ok},
        "errors": errors,
        "process_identity": {
            "mode": mode_name,
            "excluded": bool(args.exclude_process_identity or args.only_process_identity),
            "excluded_modules": excluded,
            "excluded_javascript": javascript["excluded"],
            "excluded_javascript_files": js_excluded,
            "skipped_tests": python_totals["skipped"] if mode_name == "only-process-identity" else 0,
        },
    }

    rendered = json.dumps(aggregate, indent=2, ensure_ascii=False)
    if args.json_out:
        Path(args.json_out).write_text(rendered + "\n", encoding="utf-8")
    sys.stdout.write(rendered + "\n")
    sys.stderr.write("python=%d js=%d%s failed=%d mode=%s ok=%s\n" % (
        python_totals["tests"], javascript["tests"],
        " (skipped)" if javascript["excluded"] else "",
        total_failed, mode_name, aggregate["ok"]))
    return 0 if aggregate["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
