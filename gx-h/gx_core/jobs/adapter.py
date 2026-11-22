"""The backend's side of an adapter module: read its literals, call it out of process.

An adapter module is the ``parser`` script of a capability bundle. It knows one tool's
dialect — how its entry point can be asked to answer, what a smoke input looks like,
which inputs it must refuse, what its output means. It is bound to the capability
version by sha256, and it is code somebody wrote, so:

* nothing here imports it. A literal (``ENTRYPOINT_PROBE``) is read from the syntax tree
  without executing a line; a function is called in a subprocess
  (``gx_core/jobs/adapter_host.py``) with a scrubbed environment, a working directory
  inside the job's own directory, and a time limit;
* every call re-hashes the file first, and the subprocess re-hashes it again before
  loading it;
* whether adapter code may run at all for a version that is not ACTIVE is the caller's
  question (:func:`gx_core.capabilities.ladder.require_reviewed`): none of it runs
  before an independent APPROVE review of exactly these bytes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import config
from ..capabilities import manifest as manifest_mod
from ..capabilities import procs
from ..domain import ids

HOST_REL = "gx_core/jobs/adapter_host.py"
LIB_REL = "adapters/local/lib"

#: The functions an adapter module may define. Calling any other name is refused here,
#: so a request cannot reach an arbitrary attribute of the module.
FUNCTIONS = ("smoke_request", "slow_request", "malformed_request", "validate_inputs", "parse",
             "classify_failure")
LITERALS = ("ENTRYPOINT_PROBE",)


@dataclass
class AdapterAnswer:
    ok: bool
    result: Any = None
    error_type: str | None = None
    error: str | None = None
    missing_function: bool = False
    seconds: float | None = None

    def describe(self) -> dict[str, Any]:
        return {"ok": self.ok, "error_type": self.error_type, "error": self.error,
                "missing_function": self.missing_function, "seconds": self.seconds}


def host_path() -> Path:
    return manifest_mod.harness_root() / HOST_REL


def lib_dir() -> Path:
    return manifest_mod.harness_root() / LIB_REL


def platform_digest() -> dict[str, Any]:
    """sha256 of the host program and of every library file an adapter may import.

    Recorded with each job: a capability's own sha256 covers its adapter and entry script,
    not this platform code, so a result says which platform bytes parsed it.
    """
    files: dict[str, str] = {HOST_REL: ids.sha256_of_file(host_path())}
    root = lib_dir()
    for path in sorted(root.rglob("*.py")):
        files[str(Path(LIB_REL) / path.relative_to(root))] = ids.sha256_of_file(path)
    return {"files": files, "sha256": ids.sha256_of_obj(files)}


def literal(ref: Mapping[str, Any], name: str) -> Any | None:
    """A module-level literal of the adapter file, read without executing the file.

    Returns None when the module assigns no such name. Raises ``ValueError`` when the
    name is assigned something that is not a literal: a probe computed at import time
    would be code running before review.
    """
    if name not in LITERALS:
        raise ValueError(f"{name!r} is not a literal an adapter module publishes")
    path = manifest_mod.script_path(ref)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(value)
    return None


def call(
    ref: Mapping[str, Any],
    function: str,
    args: Sequence[Any],
    *,
    work_dir: Path,
    timeout: float | None = None,
) -> AdapterAnswer:
    """Call one function of the bound adapter module in a subprocess of its own."""
    if function not in FUNCTIONS:
        raise ValueError(f"{function!r} is not a function of the adapter protocol: {FUNCTIONS}")
    module_path = manifest_mod.script_path(ref)  # CONTENT_HASH_MISMATCH when the bytes moved
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha256(f"{function}:{ids.new_id('request')}".encode()).hexdigest()[:12]
    request_path = work_dir / f"adapter_{function}_{tag}.request.json"
    response_path = work_dir / f"adapter_{function}_{tag}.response.json"
    request_path.write_text(json.dumps({
        "module_path": str(module_path), "sha256": ref["sha256"], "function": function,
        "args": list(args), "lib_dir": str(lib_dir()),
    }), encoding="utf-8")
    limit = float(timeout if timeout is not None
                  else config.engineering_budget()["metadata_command_timeout_seconds"])
    env = {**procs.MINIMAL_ENV, "HOME": str(work_dir), "TMPDIR": str(work_dir),
           "PYTHONDONTWRITEBYTECODE": "1"}
    run = procs.run_bounded(
        [sys.executable, "-I", "-B", str(host_path()), str(request_path), str(response_path)],
        timeout=limit, env=env, cwd=str(work_dir),
    )
    try:
        if not run.get("ran"):
            return AdapterAnswer(False, error_type="Timeout" if run.get("reason") == "timeout" else "OSError",
                                 error=f"the adapter host did not finish: {run.get('reason')} "
                                       f"(limit {limit:g} s)", seconds=run.get("seconds"))
        try:
            answer = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return AdapterAnswer(
                False, error_type="NoAnswer", seconds=run.get("seconds"),
                error=f"the adapter host exited {run.get('exit_code')} without an answer: {exc}; "
                      f"stderr: {(run.get('stderr') or '')[-400:]}",
            )
        if answer.get("ok"):
            return AdapterAnswer(True, result=answer.get("result"), seconds=run.get("seconds"))
        return AdapterAnswer(False, error_type=answer.get("error_type"), error=answer.get("error"),
                             missing_function=bool(answer.get("missing_function")),
                             seconds=run.get("seconds"))
    finally:
        for path in (request_path, response_path):
            try:
                path.unlink()
            except OSError:
                pass


__all__ = ["AdapterAnswer", "FUNCTIONS", "LITERALS", "call", "host_path", "lib_dir", "literal",
           "platform_digest"]
