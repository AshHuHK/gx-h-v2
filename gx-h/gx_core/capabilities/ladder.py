"""The probe ladder: what the backend observes about a capability, rung by rung.

    L0  files       code, weights and declared data exist where the manifest resolves to
    L1  metadata    the interpreter's package metadata meets the environment's requirements
    L2  entrypoint  the entry point answers under the launch recipe
    ---- an independent APPROVE review; the asset is REVIEWED ----
    L3  smoke       a smoke job ran through the job service and its output was accepted
    ERR             a malformed input is an input error
    L4  known answer  the smoke job's output, scored under a frozen specification against
                    another implementation or a reference

Two lines are drawn across it, and they are the point of the design.

L0 and L1 look at files and package metadata. A pinned local entry's bounded help/version
command may answer before review; other entrypoint commands wait for review. This is an
observation of the entry, not scientific qualification or approval to run a model.

Nothing launches a job before review. L3 and above go through the job service like
any other job: detached runner, limits from the manifest, the GPU gate, a budget.
Timeout, cancellation and idempotency belong to that service and its software tests;
onboarding does not repeat slow jobs for every scientific adapter.

:func:`advance` climbs as far as it may now and records what it saw in ``probe_records``.
It is resumable: a smoke job still running is reported as running, and the next call
picks it up. The eight checks of ``contracts/gates.json`` (:mod:`.checks`) read those
records; they launch nothing, so a gate can be asked for again at any time.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from .. import config, contracts
from ..context import Ctx
from ..domain import ids
from ..errors import CapabilityNotApproved, GxError, SchemaInvalid, ScopeDenied
from ..persistence import records, store
from ..registry import Subject
from . import environments, procs, registry, storage
from . import manifest as manifest_mod

PASS, FAIL, ERROR = "PASS", "FAIL", "ERROR"
for _name in (PASS, FAIL, ERROR):
    if _name not in contracts.gates()["check_result"]["values"]:  # pragma: no cover
        raise RuntimeError(f"check result {_name!r} left the contract")

L0, L1, L2, L3, L4 = registry.RUNGS
ERR = registry.ERROR_PROBE

PROBE_PROJECT = "gx:capability-probes"
PROBE_INPUT_KIND = "probe_input"

#: Asset states in which a job may be launched for the ladder.
JOB_STATES = ("REVIEWED", "VALIDATED", "APPROVED", "ACTIVE")

#: Bound fixture size separately for inline content and existing local files.
MAX_INLINE_INPUT_BYTES = 8 * 1024 * 1024
MAX_PATH_INPUT_BYTES = 64 * 1024 * 1024
DEFAULT_WAIT_SECONDS = 45.0


# ------------------------------------------------------------------------- the two lines

def subject_of(view: Mapping[str, Any]) -> Subject:
    return Subject(kind="asset", id=view["capability_id"], sha256=view["manifest_sha256"],
                   version=view["version"])


def review_state(conn: sqlite3.Connection, view: Mapping[str, Any]) -> dict[str, Any]:
    """The review the REVIEWED gate needs, as the governance engine reads it."""
    from ..governance import gate

    report = gate.evaluate(conn, kind=registry.ASSET_KIND, subject=subject_of(view),
                           to_state="REVIEWED", manifest=view["manifest"])
    return dict(report["review"] or {"met": False, "state": "NO_REVIEW"})


def require_reviewed(conn: sqlite3.Connection, view: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse to run bundled code of a version nobody independent has approved."""
    state = review_state(conn, view)
    if not state.get("met"):
        raise CapabilityNotApproved(
            f"{view['capability_version']} has no independent APPROVE review of sha256 "
            f"{view['manifest_sha256'][:16]}… ({state.get('state')}); nothing it bundles runs "
            "before one",
            details={"capability_version": view["capability_version"], "reason": "not_reviewed",
                     "review": state},
        )
    return state


# --------------------------------------------------------------------------------- L0, L1

def probe_files(conn: sqlite3.Connection, view: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """L0. Existence, and for the bound scripts the bytes. Executes nothing."""
    manifest, bundle = view["manifest"], view["bundle"]
    missing: list[str] = []
    seen: dict[str, Any] = {"weights": 0, "required_data": 0, "scripts": 0}
    try:
        env = environments.get(conn, view["env_id"])
    except GxError as exc:
        return FAIL, {"reason": exc.message}
    env_view = manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                    binaries=dict(env["binary_paths"]), prefix=env["prefix"])

    head = manifest_mod.placeholders(manifest["entry"]["argv_template"][0])[0]
    program = env_view.python if head == "env.python" else env_view.binaries.get(head[len("env.bin."):])
    if not program:
        missing.append(f"the environment records no executable for {{{head}}}")
    elif not (Path(program).is_file() and _executable(program)):
        missing.append(f"program {program} does not exist or is not executable")
    seen["program"] = program

    def resolved(template: str, what: str, *, directory: bool | None = None) -> Path | None:
        try:
            path = manifest_mod.resolve_path(template, env=env_view)
        except GxError as exc:
            missing.append(f"{what}: {exc.message}")
            return None
        if not path.exists():
            missing.append(f"{what} {path} does not exist")
        elif directory is True and not path.is_dir():
            missing.append(f"{what} {path} is not a directory")
        elif directory is False and not path.is_file():
            missing.append(f"{what} {path} is not a file")
        return path

    source = resolved(manifest["source"]["path"], "source.path")
    bound = manifest["source"].get("sha256")
    if bound and source is not None and source.is_file():
        observed = ids.sha256_of_file(source)
        seen["source_sha256"] = observed
        if observed != bound:
            missing.append(f"source.path {source} hashes to {observed[:16]}…, the manifest binds "
                           f"{bound[:16]}…; a changed program is a new version")
    for index, weight in enumerate(manifest["weights"]):
        if resolved(weight["path"], f"weights[{index}]", directory=False) is not None:
            seen["weights"] += 1
    for index, item in enumerate(manifest["required_data"]):
        if resolved(item, f"required_data[{index}]") is not None:
            seen["required_data"] += 1
    for index, item in enumerate(manifest["entry"].get("pythonpath") or []):
        resolved(item, f"entry.pythonpath[{index}]", directory=True)
    cwd = manifest["entry"].get("cwd")
    if cwd and "{out.dir}" not in cwd:
        resolved(cwd, "entry.cwd", directory=True)
    for name in ("entry_script", "parser"):
        ref = bundle.get(name)
        if ref is None:
            continue
        try:
            manifest_mod.script_path(ref)
            seen["scripts"] += 1
        except GxError as exc:
            missing.append(f"{name}: {exc.message}")
    seen["missing"] = missing
    return (PASS if not missing else FAIL), seen


