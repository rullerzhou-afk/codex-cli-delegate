"""Job-state and evidence-manifest schema compatibility and rejection tests.

These read committed current-shape fixtures and prove that:
* old current-version job fixtures load without an on-disk rewrite;
* an accepted job stays terminal and reservation-free even with cleanup
  attention attached;
* unknown job/manifest schema versions fail closed;
* a registered N -> N+1 migration is applied in memory before the supported
  version check, without rewriting the file on read.

No model is called and no worker is launched here.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(os.environ["DELEGATE_SCRIPT"]).resolve()
sys.path.insert(0, str(SCRIPT.parent))

# Import the canonical module: extracted helpers resolve ``claude_task`` by its
# canonical name, so patches on a second under-test copy would be missed.
import claude_task as ct  # noqa: E402

import review_evidence as evidence  # noqa: E402
import delegate_job_store as store  # noqa: E402
from delegate_service import DelegateService  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures")
CURRENT_JOB = FIXTURES / "job_state_v1_current.json"
ACCEPTED_JOB = FIXTURES / "job_state_v1_accepted.json"
MANIFEST_DIR = FIXTURES / "evidence_manifest_v1"
MANIFEST = MANIFEST_DIR / "provenance.json"

class JobStateSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = ct.Context(str(self.root / "state"), owner="fixture-owner", claude_bin="/bin/false")

    def install(self, fixture, job_id):
        target = Path(self.ctx.job_dir(job_id))
        target.mkdir(parents=True)
        (target / "job.json").write_bytes(fixture.read_bytes())
        return target / "job.json"

    def test_current_fixture_loads_without_rewrite(self):
        path = self.install(CURRENT_JOB, "11111111-1111-1111-1111-111111111111")
        before = path.read_bytes()
        job = self.ctx.load("11111111-1111-1111-1111-111111111111")
        self.assertEqual(job["schema"], ct.JOB_STATE_SCHEMA_VERSION)
        self.assertEqual(job["schema_namespace"], ct.JOB_STATE_SCHEMA_NAMESPACE)
        self.assertEqual(job["phase"], "awaiting_review")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(job), set(json.loads(CURRENT_JOB.read_text())))

    def test_round_fixture_matches_new_round_shape(self):
        fixture = json.loads(CURRENT_JOB.read_text())
        produced = ct.new_round("11111111-1111-1111-1111-111111111111", 0, "initial",
                                "prompts/r000.md", "sha", [], "missing")
        # The fixture is a completed round: it must retain every field
        # new_round writes, plus the lifecycle fields added when it finishes.
        self.assertTrue(set(produced) <= set(fixture["rounds"][0]))

    def test_accepted_fixture_is_terminal_and_reservation_free(self):
        self.install(ACCEPTED_JOB, "22222222-2222-2222-2222-222222222222")
        job = self.ctx.load("22222222-2222-2222-2222-222222222222")
        self.assertEqual(job["phase"], ct.PHASE_ACCEPTED)
        self.assertNotIn(job["phase"], ct.RESERVING_PHASES)
        self.assertNotIn(job["phase"], ct.BUSY_PHASES)
        self.assertIn("attention", job)
        payload = ct.status_payload(self.ctx, job)
        self.assertFalse(payload["reservation_held"])
        self.assertEqual(payload["attention"]["reason"], "stop_incomplete")
        service = DelegateService(self.ctx.state_dir, "/bin/false")
        compact = service.compact(self.ctx, job)
        self.assertEqual(compact["next_action"], "done")
        self.assertEqual(compact["phase"], "accepted")
        listed = ct.cmd_list(self.ctx, SimpleNamespace())
        self.assertEqual(listed["jobs"][0]["backend"], "claude")

    def test_accepted_stop_with_remaining_processes_stays_reservation_free(self):
        job_id = "22222222-2222-2222-2222-222222222222"
        self.install(ACCEPTED_JOB, job_id)
        # Mock identity/termination; never signal a real or arbitrary PID.
        with patch.object(ct, "terminate_recorded",
                          return_value={"state": "unverifiable", "signalled": False}), \
             patch.object(ct, "identity_state",
                          side_effect=lambda record: "unverifiable" if record else "unknown"):
            result = ct.cmd_stop(self.ctx, SimpleNamespace(job=job_id))
        self.assertFalse(result["stopped"])
        self.assertTrue(result["reservation_released"])
        self.assertEqual(result["phase"], ct.PHASE_ACCEPTED)
        job = self.ctx.load(job_id)
        self.assertEqual(job["phase"], "accepted")
        self.assertIn("attention", job)
        self.assertNotIn("reservation retained", job["attention"]["detail"])
        self.assertIn("reservation-free", job["attention"]["detail"])
        service = DelegateService(self.ctx.state_dir, "/bin/false")
        self.assertEqual(service.compact(self.ctx, job)["next_action"], "done")

    def test_unknown_job_schema_fails_closed(self):
        path = self.install(CURRENT_JOB, "33333333-3333-3333-3333-333333333333")
        job = json.loads(path.read_text())
        job["schema"] = 999
        path.write_text(json.dumps(job))
        with self.assertRaises(ct.CliError) as error:
            self.ctx.load("33333333-3333-3333-3333-333333333333")
        self.assertEqual(error.exception.code, "unsupported_schema")
        self.assertEqual(error.exception.extra["namespace"], ct.JOB_STATE_SCHEMA_NAMESPACE)

    def test_foreign_job_namespace_fails_closed(self):
        path = self.install(CURRENT_JOB, "66666666-6666-6666-6666-666666666666")
        job = json.loads(path.read_text())
        job["schema_namespace"] = "someone-else/job-state"
        path.write_text(json.dumps(job))
        with self.assertRaises(ct.CliError) as error:
            self.ctx.load("66666666-6666-6666-6666-666666666666")
        self.assertEqual(error.exception.code, "unsupported_schema")
        self.assertEqual(error.exception.extra["namespace"], "someone-else/job-state")
        self.assertEqual(error.exception.extra["expected"], ct.JOB_STATE_SCHEMA_NAMESPACE)

    def test_invalid_schema_types_fail_closed(self):
        for index, value in enumerate((True, False, "1", 1.0, [], {}, None)):
            job_id = str(ct.uuid.UUID(int=index + 100))
            path = self.install(CURRENT_JOB, job_id)
            job = json.loads(path.read_text())
            job["schema"] = value
            path.write_text(json.dumps(job))
            with self.subTest(value=value):
                with self.assertRaises(ct.CliError) as error:
                    self.ctx.load(job_id)
                self.assertEqual(error.exception.code, "unsupported_schema")

    def test_legacy_no_namespace_v1_reads_without_rewrite(self):
        path = self.install(CURRENT_JOB, "44444444-4444-4444-4444-444444444444")
        job = json.loads(path.read_text())
        job.pop("schema_namespace")
        path.write_text(json.dumps(job))
        before = path.read_bytes()
        loaded = self.ctx.load("44444444-4444-4444-4444-444444444444")
        self.assertEqual(loaded["schema"], 1)
        self.assertNotIn("schema_namespace", loaded)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_schema_fails_closed(self):
        path = self.install(CURRENT_JOB, "77777777-7777-7777-7777-777777777777")
        job = json.loads(path.read_text())
        job.pop("schema")
        job.pop("schema_namespace")
        path.write_text(json.dumps(job))
        with self.assertRaises(ct.CliError) as error:
            self.ctx.load("77777777-7777-7777-7777-777777777777")
        self.assertEqual(error.exception.code, "unsupported_schema")

    def test_supported_version_without_namespace_fails_closed(self):
        path = self.install(CURRENT_JOB, "88888888-8888-8888-8888-888888888888")
        job = json.loads(path.read_text())
        job["schema"] = 2
        job.pop("schema_namespace")
        path.write_text(json.dumps(job))
        saved = store.JOB_STATE_SUPPORTED_VERSIONS
        store.JOB_STATE_SUPPORTED_VERSIONS = frozenset((1, 2))
        try:
            with self.assertRaises(ct.CliError) as error:
                self.ctx.load("88888888-8888-8888-8888-888888888888")
            self.assertEqual(error.exception.code, "unsupported_schema")
        finally:
            store.JOB_STATE_SUPPORTED_VERSIONS = saved

    def test_registered_migration_applies_on_read_without_rewrite(self):
        path = self.install(CURRENT_JOB, "55555555-5555-5555-5555-555555555555")

        def migrate(job):
            job = dict(job)
            job["schema"] = 2
            job["schema_namespace"] = ct.JOB_STATE_SCHEMA_NAMESPACE
            job["migrated_field"] = "from-v1"
            return job

        saved_migrations = store.JOB_STATE_MIGRATIONS
        saved_versions = store.JOB_STATE_SUPPORTED_VERSIONS
        store.JOB_STATE_MIGRATIONS = {1: migrate}
        store.JOB_STATE_SUPPORTED_VERSIONS = frozenset((2,))
        try:
            before = path.read_bytes()
            job = self.ctx.load("55555555-5555-5555-5555-555555555555")
            self.assertEqual(job["schema"], 2)
            self.assertEqual(job["migrated_field"], "from-v1")
            self.assertEqual(path.read_bytes(), before)
        finally:
            store.JOB_STATE_MIGRATIONS = saved_migrations
            store.JOB_STATE_SUPPORTED_VERSIONS = saved_versions

    def test_foreign_namespace_is_rejected_before_migration(self):
        called = []

        def migrate(job):
            called.append(True)
            return dict(job, schema=2)

        saved_migrations = store.JOB_STATE_MIGRATIONS
        try:
            store.JOB_STATE_MIGRATIONS = {1: migrate}
            with self.assertRaises(ct.CliError) as error:
                ct.migrate_job_state({"schema": 1, "schema_namespace": "other/job-state"})
            self.assertEqual(error.exception.code, "unsupported_schema")
            self.assertEqual(called, [])
        finally:
            store.JOB_STATE_MIGRATIONS = saved_migrations

    def test_migration_must_return_object_and_advance_one_version(self):
        saved_migrations = store.JOB_STATE_MIGRATIONS
        try:
            for result in (None, {"schema": 3, "schema_namespace": ct.JOB_STATE_SCHEMA_NAMESPACE}):
                with self.subTest(result=result):
                    store.JOB_STATE_MIGRATIONS = {1: lambda job, value=result: value}
                    with self.assertRaises(ct.CliError) as error:
                        ct.migrate_job_state({"schema": 1,
                                              "schema_namespace": ct.JOB_STATE_SCHEMA_NAMESPACE})
                    self.assertEqual(error.exception.code, "bad_state")
        finally:
            store.JOB_STATE_MIGRATIONS = saved_migrations

    def test_migration_exception_is_reported_as_bad_state(self):
        def broken(job):
            raise KeyError("fixture")

        saved_migrations = store.JOB_STATE_MIGRATIONS
        try:
            store.JOB_STATE_MIGRATIONS = {1: broken}
            with self.assertRaises(ct.CliError) as error:
                ct.migrate_job_state({"schema": 1,
                                      "schema_namespace": ct.JOB_STATE_SCHEMA_NAMESPACE})
            self.assertEqual(error.exception.code, "bad_state")
        finally:
            store.JOB_STATE_MIGRATIONS = saved_migrations

    def test_all_jobs_fails_closed_on_malformed_state(self):
        job_id = "99999999-9999-4999-8999-999999999999"
        target = Path(self.ctx.job_dir(job_id))
        target.mkdir(parents=True)
        (target / "job.json").write_text("{")
        with self.assertRaises(ct.CliError) as error:
            self.ctx.all_jobs()
        self.assertEqual(error.exception.code, "bad_state")
        self.assertEqual(error.exception.extra["job_id"], job_id)


class EvidenceManifestSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_current_manifest_fixture_and_namespace(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(evidence.manifest_schema(manifest), evidence.SCHEMA_VERSION)
        self.assertEqual(manifest["schema_namespace"], evidence.SCHEMA_NAMESPACE)
        before = {path.name: path.read_bytes() for path in MANIFEST_DIR.iterdir()}
        result = evidence.verify(MANIFEST_DIR)
        self.assertTrue(result["ok"])
        self.assertEqual({path.name: path.read_bytes() for path in MANIFEST_DIR.iterdir()}, before)

    def test_legacy_manifest_without_namespace_verifies_without_rewrite(self):
        target = Path(self.tmp.name) / "legacy-manifest"
        shutil.copytree(MANIFEST_DIR, target)
        manifest_path = target / "provenance.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("schema_namespace")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        before = {path.name: path.read_bytes() for path in target.iterdir()}
        self.assertTrue(evidence.verify(target)["ok"])
        self.assertEqual({path.name: path.read_bytes() for path in target.iterdir()}, before)

    def test_export_produces_the_fixture_shape(self):
        root = Path(self.tmp.name)
        source = root / "job"
        source.mkdir()
        prompt = "Fixture prompt.\n".encode()
        stream = (json.dumps({
            "type": "assistant", "session_id": "session-a",
            "message": {"content": [{"type": "text", "text": "Fixture visible response."}]},
        }) + "\n").encode()
        (source / "prompt.md").write_bytes(prompt)
        (source / "stdout.ndjson").write_bytes(stream)
        job = dict(job_id="11111111-1111-1111-1111-111111111111", owner="fixture-owner",
                   backend="claude", session_id="session-a", cwd="/example/project",
                   model="claude-opus-5", effort="max", current_round=0,
                   rounds=[dict(finalized=True, exit_code=0, prompt="prompt.md",
                                prompt_sha256=evidence.digest(prompt),
                                run_token="codexdel-fixture-r000-abc123",
                                evidence={"stdout": "stdout.ndjson"},
                                evidence_sha256={"stdout": evidence.digest(stream)},
                                verification=dict(ok=True, model_verified=True, effort_verified=True,
                                                  session_ok=True, completion_basis="process_exit",
                                                  assistant_models=["claude-opus-5"], efforts=["max"]))])
        ctx = type("Ctx", (), {"job_dir": lambda self, job: source})()
        evidence.export(ctx, job, 0, str(root / "pkg"), "fixture commit", "2026-09-15T00:00:00Z")
        produced = json.loads((root / "pkg" / "provenance.json").read_text())
        fixture = json.loads(MANIFEST.read_text())
        self.assertEqual(set(produced), set(fixture))
        self.assertEqual(produced["schema"], fixture["schema"])
        self.assertEqual(produced["schema_namespace"], fixture["schema_namespace"])

    def test_unknown_or_foreign_manifest_schema_fails_closed(self):
        manifest = json.loads(MANIFEST.read_text())
        for mutate in (lambda m: m.update(schema=2),
                       lambda m: m.update(schema=None),
                       lambda m: m.pop("schema"),
                       lambda m: m.update(schema_namespace="other/manifest"),
                       lambda m: m.update(schema=True),
                       lambda m: m.update(schema="1")):
            broken = copy.deepcopy(manifest)
            mutate(broken)
            with self.assertRaises(evidence.EvidenceError):
                evidence.manifest_schema(broken)


class ErrorVocabulary(unittest.TestCase):
    def collect(self):
        scripts = SCRIPT.parent
        codes = set()
        for path in scripts.glob("*.py"):
            codes.update(re.findall(r"CliError\(\s*['\"]([a-z_]+)['\"]", path.read_text(encoding="utf-8")))
        # The CLI backends raise through a wrapper: module-level error('code')
        # becomes CliError(code). self.error(...) is the event monitor, not a
        # CliError, so it is deliberately not collected.
        for name in ("kimi_backend.py", "opencode_backend.py", "pi_backend.py"):
            text = (scripts / name).read_text(encoding="utf-8")
            codes.update(re.findall(r"(?<![.\w])error\(\s*['\"]([a-z_]+)['\"]", text))
        return codes

    def test_documented_vocabulary_covers_literal_codes(self):
        doc = (SCRIPT.parents[2] / "docs" / "CONTRACTS.md").read_text(encoding="utf-8")
        codes = self.collect()
        self.assertTrue(codes, "no literal CliError codes found")
        # Vocabulary entries are backticked, so prose mentions do not satisfy
        # the check by accident.
        missing = sorted(code for code in codes if "`%s`" % code not in doc)
        self.assertEqual(missing, [], "CliError codes missing from CONTRACTS.md: %s" % missing)

    def test_monitor_error_calls_are_not_treated_as_clierror(self):
        codes = self.collect()
        for monitor_code in ("opencode_error", "tool_result", "kimi_retry", "pi_retry"):
            self.assertNotIn(monitor_code, codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
