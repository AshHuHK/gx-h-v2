"""Measure the staged P0 dependency closure using Python AST, installed manifests and process audit events."""
import argparse
import ast
from collections import Counter, defaultdict
from importlib.util import resolve_name
import json
import re
from pathlib import Path
import subprocess
import sys

# This test-only site hook is installed solely into the private staged venv.
# An absolute destination keeps it available to GX's detached, environment-scrubbed python -I children.
SITE = '''
import json as _j, os as _o, sys as _s
_root = STAGED_ROOT + "/"
try:
    with open(STAGE_FILE) as _r:
        _stage = _r.read().strip()
except FileNotFoundError:
    _stage = "setup"
_fd = _o.open(TRACE_FILE, _o.O_WRONLY | _o.O_CREAT | _o.O_APPEND, 0o600)
_seen = set()
def _local(raw):
    if not isinstance(raw, str) or not raw or raw.startswith("<") or raw.startswith("-"):
        return ""
    path = _o.path.abspath(raw)
    if not path.startswith(_root):
        return ""
    rel = path[len(_root):]
    if "/__pycache__/" in rel and rel.endswith(".pyc"):
        first, name = rel.rsplit("/__pycache__/", 1)
        rel = first + "/" + name.split(".cpython-", 1)[0] + ".py"
    return rel
def _emit(kind, value):
    key = (_o.getpid(), kind, value)
    if not value or key in _seen:
        return
    _seen.add(key)
    line = [_stage, _o.getpid(), _o.getppid(), kind, value]
    _o.write(_fd, (_j.dumps(line, separators=(",",":"))+"\\n").encode())
def _audit(event, args):
    if event == "exec":
        _emit("executed_source", _local(args[0].co_filename))
    elif event == "open":
        _emit("file_open_attempt", _local(args[0]))
    elif event == "subprocess.Popen":
        program, argv = args[:2]
        if isinstance(argv, (list, tuple)):
            executable = _o.path.basename(str(program))
            selected = next((_local(arg) for arg in argv if _local(arg).endswith((".py", ".ts", ".mjs"))), "")
            if "app-server" in argv:
                selected = "app-server"
            _emit("subprocess", executable + (":" + selected if selected else ""))
_s.addaudithook(_audit)
_emit("process", _local(_s.argv[0]) or _o.path.basename(_s.executable))
'''


