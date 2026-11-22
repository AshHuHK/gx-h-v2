"""Environments: registered from a probe the backend runs, never from a statement.

An environment is where a capability executes: an interpreter and some binaries, at
absolute paths, reached without ``conda activate``. Its status follows the
``environment`` machine of ``contracts/states.json``, and the one rule that matters is
that machine's own: *HEALTHY is written only from a probe record the backend produced
by running the interpreter.* v1 accepted a row saying HEALTHY with a sentence beside it
and an interpreter that did not exist.

So the operator supplies a **specification** — where the interpreter is, which binaries
belong to it, how each can be asked to answer without doing work, which packages it must
provide — and this module produces the **observation**. A specification that carries a
status, a readiness or a probe result is refused: there is no field to put one in.

What the probe records
----------------------
* the interpreter answered ``-I -c …`` and reported its version, prefix and the installed
  versions of the declared requirements. Package metadata is read with
  ``importlib.metadata``; nothing is imported, so asking whether torch is installed does
  not load it;
* each binary ran its declared probe argv and exited with a declared code;
* **dead console scripts**, as an observation of their own. A file in ``bin/`` whose
  ``#!`` names an interpreter that no longer exists is counted and listed, as a diagnostic separate from interpreter readiness.

A probe is tied to the host it ran on. This workspace survives a pod rebuild and the
interpreters do not necessarily; a HEALTHY row probed on another host is re-probed
before anything is launched against it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .. import config, contracts
from ..domain import clock, ids, machines
from ..errors import EnvNotReady, SchemaInvalid
from ..persistence import store
from . import manifest as manifest_mod
from . import procs

MACHINE = "environment"
ENV_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}")

#: What an environment specification may contain. Nothing here is an observation.
SPEC_FIELDS = {
    "env_id": "required", "kind": "required", "prefix": "optional",
    "python_executable": "optional", "binaries": "optional", "binary_probes": "optional",
    "requirements": "optional", "limitations": "optional", "execution_host": "optional",
    "container_digest": "optional", "lockfile_ref": "optional",
}

#: Keys that would be a declared readiness. Named so the refusal can say what it refused.
_DECLARED_READINESS = ("status", "ready", "readiness", "readiness_basis", "probe", "probed",
                       "probed_at", "probe_json", "healthy", "observed", "test_status")

#: Runs inside the probed interpreter with ``-I``. Reads metadata; imports nothing else.
_CHILD = (
    "import json,sys,importlib.metadata as m\n"
    "names=json.loads(sys.argv[1])\n"
    "out={'python':sys.version.split()[0],'executable':sys.executable,'prefix':sys.prefix,'dists':{}}\n"
    "for n in names:\n"
    "    try: out['dists'][n]=m.version(n)\n"
    "    except Exception: out['dists'][n]=None\n"
    "print(json.dumps(out))\n"
)

_MAX_LISTED_DEAD = 40
_SPECIFIER = re.compile(r"^\s*(==|!=|>=|<=|>|<)\s*([A-Za-z0-9_.+!*-]+)\s*$")


# ------------------------------------------------------------------------- specification

def spec_problems(spec: Any) -> list[str]:
    if not isinstance(spec, Mapping):
        return ["environment must be an object"]
    found: list[str] = []
    declared = sorted(key for key in spec if key in _DECLARED_READINESS)
    if declared:
        found.append(
            f"environment carries {declared}: a readiness that is declared rather than "
            "observed is refused. The backend runs the probe and writes the status itself"
        )
    unknown = sorted(set(spec) - set(SPEC_FIELDS) - set(_DECLARED_READINESS))
    if unknown:
        found.append(f"environment has no field(s) {unknown}; allowed: {sorted(SPEC_FIELDS)}")
    for key, need in SPEC_FIELDS.items():
        if need == "required" and not spec.get(key):
            found.append(f"environment.{key} is required")
    if found:
        return found

    if not isinstance(spec["env_id"], str) or not ENV_ID.fullmatch(spec["env_id"]):
        found.append(f"environment.env_id must match {ENV_ID.pattern}")
    if spec["kind"] not in contracts.label_values("env_kind"):
        found.append(f"environment.kind {spec['kind']!r} is not one of "
                     f"{contracts.label_values('env_kind')}")
    for key in ("prefix", "python_executable"):
        value = spec.get(key)
        if value is not None:
            found.extend(_path_problems(value, where=f"environment.{key}",
                                        allow_prefix=key != "prefix"))
    binaries = spec.get("binaries") or {}
    probes = spec.get("binary_probes") or {}
    if not isinstance(binaries, Mapping) or not isinstance(probes, Mapping):
        found.append("environment.binaries and environment.binary_probes must be objects")
        return found
    for name, path in binaries.items():
        if not isinstance(name, str) or not manifest_mod.BINARY_NAME.fullmatch(name):
            found.append(f"environment.binaries: {name!r} is not a usable binary name")
        found.extend(_path_problems(path, where=f"environment.binaries[{name!r}]", allow_prefix=True))
        if name not in probes:
            found.append(
                f"environment.binary_probes has no entry for {name!r}: say how the binary can "
                "be asked to answer without doing work (an argv, possibly empty, and the exit "
                "codes that count as an answer)"
            )
    for name, probe in probes.items():
        where = f"environment.binary_probes[{name!r}]"
        if name not in binaries:
            found.append(f"{where}: no such binary is declared")
            continue
        if not isinstance(probe, Mapping) or set(probe) - {"argv", "ok_exit_codes"}:
            found.append(f"{where} must be {{'argv': [...], 'ok_exit_codes': [...]}}")
            continue
        argv = probe.get("argv", [])
        codes = probe.get("ok_exit_codes", [0])
        if not isinstance(argv, list) or not all(isinstance(t, str) and "\x00" not in t for t in argv):
            found.append(f"{where}.argv must be a list of strings")
        if (not isinstance(codes, list) or not codes
                or not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in codes)):
            found.append(f"{where}.ok_exit_codes must be a non-empty list of exit codes; a "
                         "signal is never an answer")
    requirements = spec.get("requirements") or {}
    if not isinstance(requirements, Mapping):
        found.append("environment.requirements must be an object of distribution -> specifier")
    else:
        if requirements and not spec.get("python_executable"):
            found.append("environment.requirements needs a python_executable to read metadata from")
        for name, specifier in requirements.items():
            if not isinstance(specifier, str) or _parse_specifier(specifier) is None:
                found.append(f"environment.requirements[{name!r}] = {specifier!r} is not a version "
                             "specifier such as '>=2.0', '==1.13.1' or '*'")
    if not spec.get("python_executable") and not binaries:
        found.append("environment declares neither an interpreter nor a binary: there would be "
                     "nothing to observe")
    for key in ("limitations", "execution_host", "container_digest", "lockfile_ref"):
        if spec.get(key) is not None and not isinstance(spec[key], str):
            found.append(f"environment.{key} must be a string")
    return found


def _path_problems(value: Any, *, where: str, allow_prefix: bool) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{where} must be a non-empty string"]
    if "\x00" in value:
        return [f"{where} contains a NUL byte"]
    names = manifest_mod.placeholders(value)
    allowed = {"workspace", "harness"} | ({"env.prefix"} if allow_prefix else set())
    bad = [name for name in names if name not in allowed]
    if bad:
        return [f"{where}: placeholder(s) {bad} are not allowed here; allowed: {sorted(allowed)}"]
    if not names and not Path(value).is_absolute():
        return [f"{where}: {value!r} must be absolute or start with a placeholder root; a "
                "relative path resolves against whatever directory a job happens to run in"]
    if ".." in Path(re.sub(r"\{[^{}]*\}", "x", value)).parts:
        return [f"{where} contains '..'"]
    return []


def _resolve(value: str, *, prefix: str | None) -> str:
    values = {"workspace": str(manifest_mod.workspace_root()), "harness": str(manifest_mod.harness_root())}
    if prefix:
        values["env.prefix"] = prefix
    out = value
    for name in manifest_mod.placeholders(value):
        if name not in values:
            raise SchemaInvalid(f"path {value!r} uses {{{name}}} but no prefix was declared",
                                details={"path": value})
        out = out.replace("{" + name + "}", values[name])
    # abspath, never realpath: a venv's interpreter is a symlink to the base interpreter,
    # and resolving it would silently leave the venv.
    return os.path.abspath(out)


def resolved_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    prefix = _resolve(spec["prefix"], prefix=None) if spec.get("prefix") else None
    python = _resolve(spec["python_executable"], prefix=prefix) if spec.get("python_executable") else None
    binaries = {name: _resolve(path, prefix=prefix) for name, path in (spec.get("binaries") or {}).items()}
    return {"prefix": prefix, "python_executable": python, "binaries": binaries}


# --------------------------------------------------------------------- version specifiers

def _parse_specifier(text: str) -> list[tuple[str, str]] | None:
    text = text.strip()
    if text in ("*", ""):
        return [] if text == "*" else None
    clauses: list[tuple[str, str]] = []
    for part in text.split(","):
        match = _SPECIFIER.match(part)
        if not match:
            return None
        clauses.append((match.group(1), match.group(2)))
    return clauses


def _release(version: str) -> tuple[int, ...]:
    """The release segment of a version as integers: ``2.5.1+cu124`` -> ``(2, 5, 1)``.

    Compare the numeric release segment independently of local build metadata.
    """
    head = version.split("+", 1)[0].split("!", 1)[-1]
    parts: list[int] = []
    for piece in head.split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group(0)))
        if digits.group(0) != piece:
            break
    return tuple(parts)


def version_satisfies(installed: str | None, specifier: str) -> bool:
    clauses = _parse_specifier(specifier)
    if clauses is None or installed is None:
        return False
    have = _release(installed)
    if not have:
        return False
    for op, wanted in clauses:
        want = _release(wanted)
        width = max(len(have), len(want))
        a = have + (0,) * (width - len(have))
        b = want + (0,) * (width - len(want))
        ok = {"==": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False
    return True


# ---------------------------------------------------------------------------------- probe

def _timeout() -> int:
    return int(config.engineering_budget()["metadata_command_timeout_seconds"])


def _file_record(path: str) -> dict[str, Any]:
    record: dict[str, Any] = {"path": path, "exists": os.path.exists(path)}
    if not record["exists"]:
        return record
    record["is_file"] = os.path.isfile(path)
    record["executable"] = os.access(path, os.X_OK)
    record["is_symlink"] = os.path.islink(path)
    try:
        record["bytes"] = os.stat(path).st_size
    except OSError:  # pragma: no cover - raced with a removal
        record["bytes"] = None
    return record


def dead_console_scripts(prefix: str | None) -> dict[str, Any]:
    """Files in ``<prefix>/bin`` whose ``#!`` interpreter does not exist. An observation."""
    record: dict[str, Any] = {"scanned": 0, "scripts": 0, "dead": 0, "names": [], "interpreters": {}}
    if not prefix:
        return record
    bin_dir = Path(prefix) / "bin"
    if not bin_dir.is_dir():
        return record
    for entry in sorted(bin_dir.iterdir()):
        try:
            if not entry.is_file():
                continue
            with open(entry, "rb") as handle:
                head = handle.read(256)
        except OSError:
            continue
        record["scanned"] += 1
        if not head.startswith(b"#!"):
            continue
        line = head.split(b"\n", 1)[0][2:].decode("utf-8", "replace").strip()
        interpreter = line.split()[0] if line.split() else ""
        if not interpreter.startswith("/"):
            continue
        record["scripts"] += 1
        if os.path.exists(interpreter):
            continue
        record["dead"] += 1
        if len(record["names"]) < _MAX_LISTED_DEAD:
            record["names"].append(entry.name)
        record["interpreters"][interpreter] = record["interpreters"].get(interpreter, 0) + 1
    record["names_truncated"] = record["dead"] > len(record["names"])
    return record


