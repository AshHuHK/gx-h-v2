"""Compare the old CLI and native product receipts against a local archive of the v2 baseline."""
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = "2314eed72822387e03378e8e676ad5fa9228a8d0"


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def private_uses(tree):
    """Resolve TeamService constructors and their local variables in P0 modules."""
    uses = []
    for path in sorted((tree / "gx-h/gx_core/p0").glob("*.py")):
        module = ast.parse(path.read_text())
        aliases = {"TeamService"}
        for imp in (n for n in ast.walk(module) if isinstance(n, ast.ImportFrom)):
            aliases.update(a.asname or a.name for a in imp.names if a.name == "TeamService")

        def constructed(value):
            return (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in aliases)

        for method in (n for n in ast.walk(module) if isinstance(n, ast.FunctionDef)):
            nodes = list(ast.walk(method))
            owners = {target.id for n in nodes if isinstance(n, ast.Assign) and constructed(n.value)
                      for target in n.targets if isinstance(target, ast.Name)}
            for attr in (n for n in nodes if isinstance(n, ast.Attribute) and n.attr.startswith("_")):
                if constructed(attr.value) or isinstance(attr.value, ast.Name) and attr.value.id in owners | aliases:
                    uses.append(f"{path.name}:{attr.lineno} {method.name} -> {attr.attr}")
    return uses


def run(tree, command, expected=0):
    env = {**os.environ, "PYTHONPATH": str(tree / "gx-h"), "GX_PYTHON": sys.executable,
           "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(command, cwd=tree, env=env, text=True, capture_output=True, timeout=120)
    if result.returncode != expected:
        raise RuntimeError(f"{command} returned {result.returncode}: {result.stderr[-3000:]}")
    return result.stdout


def commands(tree, state):
    tools = json.loads(run(tree, ["gx-h/bin/gx", "tools", "--names"]))
    schema = json.loads(run(tree, ["gx-h/bin/gx", "schema", "gx_task_submit"]))
    unknown = json.loads(run(tree, ["gx-h/bin/gx", "schema", "gx_absent_fixture_tool"], expected=1))
    help_digests = {name: hash_bytes(run(tree, [f"gx-h/bin/{name}", "--help"]).encode())
                    for name in ("gx", "gx-project", "gx-native", "gx-web", "gx-science")}
    native = json.loads(run(tree, ["gx-h/bin/gx-native", "--project", "compat", "--state-dir", state, "--check"]))
    project = json.loads(run(tree, ["gx-h/bin/gx-project", "--project", "compat", "--state-dir", state, "--status"]))
    assert tools["ok"] and schema["ok"] and not unknown["ok"]
    assert native["host_started"] is False and native["model_turns"] == 0
    return {"--help_stdout_sha256": help_digests, "tools_names_count": len(tools["tools"]),
            "tools_names_sha256": hash_bytes(json.dumps(tools, sort_keys=True).encode()),
            "gx_task_submit_schema_sha256": hash_bytes(json.dumps(schema, sort_keys=True).encode()),
            "unavailable_schema_exit_1": unknown, "native_check": native["status"], "model_turns": 0,
            "project_status": project["status"]}


def invoke_product(fixture):
    direct = set()

    def trace(frame, event, arg):
        if (event == "call" and frame.f_globals.get("__name__") == "gx_core.teams.service"
                and frame.f_code.co_qualname.startswith("TeamService.") and frame.f_back
                and frame.f_back.f_globals.get("__name__", "").startswith("gx_core.p0.")):
            direct.add(frame.f_code.co_name)

    sys.setprofile(trace)
    try:
        response = fixture.invoke(fixture.product)
        assert response["success"], response
    finally:
        sys.setprofile(None)
    return response, sorted(direct)


def scenario(action, state):
    # Use the same input and trusted wire fixture with each release's own imported code.
    spec = importlib.util.spec_from_file_location("receipt_fixture", ROOT / "gx-h/tests/test_p0_service.py")
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    fixture = fixture_module.WorkflowProductService()
    if action == "seed":
        fixture.setUp()
        try:
            response, direct = invoke_product(fixture)
            fixture.conn.close()
            shutil.copytree(fixture.directory, state)
            (state / "product.json").write_text(json.dumps(fixture.product))
        finally:
            fixture.doCleanups()
    else:
        from gx_core.p0 import schema
        from gx_core.p0.roots import Roots
        from gx_core.persistence import store
        fixture.conn = store.open_store(state / "gx.db")
        try:
            schema.install(fixture.conn)
            fixture.run, = fixture.conn.execute("SELECT run_id FROM runs").fetchone()
            fixture.product = json.loads((state / "product.json").read_text())
            fixture.roots = Roots(fixture.conn, run_id=fixture.run, cwd=state)
            fixture.roots.active_turns["reviewer"] = "turn"
            response, direct = invoke_product(fixture)
            assert fixture.conn.execute("SELECT count(*) FROM submissions").fetchone()[0] == 1
            assert fixture.conn.execute("SELECT count(*) FROM p0_root_calls").fetchone()[0] == 1
            assert fixture.conn.execute("SELECT count(*) FROM outbox WHERE topic='submission.accepted'").fetchone()[0] == 1
        finally:
            fixture.conn.close()
    result = json.loads(response["contentItems"][0]["text"])
    assert result["task_status"] == "ACCEPTED"
    return {"submission_id": result["submission_id"], "artifact_ref": result["artifact_ref"],
            "records_ref": result["records_ref"], "wire_response_sha256": hash_bytes(json.dumps(response).encode()),
            "direct_TeamService_calls_from_P0": direct}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        old = work / "baseline"
        old.mkdir()
        packed = subprocess.check_output(["git", "archive", BASE, "gx-h", "bin"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
            archive.extractall(old, filter="data")
        state = work / "native-project"
        run(old, ["gx-h/bin/gx-native", "--project", "compat", "--state-dir", state, "--init-store"])
        args = [sys.executable, str(Path(__file__).resolve())]
        baseline = {"revision": BASE, "static_private_access": private_uses(old), "cli": commands(old, state),
                    "submission": json.loads(run(old, [*args, "seed", work / "v2-receipt"]))}
        assert len(baseline["static_private_access"]) == 6
        (HERE / "before.json").write_text(json.dumps(baseline, indent=2) + "\n")
        # The v1 native-project marker records the installation path. Upgrade the
        # disposable installation at that path, as an operator would during a release.
        old.joinpath("gx-h").rename(work / "old-source")
        shutil.copytree(ROOT / "gx-h", old / "gx-h", ignore=shutil.ignore_patterns("__pycache__", "var"))
        current = {"baseline_revision": BASE, "static_private_access": private_uses(old), "cli": commands(old, state),
                   "cold_replay_of_v2_submission": json.loads(run(old, [*args, "replay", work / "v2-receipt"])),
                   "new_submission": json.loads(run(old, [*args, "seed", work / "v21-receipt"]))}
        assert not current["static_private_access"]
        assert current["cli"] == baseline["cli"]
        assert current["new_submission"]["direct_TeamService_calls_from_P0"] == ["__init__", "submit_workflow_product"]
        assert current["cold_replay_of_v2_submission"]["wire_response_sha256"] == baseline["submission"]["wire_response_sha256"]
        (HERE / "after.json").write_text(json.dumps(current, indent=2) + "\n")
        print("G02/G03: old CLI outputs match, v2 receipt replayed, accepted submission used the public task service")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        print(json.dumps(scenario(sys.argv[1], Path(sys.argv[2]))))
    else:
        main()