def _executable(path: str) -> bool:
    import os

    return os.access(path, os.X_OK)


def probe_metadata(conn: sqlite3.Connection, view: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """L1. The environment's own observation of its requirements, taken on this host."""
    try:
        env = environments.get(conn, view["env_id"])
    except GxError as exc:
        return FAIL, {"reason": exc.message}
    state = environments.readiness(env)
    requirements = (env.get("probe") or {}).get("requirements") or {}
    unmet = sorted(name for name, entry in requirements.items() if not entry.get("met"))
    detail = {"env_id": env["env_id"], "environment_status": env["status"],
              "probed_at": env.get("probed_at"), "packages": len(requirements), "unmet": unmet,
              "requirements": requirements, "readiness": state["reasons"]}
    if any(reason.startswith(("never_probed", "probed_on_another_host")) for reason in state["reasons"]):
        return ERROR, {**detail, "reason": "the environment has no observation taken on this host"}
    return (PASS if not unmet else FAIL), detail


# ------------------------------------------------------------------------------------- L2

def metadata_probe_allowed(view: Mapping[str, Any]) -> bool:
    """Whether the bound local recipe declares only a help/version observation.

    Reuse the entry recipe and source/script identity already in the registry. There
    is no separate trusted-tool list and this never admits a Job or a stdio server.
    """
    from ..jobs import adapter as adapter_mod

    manifest, bundle = view["manifest"], view["bundle"]
    if manifest["kind"] == manifest_mod.STDIO_KIND or not manifest["source"].get("sha256"):
        return False
    parser = bundle.get("parser")
    if not parser:
        return False
    try:
        spec = adapter_mod.literal(parser, "ENTRYPOINT_PROBE")
    except (ValueError, SyntaxError, GxError):
        return False
    if not isinstance(spec, dict):
        return False
    argv = spec.get("argv")
    if (manifest_mod.probe_argv_problems(manifest, argv) or len(argv) < 2
            or argv[-1] not in ("--help", "--version", "-h", "-V")
            or "-c" in argv or "-m" in argv):
        return False
    return argv[:-1] == manifest["entry"]["argv_template"][:len(argv) - 1]


def probe_entrypoint(conn: sqlite3.Connection, view: Mapping[str, Any], work_dir: Path) -> tuple[str, dict[str, Any]]:
    """L2. Run the entry point as the adapter's literal says it can be asked to answer."""
    from ..jobs import adapter as adapter_mod

    parser = view["bundle"].get("parser")
    if parser is None:
        return FAIL, {"reason": "the bundle binds no adapter module, so nothing says how its "
                                "entry point can be asked to answer"}
    try:
        spec = adapter_mod.literal(parser, "ENTRYPOINT_PROBE")
    except (ValueError, SyntaxError) as exc:
        return FAIL, {"reason": f"ENTRYPOINT_PROBE is not a literal: {exc}"}
    if not isinstance(spec, dict):
        return FAIL, {"reason": "the adapter module assigns no ENTRYPOINT_PROBE literal"}
    argv = spec.get("argv")
    problems = manifest_mod.probe_argv_problems(view["manifest"], argv)
    codes = spec.get("ok_exit_codes", [0])
    if (not isinstance(codes, list) or not codes
            or not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in codes)):
        problems.append("ENTRYPOINT_PROBE.ok_exit_codes must be a non-empty list of exit codes")
    expect = spec.get("expect")
    if expect is not None:
        try:
            re.compile(expect)
        except (re.error, TypeError):
            problems.append("ENTRYPOINT_PROBE.expect is not a regular expression")
    ceiling = float(config.engineering_budget()["ordinary_test_timeout_seconds"])
    timeout = spec.get("timeout_seconds", config.engineering_budget()["metadata_command_timeout_seconds"])
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= ceiling:
        problems.append(f"ENTRYPOINT_PROBE.timeout_seconds must be a number in (0, {ceiling:g}]")
    if problems:
        return FAIL, {"reason": problems[0], "problems": problems}

    env = environments.require_ready(conn, view["env_id"])
    env_view = manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                    binaries=dict(env["binary_paths"]), prefix=env["prefix"])
    work_dir.mkdir(parents=True, exist_ok=True)
    plan = manifest_mod.launch_plan(
        view["bundle"], env_view, argv_template=argv, out_dir=str(work_dir),
        home_dir=str(work_dir), tmp_dir=str(work_dir),
    )
    run = procs.run_bounded(plan.argv, timeout=float(timeout), env=plan.env, cwd=plan.cwd)
    text = (run.get("stdout") or "") + "\n" + (run.get("stderr") or "")
    answered = bool(run.get("ran") and run.get("exit_code") in codes)
    matched = True if expect is None else bool(re.search(expect, text))
    detail = {
        "argv": plan.argv, "cwd": plan.cwd, "exit_code": run.get("exit_code"),
        "ok_exit_codes": codes, "seconds": run.get("seconds"), "reason": run.get("reason"),
        "expect": expect, "expect_matched": matched, "output_head": text.strip()[:300],
        "env_names": sorted(plan.env),
    }
    return (PASS if answered and matched else FAIL), detail