def probe(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Observe one environment. Runs programs; touches no store."""
    found = spec_problems(spec)
    if found:
        raise SchemaInvalid(f"environment specification has {len(found)} problem(s): {found[0]}",
                            details={"problems": found})
    paths = resolved_spec(spec)
    timeout = _timeout()
    record: dict[str, Any] = {
        "env_id": spec["env_id"], "kind": spec["kind"], "probed_at": clock.now_iso(),
        "probe_host": os.uname().nodename, "timeout_seconds": timeout,
        "resolved": paths, "interpreter": None, "binaries": {}, "requirements": {},
        "evidence_level": "EXECUTION_OBSERVED",
    }
    host = spec.get("execution_host")
    if host not in (None, "", "local", "localhost", record["probe_host"]):
        record.update(status="UNPROBED", reasons=[
            f"execution_host {host!r} is not this host; a file that exists here says nothing "
            "about what that host can read"])
        record["evidence_level"] = "UNVERIFIED"
        return record

    reasons: list[str] = []
    answered = 0
    declared = 0

    python = paths["python_executable"]
    if python:
        declared += 1
        entry = _file_record(python)
        runnable = bool(entry.get("exists") and entry.get("is_file") and entry.get("executable"))
        if runnable:
            names = sorted(spec.get("requirements") or {})
            run = procs.run_bounded([python, "-I", "-B", "-c", _CHILD, json.dumps(names)], timeout=timeout)
            entry["run"] = {k: v for k, v in run.items() if k != "argv"}
            try:
                entry["reported"] = json.loads((run.get("stdout") or "").strip().splitlines()[-1])
            except (ValueError, IndexError):
                entry["reported"] = None
            entry["answered"] = bool(run.get("ran") and run.get("exit_code") == 0 and entry["reported"])
        else:
            entry["answered"] = False
        record["interpreter"] = entry
        if entry["answered"]:
            answered += 1
        else:
            reasons.append(f"interpreter {python} did not answer")

    for name, path in paths["binaries"].items():
        declared += 1
        entry = _file_record(path)
        probe_spec = (spec.get("binary_probes") or {})[name]
        ok_codes = list(probe_spec.get("ok_exit_codes", [0]))
        runnable = bool(entry.get("exists") and entry.get("is_file") and entry.get("executable"))
        if runnable:
            run = procs.run_bounded([path, *probe_spec.get("argv", [])], timeout=timeout)
            entry["run"] = {k: v for k, v in run.items() if k != "argv"}
            entry["probe_argv"] = list(probe_spec.get("argv", []))
            entry["ok_exit_codes"] = ok_codes
            entry["answered"] = bool(run.get("ran") and run.get("exit_code") in ok_codes)
        else:
            entry["answered"] = False
        record["binaries"][name] = entry
        if entry["answered"]:
            answered += 1
        else:
            reasons.append(f"binary {name} did not answer its probe")

    unmet: list[str] = []
    reported = (record["interpreter"] or {}).get("reported") or {}
    for name, specifier in sorted((spec.get("requirements") or {}).items()):
        installed = (reported.get("dists") or {}).get(name)
        met = version_satisfies(installed, specifier)
        record["requirements"][name] = {"specifier": specifier, "installed": installed, "met": met}
        if not met:
            unmet.append(f"{name}{specifier} (installed: {installed})")
    if unmet:
        reasons.append("requirements not met: " + "; ".join(unmet))

    record["dead_console_scripts"] = dead_console_scripts(paths["prefix"])
    record["declared_executables"] = declared
    record["answered_executables"] = answered

    interpreter_ok = record["interpreter"] is None or record["interpreter"]["answered"]
    if answered == 0 or not interpreter_ok:
        status = "BROKEN"
    elif answered < declared or unmet:
        status = "DEGRADED"
    else:
        status = "HEALTHY"
    record["status"] = status
    record["reasons"] = reasons
    record["readiness_basis"] = (
        f"{answered} of {declared} declared executable(s) answered a probe the backend ran on "
        f"{record['probe_host']} at {record['probed_at']}"
        + (f"; {len(record['requirements'])} requirement(s) read from package metadata without "
           "importing" if record["requirements"] else "")
    )
    return record


# ----------------------------------------------------------------------------- the store

def _row(conn: sqlite3.Connection, env_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM environments WHERE env_id=?", (env_id,)).fetchone()


def get(conn: sqlite3.Connection, env_id: str) -> dict[str, Any]:
    row = _row(conn, env_id)
    if row is None:
        raise EnvNotReady(f"environment {env_id!r} is not registered",
                          details={"env_id": env_id, "reason": "not_registered"})
    probe_json = store.loads(row, "probe_json", {}) or {}
    return {
        "env_id": row["env_id"], "kind": row["kind"], "status": row["status"],
        "python_executable": row["python_executable"],
        "binary_paths": store.loads(row, "binary_paths_json", {}) or {},
        "prefix": (probe_json.get("resolved") or {}).get("prefix"),
        "probed_at": row["probed_at"], "readiness_basis": row["readiness_basis"],
        "dead_entrypoints": store.loads(row, "dead_entrypoints_json", []) or [],
        "limitations": row["limitations"], "execution_host": row["execution_host"],
        "spec": probe_json.get("spec") or {}, "probe": probe_json.get("observed") or {},
    }


def view(conn: sqlite3.Connection, env_id: str) -> manifest_mod.EnvView:
    env = get(conn, env_id)
    return manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                binaries=dict(env["binary_paths"]), prefix=env["prefix"])


def _write_status(conn: sqlite3.Connection, env_id: str, current: str | None, target: str, *,
                  actor: str, reason: str) -> None:
    """Move along the environment machine; a BROKEN row re-enters through UNPROBED."""
    if current is None or current == target:
        return
    steps = [target]
    if not machines.can_transition(MACHINE, current, target):
        steps = [machines.initial_state(MACHINE), target]
    state = current
    for step in steps:
        if step == state:
            continue
        store.transition(conn, machine=MACHINE, subject_id=env_id, to_state=step, actor=actor,
                         reason=reason[:500], evidence_level="EXECUTION_OBSERVED", bump=False)
        state = step


def register(conn: sqlite3.Connection, spec: Mapping[str, Any], *, actor: str,
             run_probe: bool = True, observed: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Register or re-register an environment. The status is whatever the probe observed.

    The probe runs before any transaction is opened: it starts processes and may take as
    long as its timeout, and a write lock held that long stops every other writer.
    ``observed`` is for a caller that already ran :func:`probe` on this specification for
    that reason; it is checked to be an observation of it, and is never a caller's claim
    — no tool passes one in.
    """
    found = spec_problems(spec)
    if found:
        raise SchemaInvalid(f"environment specification has {len(found)} problem(s): {found[0]}",
                            details={"problems": found})
    env_id = spec["env_id"]
    paths = resolved_spec(spec)
    if observed is not None:
        if (observed.get("env_id") != env_id or observed.get("resolved") != paths
                or observed.get("probe_host") != os.uname().nodename):
            raise SchemaInvalid("the observation handed to register() is not a probe of this "
                                "specification on this host", details={"env_id": env_id})
        observed = dict(observed)
    elif run_probe:
        observed = probe(spec)
    target = observed["status"] if observed else machines.initial_state(MACHINE)
    now = clock.now_iso()
    stored = {"spec": dict(spec), "resolved": paths, "observed": observed or {},
              "spec_sha256": ids.sha256_of_obj(dict(spec))}
    dead = (observed or {}).get("dead_console_scripts") or {}

    with store.transaction(conn) as txn:
        existing = _row(txn, env_id)
        if existing is None:
            txn.execute(
                "INSERT INTO environments(env_id, kind, python_executable, binary_paths_json,"
                " container_digest, execution_host, lockfile_ref, env_var_names_json, status,"
                " probe_json, probed_at, readiness_basis, dead_entrypoints_json, limitations,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (env_id, spec["kind"], paths["python_executable"], ids.canonical_json(paths["binaries"]),
                 spec.get("container_digest"), spec.get("execution_host") or "local",
                 spec.get("lockfile_ref"), "[]", machines.initial_state(MACHINE),
                 ids.canonical_json(stored), None, None, "[]", spec.get("limitations"), now, now),
            )
            store.append_event(
                txn, kind="environment.registered", actor=actor, subject_kind="environment",
                subject_id=env_id, new_state=machines.initial_state(MACHINE),
                evidence_level="PROPOSED",
                payload={"kind": spec["kind"], "spec_sha256": stored["spec_sha256"]},
            )
            current = machines.initial_state(MACHINE)
        else:
            current = existing["status"]
            if current in machines.terminal_states(MACHINE):
                raise SchemaInvalid(
                    f"environment {env_id!r} is {current}; register a new env_id instead",
                    details={"env_id": env_id, "status": current},
                )
        if observed is not None:
            txn.execute(
                "UPDATE environments SET kind=?, python_executable=?, binary_paths_json=?,"
                " container_digest=?, execution_host=?, lockfile_ref=?, probe_json=?, probed_at=?,"
                " readiness_basis=?, dead_entrypoints_json=?, limitations=?, updated_at=?"
                " WHERE env_id=?",
                (spec["kind"], paths["python_executable"], ids.canonical_json(paths["binaries"]),
                 spec.get("container_digest"), spec.get("execution_host") or "local",
                 spec.get("lockfile_ref"), ids.canonical_json(stored), observed["probed_at"],
                 observed.get("readiness_basis"),
                 ids.canonical_json({"dead": dead.get("dead", 0), "scripts": dead.get("scripts", 0),
                                     "names": dead.get("names", []),
                                     "interpreters": dead.get("interpreters", {})}),
                 spec.get("limitations"), now, env_id),
            )
            _write_status(txn, env_id, current, target, actor=actor,
                          reason="; ".join(observed.get("reasons") or []) or observed["readiness_basis"])
            _record_probe(txn, env_id, observed)
    result = get(conn, env_id)
    return {
        "env_id": env_id, "environment_status": result["status"],
        "probed": observed is not None, "probed_at": result["probed_at"],
        "readiness_basis": result["readiness_basis"],
        "reasons": (observed or {}).get("reasons", []),
        "dead_console_scripts": dead.get("dead", 0),
        "probes": _probe_rows(conn, env_id, limit=3),
    }


