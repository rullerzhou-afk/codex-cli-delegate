"""Persisted job/round access and job-state schema policy.

This is the only place that knows how a job is laid out on disk, how a job id
maps to a path under the state root, and how the job-state schema is validated
and migrated. It holds no process control and no provider invocation.

The job-state schema namespace/version constants and the migration registry are
defined here, together with the reader that enforces them. ``claude_task``
re-exports them for the existing public surface; callers/tests that mutate the
migration registry mutate it on this module.
"""
from __future__ import annotations

import os

from delegate_ownership import validate_owner
from delegate_core import CliError

# Job-state schema namespace and versions. Deliberately distinct from the
# evidence-manifest namespace in review_evidence.py: a change to one never
# implies a change to the other. Both are versioned and fail closed on unknown
# versions. These live with the reader that enforces them; claude_task
# re-exports them for the existing public surface.
JOB_STATE_SCHEMA_NAMESPACE = "codex-cli-delegate/job-state"
JOB_STATE_SCHEMA_VERSION = 1
JOB_STATE_SUPPORTED_VERSIONS = frozenset((JOB_STATE_SCHEMA_VERSION,))
# Pre-namespace v1 documents still have ``schema: 1`` but no
# ``schema_namespace``. They are read as this version and gain the namespace
# only when a later save persists them. A missing version or a document that
# declares a different namespace always fails closed.
JOB_STATE_LEGACY_NO_NAMESPACE_VERSION = 1

# Registry of {from_version: migrate_fn(job) -> job}. Adding or changing a
# persisted job-state field REQUIRES an N -> N+1 entry here plus a regenerated
# post-migration fixture. The read path applies migrations before the
# supported-version check, in memory; a read never rewrites the job file.
JOB_STATE_MIGRATIONS = {}


def default_state_dir():
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(codex_home, "claude-delegate")


def migrate_job_state(job):
    """Apply registered N -> N+1 migrations in memory, then return the job.

    A version with no registered migration is returned untouched so the
    supported-version check decides whether it is acceptable. A cycle or a
    migration that does not advance exactly one version is refused.
    """
    if not isinstance(job, dict):
        raise CliError("bad_state", "job state is not a JSON object")
    validate_job_namespace(job)
    version = job.get("schema")
    seen = set()
    while type(version) is int and version in JOB_STATE_MIGRATIONS:
        if version in seen:
            raise CliError("bad_state", "job-state migration loop at schema %r" % (version,))
        seen.add(version)
        next_version = version + 1
        try:
            job = JOB_STATE_MIGRATIONS[version](job)
        except CliError:
            raise
        except Exception as exc:
            raise CliError("bad_state", "job-state migration failed: %s" % type(exc).__name__)
        if not isinstance(job, dict):
            raise CliError("bad_state", "job-state migration did not return an object")
        validate_job_namespace(job)
        version = job.get("schema")
        if type(version) is not int or version != next_version:
            raise CliError(
                "bad_state",
                "job-state migration must advance exactly one version",
                expected_schema=next_version,
                actual_schema=version,
            )
    return job


def validate_job_namespace(job):
    """Reject a declared foreign namespace before any migration runs."""
    namespace = job.get("schema_namespace")
    if namespace is not None and namespace != JOB_STATE_SCHEMA_NAMESPACE:
        raise CliError(
            "unsupported_schema",
            "foreign job-state schema namespace",
            schema=job.get("schema"),
            namespace=namespace,
            expected=JOB_STATE_SCHEMA_NAMESPACE,
        )


def validate_job_schema(job):
    """Fail closed unless the job carries a supported job-state schema.

    A document that declares a different ``schema_namespace`` is refused. An
    absent namespace is tolerated only for the legacy pre-namespace v1 shape
    and is left untouched on read; any present but unsupported version or type
    is refused so a newer or foreign writer is never silently misinterpreted.
    """
    validate_job_namespace(job)
    namespace = job.get("schema_namespace")
    version = job.get("schema")
    if type(version) is not int or version not in JOB_STATE_SUPPORTED_VERSIONS:
        raise CliError(
            "unsupported_schema",
            "unsupported job-state schema version",
            schema=job.get("schema"),
            namespace=JOB_STATE_SCHEMA_NAMESPACE,
            supported=sorted(JOB_STATE_SUPPORTED_VERSIONS),
        )
    if namespace is None and version != JOB_STATE_LEGACY_NO_NAMESPACE_VERSION:
        raise CliError(
            "unsupported_schema",
            "job state is missing its schema namespace",
            schema=version,
            namespace=None,
            expected=JOB_STATE_SCHEMA_NAMESPACE,
        )
    return version