# ------------------------------------------------------------------------- probe requests

def probe_run(ctx: Ctx, view: Mapping[str, Any]) -> str:
    """Use the owning run's existing budget for any authored capability version.

    Public admission already establishes the caller/task. The JobService retains
    ownership, budget and launch checks; no per-tool Native identity ladder is added.
    A standalone probe uses its authorized project. Unscoped imports retain the
    original shared probe project.
    """
    from .. import budgets

    conn = ctx.conn
    asset = conn.execute("SELECT content_sha256, source_json FROM assets"
                         " WHERE asset_id=? AND version=? AND kind=?",
                         (view.get("capability_id"), view.get("version"), registry.ASSET_KIND)).fetchone()
    source_run = (store.loads(asset, "source_json", {}) or {}).get("run_id") if asset else None
    run_id = ctx.authz.get("run_id") or source_run
    if run_id:
        parent = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if (parent is None or asset is None
                or (asset and asset["content_sha256"] != view["manifest_sha256"])
                or not ctx.principal.may_access_project(parent["project_ref"])
                or (ctx.authz.get("project_ref") is not None
                    and ctx.authz["project_ref"] != parent["project_ref"])
                or parent["status"] not in contracts.states()["machines"]["run"]["executable"]
                or not parent["budget_ref"]
                or conn.execute("SELECT 1 FROM budgets WHERE budget_ref=?", (parent["budget_ref"],)).fetchone() is None):
            raise ScopeDenied("capability probes require their owning executable run and existing budget")
        return run_id
    objective = (f"probe ladder of {view['capability_version']} at sha256 "
                 f"{view['manifest_sha256']}")
    project = ctx.authz.get("project_ref")
    if project is None:
        if ctx.principal.allows_all_projects:
            project = PROBE_PROJECT
        elif len(ctx.principal.project_refs) == 1:
            project = ctx.principal.project_refs[0]
        else:
            raise ScopeDenied("standalone capability probes need one authorized project or an owning run")
    if not ctx.principal.may_access_project(project):
        raise ScopeDenied("the capability probe project is outside the caller's scope")
    with store.transaction(conn) as txn:
        row = txn.execute("SELECT run_id FROM runs WHERE project_ref=? AND objective=?"
                          " AND workflow_id IS NULL ORDER BY rowid LIMIT 1", (project, objective)).fetchone()
        if row is not None:
            return row["run_id"]
        inputs = {}
        budget_ref = budgets.create_root(txn, run_id=None, actor=ctx.actor)
        return records.create_run(txn, objective=objective, project_ref=project,
                                  actor=ctx.actor, budget_ref=budget_ref, inputs=inputs)