def _record_probe(conn: sqlite3.Connection, env_id: str, observed: Mapping[str, Any]) -> None:
    files_ok = all(
        entry.get("exists") and entry.get("is_file") and entry.get("executable")
        for entry in [observed.get("interpreter") or {"exists": True, "is_file": True, "executable": True},
                      *observed.get("binaries", {}).values()]
    )
    rows = [
        ("L0_FILES", "PASS" if files_ok else "FAIL",
         {"resolved": observed.get("resolved"), "declared": observed.get("declared_executables")}),
        ("L2_ENTRYPOINT", "PASS" if observed.get("status") in ("HEALTHY", "DEGRADED")
         and observed.get("answered_executables") == observed.get("declared_executables") else "FAIL",
         {"answered": observed.get("answered_executables"), "declared": observed.get("declared_executables"),
          "reasons": observed.get("reasons"), "probe_host": observed.get("probe_host")}),
    ]
    if observed.get("requirements"):
        rows.insert(1, ("L1_METADATA",
                        "PASS" if all(v["met"] for v in observed["requirements"].values()) else "FAIL",
                        {"requirements": observed["requirements"]}))
    for rung, result, detail in rows:
        conn.execute(
            "INSERT INTO probe_records(probe_id, subject_kind, subject_id, rung, result, detail_json,"
            " job_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_probe_id(), "environment", env_id, rung, result, ids.canonical_json(detail), None,
             clock.now_iso()),
        )