class Context:
    def __init__(self, state_dir, owner=None, claude_bin=None):
        import claude_task as ct
        self.state_dir = os.path.abspath(os.path.expanduser(state_dir))
        ct.ensure_dir(self.state_dir)
        self.jobs_dir = ct.ensure_dir(os.path.join(self.state_dir, "jobs"))
        self.real_jobs_dir = os.path.realpath(self.jobs_dir)
        self.owner_raw = owner
        self.claude_bin_raw = claude_bin

    def owner(self):
        return validate_owner(self.owner_raw)

    def job_dir(self, job_id):
        import claude_task as ct
        ct.require_uuid(job_id, "job id")
        path = os.path.join(self.jobs_dir, job_id)
        # A symlinked job directory would let a write escape the state root.
        if os.path.islink(path):
            raise CliError("unsafe_path", "job directory is a symlink: %s" % job_id)
        if os.path.realpath(path) != os.path.join(self.real_jobs_dir, job_id):
            raise CliError("unsafe_path", "job directory resolves outside the state root")
        return path

    def job_file(self, job_id):
        return os.path.join(self.job_dir(job_id), "job.json")

    def load(self, job_id):
        import claude_task as ct
        path = self.job_file(job_id)
        if not os.path.isfile(path):
            raise CliError("not_found", "no such job: %s" % job_id)
        try:
            job = ct.read_json(path)
        except (OSError, ValueError) as exc:
            raise CliError("bad_state", "unreadable job state: %s" % exc)
        job = migrate_job_state(job)
        validate_job_schema(job)
        return job

    def save(self, job):
        import claude_task as ct
        # Disk-loaded jobs pass validate_job_schema before reaching this point.
        # The defaults also support new in-process objects built by internal
        # callers and tests; they are not a compatibility path for schema-less
        # files on disk. A legacy v1 object gains the namespace on its next save.
        job.setdefault("schema", JOB_STATE_SCHEMA_VERSION)
        job.setdefault("schema_namespace", JOB_STATE_SCHEMA_NAMESPACE)
        job["updated_at"] = ct.iso()
        ct.write_json(self.job_file(job["job_id"]), job)

    def load_owned(self, job_id):
        job = self.load(job_id)
        owner = self.owner()
        if job.get("owner") != owner:
            # Addressing is per-owner so two Codex tasks cannot reach into each
            # other's jobs by accident.  Not a multi-user security boundary.
            raise CliError("forbidden", "job %s is not owned by %s" % (job_id, owner))
        return job

    def all_jobs(self):
        import claude_task as ct
        out = []
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except OSError:
            return out
        for name in names:
            if not ct.valid_uuid(name):
                continue
            path = os.path.join(self.jobs_dir, name)
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            job_file = os.path.join(path, "job.json")
            if not os.path.isfile(job_file):
                # A crash before the first save can leave an empty directory,
                # but no persisted reservation exists in that case.
                continue
            # Use the same reader as direct job operations. A malformed,
            # foreign, or newer state may still represent a reservation, so
            # enumeration fails closed instead of silently starting over it.
            try:
                out.append(self.load(name))
            except CliError as exc:
                extra = dict(exc.extra)
                extra.setdefault("job_id", name)
                raise CliError(exc.code, exc.message, **extra)
        return out


def round_dir(job_root, index):
    return os.path.join(job_root, "rounds", "r%03d" % index)


def prompt_path(job_root, index):
    return os.path.join(job_root, "prompts", "r%03d.md" % index)


def current_round(job):
    index = job.get("current_round", 0)
    rounds = job.get("rounds") or []
    if 0 <= index < len(rounds):
        return index, rounds[index]
    return index, {}