def materialise(ctx: Ctx, run_id: str, view: Mapping[str, Any], request: Any, *, label: str,
                selected_inputs: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Turn an adapter's request into artifacts and an input manifest the job service takes."""
    if not isinstance(request, dict) or not isinstance(request.get("inputs"), dict):
        raise SchemaInvalid(f"{label}: an adapter request is an object with 'inputs' and 'parameters'",
                            details={"label": label})
    parameters = request.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise SchemaInvalid(f"{label}: 'parameters' must be an object", details={"label": label})
    artifacts = storage.artifact_service(ctx.conn)
    namespace = f"{run_id}/probe-inputs"
    entries = []
    for role, item in sorted((request["inputs"] if selected_inputs is None else selected_inputs).items()):
        if selected_inputs is not None:
            entries.append({"role": role, "artifact_ref": item})
            continue
        if not isinstance(role, str) or not manifest_mod.NAME.fullmatch(role) or not isinstance(item, dict):
            raise SchemaInvalid(f"{label}: input {role!r} is not a named object", details={"label": label})
        if "text" in item:
            data = str(item["text"]).encode("utf-8")
            filename = str(item.get("filename") or f"{role}.txt")
            if len(data) > MAX_INLINE_INPUT_BYTES:
                raise SchemaInvalid(f"{label}: inline input {role!r} is larger than "
                                    f"{MAX_INLINE_INPUT_BYTES} bytes; name a file by path instead",
                                    details={"label": label, "role": role})
        elif "path" in item:
            path = manifest_mod.resolve_path(str(item["path"]))
            if not path.is_file() or path.stat().st_size > MAX_PATH_INPUT_BYTES:
                raise SchemaInvalid(f"{label}: input {role!r} at {path} is not a file of at most "
                                    f"{MAX_PATH_INPUT_BYTES} bytes", details={"label": label, "role": role})
            data = path.read_bytes()
            declared = item.get("sha256")
            observed = hashlib.sha256(data).hexdigest()
            if declared != observed:
                raise SchemaInvalid(
                    f"{label}: input {role!r} at {path} hashes to {observed[:16]}… and the adapter "
                    f"declares {str(declared)[:16]}…; a probe input named by path carries its sha256",
                    details={"label": label, "role": role, "observed_sha256": observed})
            filename = str(item.get("filename") or path.name)
        else:
            raise SchemaInvalid(f"{label}: input {role!r} has neither 'text' nor 'path'",
                                details={"label": label, "role": role})
        if "/" in filename or filename in ("", ".", ".."):
            raise SchemaInvalid(f"{label}: {filename!r} is not a file name", details={"label": label})
        digest = hashlib.sha256(data).hexdigest()
        published = artifacts.publish_bytes(
            namespace=namespace, rel_path=f"{digest[:16]}/{filename}", kind=PROBE_INPUT_KIND,
            data=data, run_id=run_id, actor=ctx.actor, expected_sha256=digest,
            provenance={"label": label, "capability_version": view["capability_version"]},
        )
        entries.append({"role": role, "artifact_ref": published.artifact_ref})
    # The same request asked for twice is the same manifest: the idempotency probe depends
    # on it, and an input manifest carries its creation time, so it is looked up, not remade.
    rel_path = f"manifests/{label}-{ids.sha256_of_obj(entries)[:16]}.json"
    existing = ctx.conn.execute(
        "SELECT artifact_ref FROM artifacts WHERE namespace=? AND rel_path=?", (namespace, rel_path)
    ).fetchone()
    if existing is not None:
        manifest_ref = existing["artifact_ref"]
    else:
        manifest_ref = artifacts.make_input_manifest(
            namespace=namespace, entries=entries, rel_path=rel_path, run_id=run_id, actor=ctx.actor,
        ).artifact_ref
    return {"input_manifest_ref": manifest_ref, "parameters": dict(parameters),
            "wall_seconds": request.get("wall_seconds")}


def _adapter_request(view: Mapping[str, Any], function: str, work_dir: Path) -> Any:
    from ..jobs import adapter as adapter_mod

    parser = view["bundle"].get("parser")
    if parser is None:
        raise SchemaInvalid("the bundle binds no adapter module", details={"function": function})
    answer = adapter_mod.call(parser, function, [], work_dir=work_dir)
    if not answer.ok:
        raise SchemaInvalid(
            f"the adapter's {function}() did not answer: {answer.error_type}: {answer.error}",
            details={"function": function, **answer.describe()})
    return answer.result


def _key(view: Mapping[str, Any], what: str) -> str:
    return f"probe:{what}:{view['manifest_sha256']}"


#: Failures of the machinery, not of the capability: the job was refused at launch (a hold, a
#: switch, the preflight, the lease, the network route), the runner was lost or failed, or the
#: device grew past the job's limit (device-wide, so possibly a neighbour). A probe that ended
#: so says nothing about the capability: it is ERROR, and the next pass runs a new attempt.
MACHINERY_FAILURES = frozenset({"not_started", "runner_lost", "runner_terminated", "runner_error",
                                "spawn_failed", "gpu_memory_exceeded"})
MAX_PROBE_ATTEMPTS = 3


def _machinery(job: Mapping[str, Any]) -> bool:
    return job.get("status") == "FAILED" and (job.get("failure") or {}).get("observed") in MACHINERY_FAILURES


def _attempt_key(conn: sqlite3.Connection, run_id: str, view: Mapping[str, Any],
                 what: str) -> tuple[str, int, sqlite3.Row | None]:
    """The idempotency key of a probe job: the one used last time, unless the job under it
    ended for a reason of the machinery; then the next attempt, up to MAX_PROBE_ATTEMPTS (the
    last attempt is replayed after that, so a machine that keeps refusing is not retried for
    ever). A probe job that the capability failed is replayed: its verdict stands at this sha."""
    base = _key(view, what)
    key = base
    for attempt in range(1, MAX_PROBE_ATTEMPTS + 1):
        key = base if attempt == 1 else f"{base}:attempt{attempt}"
        row = conn.execute(
            "SELECT j.*, b.status AS reservation_status FROM jobs j LEFT JOIN budget_reservations b "
            "ON b.reservation_ref=j.reservation_ref WHERE j.idempotency_key=?", (f"job:{run_id}:{key}",)).fetchone()
        if row is None:
            return key, attempt, None
        failure = (store.loads(row, "outcome_json", {}) or {}).get("failure") or {}
        if not _machinery({"status": row["status"], "failure": failure}):
            return key, attempt, row
    return key, MAX_PROBE_ATTEMPTS, row


# -------------------------------------------------------------------------------- L3, ERR

def test_bound(manifest: Mapping[str, Any], wall_seconds: int | None = None) -> dict[str, int]:
    """The ``tighten`` a probe job is submitted with: a test the platform runs is ended at
    ``engineering_budget.ordinary_test_timeout_seconds``, so a job whose wall limit is higher has it lowered to that line. A
    lower limit, the manifest's or the probe's own (``wall_seconds``), stays: the platform may
    lower a line by itself, never raise it."""
    ceiling = int(config.engineering_budget()["ordinary_test_timeout_seconds"])
    own = int(manifest["resources"]["wall_seconds"])
    wanted = own if wall_seconds is None else min(own, int(wall_seconds))
    return {"wall_seconds": wanted if wanted < ceiling else ceiling} if (wall_seconds is not None or own > ceiling) else {}


def _smoke_attempt(conn, run_id, view, request, *, selected_inputs=None, retry_job_id=None):
    """Resume the existing smoke Job or a single operator-selected longer run after timeout."""
    key, attempt, prior = _attempt_key(conn, run_id, view, "smoke")
    intent = store.loads(prior, "submit_intent_json", {}) if prior is not None else {}
    ordinary = intent.get("tighten")
    if ordinary is None:
        ordinary = test_bound(view["manifest"], request.get("wall_seconds"))
    if retry_job_id is None and (prior is None or prior["status"] != "FAILED"):
        return key, attempt, ordinary
    retry_key = key + ":manifest-wall"
    existing = conn.execute("SELECT submit_intent_json FROM jobs WHERE idempotency_key=?",
                            (f"job:{run_id}:{retry_key}",)).fetchone()
    if retry_job_id is None and existing is None:
        return key, attempt, ordinary

    # The operator reconciles the original Job and decides whether its requested model
    # work fits the commission. JobService owns the new preflight, reservation and launch.
    if (not selected_inputs or prior is None or attempt >= MAX_PROBE_ATTEMPTS
            or (retry_job_id is not None and prior["job_id"] != retry_job_id)):
        raise SchemaInvalid("a longer qualification retry must name the original project-selected smoke Job")
    if (intent.get("input_roles") != selected_inputs or prior["input_manifest_ref"] != request["input_manifest_ref"]
            or intent.get("parameters") != request["parameters"] or intent.get("manifest_sha256") != view["manifest_sha256"]):
        raise SchemaInvalid("a qualification retry must preserve the original inputs, parameters and manifest")
    if existing is not None:
        # An intervening limits change applies to new launches. Reconcile the already admitted Job.
        return retry_key, attempt + 1, store.loads(existing, "submit_intent_json", {})["tighten"]
    outcome = store.loads(prior, "outcome_json", {})
    budget = config.engineering_budget()
    retry_ceiling = budget.get("project_qualification_retry_timeout_seconds", budget["ordinary_test_timeout_seconds"])
    wall = min(int(retry_ceiling), int(view["manifest"]["resources"]["wall_seconds"]))
    if request.get("wall_seconds") is not None:
        wall = min(wall, int(request["wall_seconds"]))
    observed_fail = conn.execute(
        "SELECT 1 FROM probe_records WHERE subject_kind='capability' AND subject_id=? AND rung=? "
        "AND result=? AND job_id=? LIMIT 1", (view["capability_version"], L3, FAIL, prior["job_id"])).fetchone()
    if (prior["status"] != "FAILED" or prior["side_effect_status"] != "APPLIED"
            or prior["reservation_status"] != "SETTLED" or (outcome.get("failure") or {}).get("observed") != "timed_out"
            or not observed_fail or wall <= prior["timeout_seconds"]):
        raise SchemaInvalid("a longer qualification retry needs a recorded, settled timeout below the reviewed wall")
    return retry_key, attempt + 1, {"wall_seconds": wall}


def _required_outputs_absent(conn: sqlite3.Connection, declared: list[Mapping[str, Any]],
                             job: Mapping[str, Any]) -> list[str]:
    """Match required outputs to this job's actual publication records.

    JobService publishes a directory as ``name/relative-file`` artifacts, not an
    artifact named ``name``. Require at least one child and a publication for every
    reported child. File/document outputs keep exact-name matching. JobService owns
    checking paths and bytes; this read checks its output metadata against its records.
    """
    published = {row["artifact_ref"]: row for row in conn.execute(
        "SELECT artifact_ref,kind,sha256,bytes FROM artifacts WHERE job_id=? AND published=1",
        (job["job_id"],))}

    def complete(item: Mapping[str, Any], kind: str) -> bool:
        ref = item.get("artifact_ref")
        row = published.get(ref) if isinstance(ref, str) and ref else None
        return bool(row is not None and item.get("kind") == kind == row["kind"]
                    and item.get("sha256") == row["sha256"] and type(item.get("bytes")) is int
                    and item["bytes"] == int(row["bytes"]))

    absent = []
    for output in declared:
        if not output["required"]:
            continue
        name, kind = output["name"], output["kind"]
        directory = manifest_mod.is_dir_output(output)
        if directory or manifest_mod.is_raw_output(output):
            kind = kind.split(":", 1)[1] if ":" in kind else "job_output_file"
        prefix = name + "/"
        matches = [item for item in job["outputs"] if isinstance(item.get("output_name"), str)
                   and ((item["output_name"].startswith(prefix) and len(item["output_name"]) > len(prefix))
                        if directory else item["output_name"] == name)]
        if not matches or not all(complete(item, kind) for item in matches):
            absent.append(name)
    return sorted(absent)


def _onboarding_smoke_inputs(ctx, view, review):
    """Only the onboarding run owning this exact independent review can select its L3 inputs."""
    owner = ctx.conn.execute(
        "SELECT r.* FROM reviews v JOIN tasks t ON t.task_id=v.task_id JOIN runs r ON r.run_id=t.run_id "
        "WHERE v.review_id=? AND v.subject_kind='asset' AND v.subject_id=? AND v.subject_version=? "
        "AND v.subject_sha256=? AND r.workflow_id='onboarding'",
        (review.get("review_id"), view["capability_id"], view["version"], view["manifest_sha256"])).fetchone()
    if owner is None:
        return None
    inputs = (store.loads(owner, "inputs_json", {}) or {}).get("inputs", {})
    if "qualification_inputs" not in inputs:
        return None
    selected = inputs["qualification_inputs"]
    if not isinstance(selected, dict) or set(selected) - set(inputs.get("assets", [])):
        raise SchemaInvalid("qualification_inputs must name exact versions in the onboarding assets list")
    version = view["capability_version"]
    if version not in selected:
        return None
    from ..hostobs import native_context
    call = ctx.native_call
    host = store.loads(owner, "host_json", {})
    if (call is None or native_context.source_of_run(owner) != native_context.SOURCE
            or call._issuer.project_ref != owner["project_ref"] or call.session_id != host.get("session_ref")
            or call.root_thread_id != host["policy_selection"].get("native_agent_ref")):
        raise ScopeDenied("explicit qualification inputs belong to their native onboarding project and session")
    refs = selected[version]
    if not isinstance(refs, dict) or set(refs) != set(manifest_mod.input_roles(view["manifest"])):
        raise SchemaInvalid("qualification inputs must name all input roles of the reviewed capability")
    for role, ref in refs.items():
        row = ctx.conn.execute("SELECT 1 FROM artifacts a JOIN runs r ON r.run_id=a.run_id "
                               "WHERE a.artifact_ref=? AND a.published=1 AND r.project_ref=?",
                               (ref, owner["project_ref"])).fetchone() if isinstance(ref, str) else None
        if row is None:
            raise SchemaInvalid(f"qualification input {role!r} needs a published artifact in the onboarding project")
    return refs


def probe_smoke(ctx: Ctx, view: Mapping[str, Any], run_id: str, work_dir: Path, deadline: float,
                *, selected_inputs=None, retry_job_id=None):
    """L3. Returns ``(result or None while the job is still running, detail, job_id)``."""
    from ..jobs.service import PURPOSE_PROBE, SUCCEEDED, JobService

    request = materialise(ctx, run_id, view, _adapter_request(view, "smoke_request", work_dir),
                          label="smoke", selected_inputs=selected_inputs)
    service = JobService(ctx.conn, ctx=ctx)
    key, attempt, tighten = _smoke_attempt(ctx.conn, run_id, view, request, selected_inputs=selected_inputs,
                                         retry_job_id=retry_job_id)
    answer = service.submit(
        run_id=run_id, capability_version=view["capability_version"],
        input_manifest_ref=request["input_manifest_ref"], parameters=request["parameters"],
        tighten=tighten, idempotency_key=key, purpose=PURPOSE_PROBE,
    )
    job = _wait(service, answer["job_id"], deadline)
    detail = {"job_id": job["job_id"], "status": job["status"], "exit_code": job["exit_code"],
              "measured": job["measured"], "failure": job["failure"], "attempt": attempt,
              "outputs": [{k: item[k] for k in ("output_name", "kind", "artifact_ref", "validated")}
                          for item in job["outputs"]]}
    if not job["terminal"]:
        return None, detail, job["job_id"]
    if _machinery(job):
        another = attempt < MAX_PROBE_ATTEMPTS and not key.endswith(":manifest-wall")
        detail["machinery"] = (f"the smoke job did not run the capability ({job['failure'].get('observed')}): "
                               f"attempt {attempt} of {MAX_PROBE_ATTEMPTS}; the next pass of the ladder "
                               + ("runs another" if another else "replays this one"))
        return ERROR, detail, job["job_id"]
    absent = _required_outputs_absent(ctx.conn, view["manifest"]["outputs"], job)
    gpu = (job["measured"] or {}).get("gpu") or {}
    detail.update(required_outputs_absent=absent, gpu_over_declared_need=gpu.get("reported_peak_over_limit"))
    ok = job["status"] == SUCCEEDED and not absent
    return (PASS if ok else FAIL), detail, job["job_id"]


def gpu_over_declared_need(measured_gpu: Mapping[str, Any] | None, slack_mib: int | None = None) -> bool:
    """Whether JobService measured a tool-reported peak above its declared limit.

    Older measurements have only device-wide peaks. The historical slack argument remains
    accepted for callers reading those records.
    """
    return (measured_gpu or {}).get("reported_peak_over_limit") is True


def _wait(service: Any, job_id: str, deadline: float) -> dict[str, Any]:
    while True:
        remaining = deadline - time.monotonic()
        view = service.wait(job_id, timeout_seconds=max(0.0, min(remaining, 60.0)))
        if view["terminal"] or deadline - time.monotonic() <= 0:
            return view


def probe_errors(ctx: Ctx, view: Mapping[str, Any], run_id: str, work_dir: Path, deadline: float,
                 smoke_job_id: str):
    """Adapter-specific malformed-input handling through the original JobService.

    The legacy probe ID is preserved for old records. Timeout and idempotency are
    JobService behavior, tested once in its software slice, not per model version.
    """
    from ..jobs.service import FAILED, PURPOSE_PROBE, JobService

    service = JobService(ctx.conn, ctx=ctx)
    detail: dict[str, Any] = {}

    # 1. a malformed input is an input error, never a scientific failure
    request = materialise(ctx, run_id, view, _adapter_request(view, "malformed_request", work_dir),
                          label="malformed")
    try:
        answer = service.submit(
            run_id=run_id, capability_version=view["capability_version"],
            input_manifest_ref=request["input_manifest_ref"], parameters=request["parameters"],
            tighten=test_bound(view["manifest"]), idempotency_key=_key(view, "malformed"), purpose=PURPOSE_PROBE,
        )
    except SchemaInvalid as exc:
        detail["malformed"] = {"refused_before_launch": True, "code": exc.code,
                               "input_error": bool(exc.details.get("input_error")),
                               "message": exc.message[:300]}
        malformed_ok = bool(exc.details.get("input_error"))
    else:
        job = _wait(service, answer["job_id"], deadline)
        if not job["terminal"]:
            return None, {**detail, "malformed": {"job_id": job["job_id"], "status": job["status"]}}
        failure = job["failure"] or {}
        detail["malformed"] = {"refused_before_launch": False, "job_id": job["job_id"],
                               "status": job["status"], "failure_class": failure.get("failure_class")}
        malformed_ok = job["status"] == FAILED and failure.get("failure_class") == "input_or_format"

    detail["passed"] = {"malformed": malformed_ok}
    return (PASS if malformed_ok else FAIL), detail


# ------------------------------------------------------------------------------- advance

def observation_inputs(conn: sqlite3.Connection, view: Mapping[str, Any], scope: str = "runtime") -> str:
    """Cheap identity of the local facts an observation used, without hashing weights.

    The registry owns declared hashes and the environment owns package observations.
    File metadata detects changed local facts so their existing checks can refresh;
    this is a reuse key, not an immutable-source or confinement claim.
    """
    try:
        env = environments.get(conn, view["env_id"])
    except GxError as exc:
        return hashlib.sha256(ids.canonical_json({"env_error": exc.message}).encode()).hexdigest()
    env_view = manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                    binaries=dict(env["binary_paths"]), prefix=env["prefix"])
    manifest = view["manifest"]
    files_only = scope in (L0, "probe_L0_files")
    metadata_only = scope in (L1, "probe_L1_metadata")
    weights_only = scope == "weights_digest"
    paths = [item["path"] for item in manifest["weights"]] if not metadata_only else []
    if not metadata_only and not weights_only:
        paths += [manifest["source"]["path"], *manifest["required_data"],
                  *(manifest["entry"].get("pythonpath") or [])]
    cwd = manifest["entry"].get("cwd")
    if cwd and "{out.dir}" not in cwd and not metadata_only and not weights_only:
        paths.append(cwd)
    env_keys = ("env_id", "prefix") if weights_only else (
        ("env_id", "python_executable", "binary_paths", "prefix") if files_only else
        ("env_id", "status", "python_executable", "binary_paths", "prefix", "spec", "probe", "probed_at"))
    facts: dict[str, Any] = {"environment": {key: env.get(key) for key in env_keys},
        "files": {}}

    def stamp(path: Path) -> None:
        try:
            observed = path.stat()
            value = [str(path.resolve()), observed.st_dev, observed.st_ino, observed.st_mode,
                     observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns]
        except OSError as exc:
            value = ["unavailable", exc.errno]
        facts["files"][str(path)] = value

    for template in paths:
        try:
            stamp(manifest_mod.resolve_path(template, env=env_view))
        except GxError as exc:
            facts["files"][template] = ["unresolved", exc.message]
    for ref in (() if metadata_only or weights_only else
                (view["bundle"].get("entry_script"), view["bundle"].get("parser"))):
        if ref:
            stamp(manifest_mod.harness_root() / ref["path"])
    for program in (() if weights_only else (env_view.python, *env_view.binaries.values())):
        if program:
            stamp(Path(program))
    return hashlib.sha256(ids.canonical_json(facts).encode()).hexdigest()


