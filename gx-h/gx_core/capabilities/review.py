"""What a reviewer of a capability needs in front of it, taken from what the backend holds.

The reviewer sees the declared launch, weight loading, scripts, method card, static
scan and existing observations. A pinned local entry can answer a bounded metadata
probe before independent review. Scientific Jobs require a reviewed capability.

:func:`material` collects it as data; :func:`render` lays it out as the brief section.
Nothing here is the author's description of its own work: the launch is resolved from
the manifest and the environment row, the digests are the registered ones, the scan
findings are the latest check run's.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import GxError
from ..persistence import store
from . import environments, ladder, registry
from . import manifest as manifest_mod

#: Stable review item IDs. Earlier frozen packages carry q6, the generic slow-request check.
QUESTIONS = {
    "q1": "Does the command select the device declared by its resource requirements?",
    "q2": "Arguments. Does the argv template fix every option that changes what is computed, so that "
          "two jobs with the same parameters are the same computation? Every `{param.NAME}` is a "
          "choice the caller makes: is each one meant to be?",
    "q3": "Inputs. Does `validate_inputs` refuse what the tool would accept silently and answer "
          "incorrectly?",
    "q4": "Outputs. Does `parse` check that what it read is about the records that were asked for "
          "(matching identifiers and order), and raise when it is not? An adapter that returns a "
          "document whatever the tool wrote passes its own smoke test.",
    "q5": "Smoke request. Could it fail? An input no tool can get wrong observes nothing.",
    "q7": "Method card. Do family, mechanism, objective and estimator say what this code does, and does "
          "the training-data lineage say only what has been established (and say so where it has not)?",
    "q8": "Weights. Is each file loaded in the mode the manifest states? A full unpickle runs code: is "
          "its upstream digest one the upstream itself published or tracks?",
    "q9": "Scan findings. For each one below: is it what this adapter has to do (start its tool, write "
          "into its output directory), or something it has no reason to do?",
    "q10": "Licence. Is the licence the one in the file named as its basis?",
}


def questions_for(kind: str | None) -> list[tuple[str, str]]:
    """The applicable questions for a new review package, in display order."""
    return list(QUESTIONS.items())


def _launch(view: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    manifest = view["manifest"]
    entry = manifest["entry"]
    launch: dict[str, Any] = {
        "argv_template": list(entry["argv_template"]), "cwd": entry.get("cwd") or "{out.dir}",
        "pythonpath": list(entry.get("pythonpath") or []), "launch_env": dict(entry.get("launch_env") or {}),
        "input_roles": manifest_mod.input_roles(manifest),
        "parameters": manifest_mod.parameter_names(manifest),
    }
    try:
        env = environments.get(conn, view["env_id"])
        env_view = manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                        binaries=dict(env["binary_paths"]), prefix=env["prefix"])
        plan = manifest_mod.launch_plan(
            view["bundle"], env_view,
            inputs={role: f"<in.{role}>" for role in launch["input_roles"]},
            out_dir="<out.dir>", home_dir="<job>/home", tmp_dir="<job>/tmp",
            parameters={name: f"<param.{name}>" for name in launch["parameters"]},
            gpu_device="<leased device>" if manifest["resources"].get("gpu") else None,
            gpu_memory_mib=(manifest["resources"].get("gpu") or {}).get("memory_mib"),
        )
        launch.update(resolved_argv=plan.argv, resolved_cwd=plan.cwd, environment=plan.env,
                      environment_status=env["status"], environment_probed_at=env["probed_at"],
                      dead_console_scripts=(env["dead_entrypoints"] or {}).get("dead", 0)
                      if isinstance(env["dead_entrypoints"], dict) else 0)
    except GxError as exc:
        launch["not_resolvable"] = exc.message
    return launch


def material(conn: sqlite3.Connection, capability_version: str, *,
             questions: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Everything the backend holds that bears on a review of this version."""
    view = registry.candidate(conn, capability_version)
    manifest, bundle = view["manifest"], view["bundle"]
    findings: list[dict[str, Any]] = []
    scan = conn.execute(
        "SELECT check_run_id, result, diagnostics_json, created_at FROM check_runs WHERE subject_kind='asset'"
        " AND subject_id=? AND subject_sha256=? AND check_id='scope_scan' AND superseded=0"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (view["capability_id"], view["manifest_sha256"])).fetchone()
    if scan is not None:
        for item in store.loads(scan, "diagnostics_json", []) or []:
            findings.append({key: item.get(key) for key in ("fingerprint", "script", "category", "line", "what")})
    records = ladder.latest(conn, view)
    probes = {rung: record["result"] for rung, record in records.items()}
    if ladder.L3 in probes:
        what_has_run = f"smoke probe recorded {probes[ladder.L3]}"
        if "reused_from" in records[ladder.L3]:
            source = records[ladder.L3]["reused_from"]
            what_has_run = (f"Original software controls from {source['capability_version']} "
                            f"({source['manifest_sha256']}, {source['wall_seconds']} s); "
                            f"smoke Job {source['job_id']}. This version extends the CPU deadline.")
    elif ladder.L2 in probes:
        argv = records[ladder.L2]["detail"].get("argv") or []
        flag = f" ({argv[-1]})" if argv and argv[-1] in ("--version", "--help", "-h", "-V") else ""
        what_has_run = f"entrypoint probe{flag} recorded {probes[ladder.L2]}. No smoke result is recorded"
    else:
        what_has_run = "only static file and environment observations are available"
    return {
        "capability_version": view["capability_version"], "manifest_sha256": view["manifest_sha256"],
        "kind": view["kind"], "status": view["status"], "method_card": dict(manifest["method_card"]),
        "launch": _launch(view, conn), "resources": dict(manifest["resources"]),
        "weights": [dict(weight) for weight in manifest["weights"]],
        "required_data": list(manifest["required_data"]), "outputs": [dict(o) for o in manifest["outputs"]],
        "source": dict(manifest["source"]), "licence": manifest["licence"],
        "licence_basis": manifest["licence_basis"], "known_answer": manifest.get("known_answer"),
        "limitations": manifest["limitations"],
        "scripts": {name: dict(bundle[name]) for name in ("parser", "entry_script") if bundle.get(name)},
        "scan": {"check_run_id": scan["check_run_id"] if scan is not None else None,
                 "result": scan["result"] if scan is not None else "NOT_RUN", "findings": findings},
        "probes": probes, "reused_controls": {rung: record["reused_from"] for rung, record in records.items()
                                              if "reused_from" in record},
        "what_has_run": what_has_run,
        "questions": questions_for(view["kind"]) if questions is None else questions,
    }