def install_observer(venv, root, work):
    python = venv / "bin/python"
    target = Path(subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    trace, stage = work / "audit-events.jsonl", work / "audit-stage"
    stage.write_text("setup")
    header = f"STAGED_ROOT={str(root)!r}\nTRACE_FILE={str(trace)!r}\nSTAGE_FILE={str(stage)!r}\n"
    (target / "sitecustomize.py").write_text(header + SITE)
    return trace, stage


def documents(root):
    directory = root / "gx-h"
    targets = [*directory.glob("gx_core/**/*.py"), *directory.glob("workflows/**/*.py"),
               *directory.glob("adapters/**/*.py"), *directory.glob("examples/**/*.py")]
    entries = [root / "bin/gx-p0", *sorted((directory/"bin").glob("gx*"))]
    paths = {p.relative_to(root).as_posix(): p for p in targets + entries if p.is_file()}
    modules = {}
    for name, path in paths.items():
        if path.suffix == ".py":
            local = path.relative_to(directory).with_suffix("").parts
            modules[".".join(local[:-1] if local[-1] == "__init__" else local)] = name
    return paths, modules


def imports(root):
    paths, modules = documents(root)
    edges = defaultdict(set)
    late = []
    external = defaultdict(set)
    by_path = {v:k for k,v in modules.items()}
    def link(source, target):
        while target:
            if target in modules:
                edges[source].add(modules[target])
            target = target.rpartition(".")[0]

    for name, path in paths.items():
        module = by_path.get(name, "")
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        source = path.read_text()
        if source.startswith("#!/bin/sh"):
            match = re.search(r" -m ([\w.]+)", source)
            if not match:
                raise ValueError("Unrecognized CLI shell entry " + name)
            target = match[1]
            link(name, target + ".__main__" if target + ".__main__" in modules else target)
            continue
        tree = ast.parse(source, filename=name)
        for item in ast.walk(tree):
            if isinstance(item, ast.Import):
                for alias in item.names:
                    link(name, alias.name)
                    if alias.name.split(".",1)[0] not in sys.stdlib_module_names and alias.name not in modules:
                        external[name].add(alias.name)
            elif isinstance(item, ast.ImportFrom):
                target = item.module or ""
                if item.level:
                    target = resolve_name("."*item.level+target, package)
                link(name, target)
                for alias in item.names:
                    link(name, target+"."+alias.name)
                if (target and not item.level and target not in modules
                        and target.split(".",1)[0] not in sys.stdlib_module_names):
                    external[name].add(target)
            elif isinstance(item, ast.Call):
                call = ast.unparse(item.func)
                if call.endswith(("import_module", "spec_from_file_location", "__import__")):
                    late.append({"source":name, "line":item.lineno, "expression":ast.unparse(item)[:180]})
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id in {"PROVIDERS", "OWNERS"}:
                # Registered GX providers and the lazy seams resolve module names stored in literals.
                for value in ast.walk(item.value):
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        link(name, value.value.split(":")[0])
    manifest = root / "gx-h/config/workflow_packages.json"
    reg = "gx-h/gx_core/orchestration/service.py"
    manifest_edges = []
    if manifest.is_file():
        settings = json.loads(manifest.read_text())
        for entry in settings["packages"]:
            names = [entry] if isinstance(entry,str) else [entry["module"], *entry.get("compat_hooks",{}).values()]
            for target in names:
                manifest_edges.append(target)
                link(reg, target.split(":")[0])
        for value in settings.get("examples", {}).values():
            if isinstance(value,dict) and isinstance(value.get("factory"),str):
                manifest_edges.append(value["factory"])
                link("gx-h/gx_core/p0/cli.py", value["factory"].split(":")[0])
    # Job executables and parsers are selected by the installed capability manifests.
    for manifest_file in sorted((root / "gx-h/adapters").rglob("*.json")):
        data = json.loads(manifest_file.read_text())
        if not isinstance(data, dict) or "manifest" not in data:
            continue
        for key in ("entry_script", "parser"):
            target = data.get(key)
            if isinstance(target, str) and "gx-h/" + target in paths:
                manifest_edges.append(manifest_file.relative_to(root).as_posix() + "#" + key + "=" + target)
                edges["gx-h/gx_core/jobs/service.py"].add("gx-h/" + target)
    for child, owner in (("runner.py", "executor.py"), ("resource_guard.py", "runner.py"), ("adapter_host.py", "adapter.py")):
        edges["gx-h/gx_core/jobs/" + owner].add("gx-h/gx_core/jobs/" + child)
    return paths, edges, sorted(late,key=lambda row:(row["source"],row["line"])), sorted(manifest_edges), external


def reachable(edges, start):
    result = set()
    queue = list(start)
    while queue:
        name = queue.pop()
        if name not in result:
            result.add(name)
            queue.extend(edges[name] - result)
    return result


def classify(name, primary, compatibility, executed):
    if (name.startswith(("gx-h/gx_core/cli/","gx-h/gx_core/web/","gx-h/gx_core/mcp/","gx-h/workflows/compat_v20/"))
            or "/manager" in name or name.startswith("gx-h/bin/")):
        return "compatibility"
    offline_helpers = {"gx-h/adapters/hosts/generate.py", "gx-h/examples/p01_measure.py",
                       "gx-h/examples/p01_inventory.py", "gx-h/gx_core/capabilities/matrix.py",
                       "gx-h/gx_core/knowledge/index_run.py", "gx-h/gx_core/jobs/applicability.py",
                       "gx-h/gx_core/scheduling/routing.py", "gx-h/gx_core/hostobs/caller.py"}
    if name in offline_helpers:
        return "optional"
    if name.startswith("gx-h/adapters/local/selftest/") or name.removesuffix(".py").endswith(
            ("claude_code","pool","source_host_adapter","record","observation")) and name not in executed:
        return "optional"
    if name in primary or name in executed:
        return "p0_required"
    if name in compatibility:
        return "compatibility"
    return "outside_measured_roots"


def observe(root, trace, out):
    paths, edges, late, registrations, external = imports(root)
    primary = reachable(edges, ["bin/gx-p0"])
    compatibility = reachable(edges, [name for name in paths if name.startswith("gx-h/bin/")])
    by_stage = {}
    for raw in trace.read_text().splitlines():
        stage, pid, ppid, kind, value = json.loads(raw)
        group = by_stage.setdefault(stage, defaultdict(lambda: defaultdict(set)))
        group[kind][value].add(pid)
    stages = {stage: {kind: {value:len(pids) for value,pids in sorted(records.items())}
                       for kind,records in sorted(group.items())} for stage,group in sorted(by_stage.items())}
    candidate = {name: data for name,data in stages.items() if name.startswith("candidate") and "compat" not in name}
    executed = {name for data in candidate.values() for name in data.get("executed_source",{})}
    required_stages = {"original-v2","restored-original-v2","candidate-before-rollback","candidate-final",
                       "candidate-cli-help", "candidate-compat-help"}
    if not required_stages <= set(stages):
        raise AssertionError("Missing staged runtime observations: " + str(required_stages-set(stages)))
    final = stages["candidate-final"]
    required_suffixes = ("/p0/cli.py", "/p0/driver.py", "/jobs/runner.py", "/p01_grid/worker.py")
    for suffix in required_suffixes:
        if not any(name.endswith(suffix) for name in final.get("executed_source", {})):
            raise AssertionError("Staged official/CPU execution failed to observe " + suffix)
    if not any("codex:app-server" == name for name in final.get("subprocess",{})):
        raise AssertionError("Official App Server process was not observed")
    cold_start = set(stages["candidate-cli-help"].get("executed_source", {}))
    compat_executed = set(stages["candidate-compat-help"].get("executed_source", {}))
    counts = Counter()
    records = []
    for name in sorted(paths):
        category = classify(name, primary, compatibility, executed)
        counts[category] += 1
        records.append({"file":name, "classification":category, "p0_static":name in primary,
                        "compat_static":name in compatibility, "observed_candidate_execution":name in executed,
                        "observed_cold_cli": name in cold_start, "observed_compat_cli": name in compat_executed,
                        "internal_dependencies":sorted(edges[name]), "nonstdlib_imports":sorted(external[name])})
    summary = {"entry":"bin/gx-p0","cold_cli_executed_sources":len(cold_start),
               "compat_cli_executed_sources":len(compat_executed),"static_units":len(paths),"p0_static_reachable":len(primary),
               "compat_static_reachable":len(compatibility),"candidate_executed_sources":len(executed),
               "runtime_only_sources":sorted(executed-primary),"module_classification_counts":dict(sorted(counts.items())),
               "candidate_final_subprocess":final.get("subprocess",{}),
               "candidate_final_directly_observed_cpu_workers":{k:n for k,n in final["executed_source"].items()
                                                                if k.endswith(("/p01_grid/worker.py", "/jobs/runner.py"))}}
    # These references are delivered separately and have explicit non-runtime purposes.
    references = {
        "audit/":"v2 import and native acceptance inventory",
        "acc_1/":"v2 review, acceptance and numerical evidence",
        "acceptance/runs/":"recorded earlier test outputs; schema manifest also feeds the release installer",
        "codex_adapter/app_server.py":"reference JSON-RPC client; production imports gx_core.hostobs.app_server_client",
        "gx_reference/":"reference intent policy and schema; database upgrade reads gx-h/contracts and gx_core/p0 SQL",
    }
    result = {"packaged_historical_references":references, "summary":summary,"manifest_registrations":registrations,"late_binding_calls":late,"modules":records,
              "stage_exec_reads_and_spawns":stages}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result, sort_keys=True, indent=2)+"\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(observe(args.install.resolve(),args.trace,args.output), indent=2))