def _record(conn: sqlite3.Connection, view: Mapping[str, Any], rung: str, result: str,
            detail: Mapping[str, Any], job_id: str | None = None) -> dict[str, Any]:
    import os

    stamped = {**dict(detail), "manifest_sha256": view["manifest_sha256"],
               "observation_inputs": observation_inputs(conn, view, rung),
               "probe_host": os.uname().nodename}
    probe_id = registry.record_probe(conn, view["capability_version"], rung=rung, result=result,
                                     detail=stamped, job_id=job_id)
    return {"probe_id": probe_id, "rung": rung, "result": result, "job_id": job_id,
            "summary": _summary(stamped)}


def _summary(detail: Mapping[str, Any]) -> str:
    for key in ("reason", "missing", "unmet", "passed", "status", "verdict"):
        if detail.get(key):
            return f"{key}: {str(detail[key])[:200]}"
    return ""


def _current_records(conn: sqlite3.Connection, view: Mapping[str, Any], records: Mapping[str, dict]) -> dict:
    import os

    out = {}
    observed: dict[str, str] = {}
    for rung, record in records.items():
        detail = record["detail"]
        scope = rung if rung in (L0, L1) else "runtime"
        if scope not in observed:
            observed[scope] = observation_inputs(conn, view, scope)
        inputs = observed[scope]
        if (detail.get("manifest_sha256") == view["manifest_sha256"]
                and detail.get("probe_host") == os.uname().nodename
                and detail.get("observation_inputs", inputs) == inputs):
            if rung == L3 and not record['job_id']:
                continue
            out[rung] = record
    return out