def render(found: dict[str, Any]) -> str:
    """The brief section, as text a worker reads."""
    launch = found["launch"]
    card = found["method_card"]
    lines = [
        f"Capability {found['capability_version']}, a {found['kind']}, content sha256 {found['manifest_sha256']}.",
        f"Status {found['status']}. What has run so far: {found['what_has_run']}.",
        "",
        "Method card (the only source of method identity once this is ACTIVE):",
        *[f"  {key}: {card.get(key)}" for key in ("family", "mechanism", "objective", "estimator",
                                                    "training_data_lineage")],
        f"  citation_evidence: {card.get('citation_evidence') or 'none given'}",
        "",
        "Launch, as the backend resolves it (one argv element per line; nothing passes through a shell):",
        *[f"  {token!r}" for token in launch.get("resolved_argv") or launch["argv_template"]],
        f"  working directory: {launch.get('resolved_cwd') or launch['cwd']}",
        f"  module search path: {launch['pythonpath'] or 'none'}; fixed environment values: {launch['launch_env'] or 'none'}",
        f"  environment the tool is given: {sorted((launch.get('environment') or {}))}",
        f"  CUDA_VISIBLE_DEVICES: {(launch.get('environment') or {}).get('CUDA_VISIBLE_DEVICES')!r} "
        "(empty unless the manifest declares a GPU need)",
        f"  input roles: {launch['input_roles']}; parameters a caller chooses: {launch['parameters'] or 'none'}",
        f"  declared resources: {found['resources']}",
    ]
    if launch.get("not_resolvable"):
        lines.append(f"  NOT RESOLVABLE on this host: {launch['not_resolvable']}")
    lines += ["", "Weights:"]
    lines += [f"  {w['path']}  {w['bytes']} bytes  sha256 {w['sha256']}  load mode {w['load_mode']}"
              f"  upstream digest {w.get('upstream_digest') or 'none'}" for w in found["weights"]] or ["  none"]
    lines += ["", "Scripts (read each with gx_artifact_get; the sha256 is what is bound):"]
    lines += [f"  {name}: {ref['path']}  artifact {ref['artifact_ref']}  sha256 {ref['sha256']}"
              for name, ref in found["scripts"].items()] or ["  none"]
    lines += ["", f"Static scan ({found['scan']['result']}, check run {found['scan']['check_run_id']}):"]
    lines += [f"  {f['fingerprint']}  {f['script']}:{f['line']}  {f['category']}: {f['what']}"
              for f in found["scan"]["findings"]] or ["  no findings"]
    lines += ["", f"Probes on record for these bytes: {found['probes'] or 'none'}",
              f"Licence: {found['licence']}; read from: {found['licence_basis']}",
              f"Source: {found['source']}",
              f"Known-answer control: {found['known_answer'] or 'none declared'}",
              f"Stated limitations: {found['limitations']}",
              "", "What only a reader of the adapter can decide:"]
    lines += [f"  {item_id}. {question}" for item_id, question in found["questions"]]
    return "\n".join(lines)


__all__ = ["QUESTIONS", "material", "questions_for", "render"]