def new_probe_id() -> str:
    return ids.new_id("probe")


def _probe_rows(conn: sqlite3.Connection, subject_id: str, *, limit: int,
                subject_kind: str = "environment") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT probe_id, rung, result, job_id, created_at FROM probe_records"
        " WHERE subject_kind=? AND subject_id=? ORDER BY created_at DESC, probe_id DESC LIMIT ?",
        (subject_kind, subject_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def reprobe(conn: sqlite3.Connection, env_id: str, *, actor: str) -> dict[str, Any]:
    env = get(conn, env_id)
    if not env["spec"]:
        raise EnvNotReady(f"environment {env_id!r} has no stored specification to probe again",
                          details={"env_id": env_id})
    return register(conn, env["spec"], actor=actor, run_probe=True)


def readiness(env: Mapping[str, Any]) -> dict[str, Any]:
    """Why an environment is or is not callable, as data."""
    reasons: list[str] = []
    ready_states = contracts.states()["machines"][MACHINE]["ready"]
    if env["status"] not in ready_states:
        reasons.append(f"status={env['status']}")
    observed = env.get("probe") or {}
    if not env.get("probed_at") or not observed:
        reasons.append("never_probed")
    elif observed.get("probe_host") != os.uname().nodename:
        reasons.append(f"probed_on_another_host:{observed.get('probe_host')}")
    return {"env_id": env["env_id"], "status": env["status"], "ready": not reasons,
            "reasons": reasons, "probed_at": env.get("probed_at"),
            "readiness_basis": env.get("readiness_basis")}


def readiness_view(conn: sqlite3.Connection, env_id: str) -> dict[str, Any]:
    """What may be known about an environment without running anything, read from its
    stored probe. Never probes and never writes, so it can be asked inside a selection wave.

        healthy          the status is one the environment machine calls ready
        probed           a probe is on record
        host, same_host  the host the stored probe ran on, and whether it is this one
        max_age_seconds  None: the job service sets no age limit on a probe made on this
                         host. It probes again, before a launch, only a record made on
                         another host, and a wave must not, so such a record is not fresh
        fresh            probed, on this host: the job service would use the record as it is
        ready            healthy and fresh: a job submitted now passes the environment check
                         of :func:`require_ready` without a probe
        why              None when ready; otherwise what is missing, as one sentence

    ``ready`` is :func:`readiness`'s answer for the same row, so this view and the job
    service cannot disagree about a record they both read.
    """
    this_host = os.uname().nodename
    base: dict[str, Any] = {"env_id": env_id, "this_host": this_host, "max_age_seconds": None}
    try:
        env = get(conn, env_id)
    except EnvNotReady as exc:
        return {**base, "registered": False, "status": None, "healthy": False, "probed": False,
                "probed_at": None, "host": None, "same_host": False, "fresh": False, "ready": False,
                "reasons": ["not_registered"], "probe_reasons": [], "why": exc.message}
    state = readiness(env)
    observed = env.get("probe") or {}
    healthy = env["status"] in contracts.states()["machines"][MACHINE]["ready"]
    probed = bool(env.get("probed_at") and observed)
    host = observed.get("probe_host") if probed else None
    same_host = probed and host == this_host
    why: list[str] = []
    if not probed:
        why.append(f"environment {env_id} has never been probed")
    elif not same_host:
        why.append(f"environment {env_id} was last probed on host {host}, not on this host {this_host}; "
                   "the job service probes it again before a launch, and a wave does not")
    if not healthy:
        why.append(f"environment {env_id} is {env['status']}"
                   + (f" by the probe at {env['probed_at']} on {host}" if probed else "")
                   + (f": {'; '.join(observed.get('reasons') or [])}" if observed.get("reasons") else ""))
    return {**base, "registered": True, "status": env["status"], "healthy": healthy, "probed": probed,
            "probed_at": env.get("probed_at"), "host": host, "same_host": same_host, "fresh": same_host,
            "ready": state["ready"], "reasons": state["reasons"],
            "probe_reasons": list(observed.get("reasons") or []),
            "why": None if state["ready"] else "; ".join(why) or ", ".join(state["reasons"])}


def require_ready(conn: sqlite3.Connection, env_id: str, *, actor: str = "backend",
                  reprobe_when_stale: bool = True) -> dict[str, Any]:
    """The environment row, or ``ENV_NOT_READY`` saying why.

    A row probed on another host is probed again here before it is trusted: the
    interpreters live on a layer a pod rebuild can change, the store does not.
    """
    env = get(conn, env_id)
    state = readiness(env)
    stale = any(reason.startswith("probed_on_another_host") for reason in state["reasons"])
    if stale and reprobe_when_stale and env["spec"]:
        reprobe(conn, env_id, actor=actor)
        env = get(conn, env_id)
        state = readiness(env)
    if not state["ready"]:
        raise EnvNotReady(
            f"environment {env_id!r} is not ready: {', '.join(state['reasons'])}",
            details={**state, "probe_reasons": (env.get("probe") or {}).get("reasons", [])},
        )
    return env


def listing(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT env_id FROM environments ORDER BY env_id LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        env = get(conn, row["env_id"])
        out.append({"env_id": env["env_id"], "kind": env["kind"], "status": env["status"],
                    "probed_at": env["probed_at"], "python_executable": env["python_executable"],
                    "binaries": sorted(env["binary_paths"]),
                    "dead_console_scripts": (env["dead_entrypoints"] or {}).get("dead", 0)
                    if isinstance(env["dead_entrypoints"], dict) else 0})
    return out


__all__ = ["ENV_ID", "MACHINE", "dead_console_scripts", "get", "listing", "new_probe_id", "probe",
           "readiness", "readiness_view", "register", "reprobe", "require_ready", "resolved_spec",
           "spec_problems", "version_satisfies", "view"]