def _cpu_wall_successor(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """Compare the complete registered bundles, allowing only version and a longer CPU deadline."""
    before, after = old["manifest"], new["manifest"]
    resources = before["resources"]
    wall = after["resources"]["wall_seconds"]
    if resources.get("gpu") is not None or wall <= resources["wall_seconds"]:
        return False
    expected = {**before, "version": after["version"], "resources": {**resources, "wall_seconds": wall}}
    if expected != after:
        return False
    # Independent authors may have published the same script at different artifact refs.
    # Both refs were verified at intake. The path and all other bindings must match.
    def scripts(bundle):
        return {name: (ref["path"], ref["sha256"]) for name in ("entry_script", "parser")
                if (ref := bundle.get(name)) is not None}
    return scripts(old["bundle"]) == scripts(new["bundle"])


def latest(conn: sqlite3.Connection, view: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read current observations, including original controls applicable to a longer CPU deadline."""
    records = registry.latest_probes(conn, view["capability_version"])
    current = _current_records(conn, view, records)
    controls = (L3, ERR, L4) if view["manifest"].get("known_answer") else (L3, ERR)
    if view["manifest"]["resources"].get("gpu") is not None or any(rung in records for rung in controls):
        return current
    candidates = conn.execute(
        "SELECT a.version FROM assets a JOIN assets target ON target.asset_id=a.asset_id AND target.kind=a.kind"
        " WHERE target.asset_id=? AND target.version=? AND target.kind=? AND a.rowid<target.rowid"
        " AND a.namespace IS target.namespace AND a.status='ACTIVE' ORDER BY a.rowid DESC",
        (view["capability_id"], view["version"], registry.ASSET_KIND))
    for row in candidates:
        try:
            source = registry.candidate(conn, f"{view['capability_id']}@{row['version']}")
        except GxError:
            continue  # An unreadable older version cannot provide a control.
        if not _cpu_wall_successor(source, view):
            continue
        original = registry.latest_probes(conn, source["capability_version"])
        if not any(rung in original for rung in controls):
            continue  # An intermediate version may also have used the original controls.
        observed = _current_records(conn, source, original)
        if not all(observed.get(rung, {}).get("result") == PASS and observed[rung]["detail"].get("observation_inputs")
                   for rung in controls) or not observed[L3]["job_id"]:
            break
        # Preserve the original observations. No new probe row, Job, measurement or charge.
        for rung in controls:
            probe = observed[rung]
            current[rung] = {**probe, "reused_from": {
                "capability_version": source["capability_version"], "manifest_sha256": source["manifest_sha256"],
                "probe_id": probe["probe_id"], "job_id": probe["job_id"],
                "wall_seconds": source["resources"]["wall_seconds"]}}
        break
    return current


def observed_rung(conn: sqlite3.Connection, view: Mapping[str, Any], rung: str) -> dict[str, Any] | None:
    """Resolve only the requested observation when its check runs."""
    if view["resources"].get("gpu") is None and rung in (L3, ERR, L4):
        return latest(conn, view).get(rung)
    record = registry.latest_probes(conn, view["capability_version"]).get(rung)
    if record is None:
        return None
    return _current_records(conn, view, {rung: record}).get(rung)


def advance(ctx: Ctx, capability_version: str, *, wait_seconds: float = DEFAULT_WAIT_SECONDS,
            retry_job_id: str | None = None) -> dict[str, Any]:
    """Climb the ladder as far as it may go now. Returns what each rung showed and why it stopped."""
    conn = ctx.conn
    if conn.in_transaction:
        raise RuntimeError("the probe ladder starts processes and is not run inside a transaction")
    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    view = registry.candidate(conn, capability_version)
    work_dir = storage.jobs_root(conn) / "probe-work" / view["manifest_sha256"][:16]
    probes: list[dict[str, Any]] = []
    done = latest(conn, view)

    def finish(stopped: str | None) -> dict[str, Any]:
        current = registry.candidate(conn, capability_version)
        answer = {"capability_version": capability_version, "manifest_sha256": view["manifest_sha256"],
                  "status": current["status"], "probe_rung": current["probe_rung"],
                  "test_status": current["test_status"], "probes": probes, "stopped": stopped}
        reused = {rung: item["reused_from"] for rung, item in done.items() if "reused_from" in item}
        if reused:
            answer["reused_controls"] = reused
        if view["manifest"].get("known_answer") is None:
            # No rung above L3 can pass for it: say so wherever the rungs are summarised.
            answer["not_exercised"] = {L4: registry.L4_NOT_EXERCISED}
        return answer

    for rung, observe in ((L0, probe_files), (L1, probe_metadata)):
        if (done.get(rung) or {}).get("result") == PASS:
            continue
        result, detail = observe(conn, view)
        probes.append(_record(conn, view, rung, result, detail))
        if result != PASS:
            return finish(f"{rung} did not pass")

    review = review_state(conn, view)
    if (done.get(L2) or {}).get("result") != PASS and (review.get("met") or metadata_probe_allowed(view)):
        result, detail = probe_entrypoint(conn, view, work_dir)
        probes.append(_record(conn, view, L2, result, detail))
        if result != PASS:
            return finish("L2_ENTRYPOINT did not pass")

    if not review.get("met"):
        return finish(f"waiting for an independent APPROVE review of these bytes "
                      f"({review.get('state')}); scientific jobs wait for it")

    if view["status"] not in JOB_STATES:
        return finish(f"the asset is {view['status']}; the ladder launches jobs once it is REVIEWED")

    selected_inputs = _onboarding_smoke_inputs(ctx, view, review)
    previous = (done.get(L3) or {}).get("job_id")
    if selected_inputs is not None and previous:
        original = conn.execute("SELECT j.submit_intent_json,r.project_ref FROM jobs j JOIN runs r USING(run_id) "
                                "WHERE job_id=?", (previous,)).fetchone()
        intent = store.loads(original, "submit_intent_json", {})
        if (original is None or original["project_ref"] != ctx.native_call._issuer.project_ref
                or intent.get("input_roles") != selected_inputs or intent.get("capability_version") != capability_version):
            raise SchemaInvalid("the original qualification Job has different inputs, project or capability version")
    if all("reused_from" in done.get(rung, {}) for rung in (L3, ERR)):
        return finish(None)
    run_id = probe_run(ctx, view)
    smoke_job_id = (done.get(L3) or {}).get("job_id") if (done.get(L3) or {}).get("result") == PASS else None
    if smoke_job_id is None:
        try:
            result, detail, job_id = probe_smoke(ctx, view, run_id, work_dir, deadline,
                                                 selected_inputs=selected_inputs, retry_job_id=retry_job_id)
        except GxError as exc:
            if retry_job_id is not None:
                raise  # refusal of a new attempt does not replace the original observation
            probes.append(_record(conn, view, L3, ERROR, {"reason": exc.message, "error": exc.to_payload()}))
            return finish("L3_SMOKE could not run")
        if result is None:
            probes.append({"rung": L3, "result": None, "job_id": job_id,
                           "summary": f"job {job_id} is {detail['status']}; ask again"})
            return finish("the smoke job is still running")
        probes.append(_record(conn, view, L3, result, detail, job_id=job_id))
        if result != PASS:
            return finish("L3_SMOKE did not pass")
        smoke_job_id = job_id

    if (done.get(ERR) or {}).get("result") != PASS:
        try:
            result, detail = probe_errors(ctx, view, run_id, work_dir, deadline, smoke_job_id)
        except GxError as exc:
            probes.append(_record(conn, view, ERR, ERROR, {"reason": exc.message, "error": exc.to_payload()}))
            return finish("the malformed-input probe could not run")
        if result is None:
            probes.append({"rung": ERR, "result": None, "job_id": None,
                           "summary": "a probe job is still running; ask again"})
            return finish("an error-probe job is still running")
        probes.append(_record(conn, view, ERR, result, detail,
                              job_id=(detail.get("malformed") or {}).get("job_id")))
        if result != PASS:
            return finish("the malformed-input probe did not pass")

    return finish(None)


__all__ = ["DEFAULT_WAIT_SECONDS", "ERR", "ERROR", "FAIL", "JOB_STATES", "PASS", "PROBE_PROJECT",
           "advance", "gpu_over_declared_need", "latest", "materialise", "probe_entrypoint", "probe_errors",
           "probe_files",
           "metadata_probe_allowed", "observation_inputs", "probe_metadata", "probe_run", "probe_smoke",
           "require_reviewed", "review_state", "subject_of"]
