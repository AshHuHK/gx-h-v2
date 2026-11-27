"""Upgrade a disposable, ordinarily admitted baseline Goal in place and resume its bound Lead."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


BASE = "4759c260"
ROOT = Path(__file__).resolve().parents[2]
EMPTY_GRANT = {
    "evidence_policy": "gx.evidence/6.2.4", "research_effort": "deep", "research_modules": [],
    "plugins": [], "capabilities": [], "capability_options": [], "input_files": [],
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def stage(product, work, action):
    sys.path[:0] = [str(product), str(product / "gx-h")]
    from acceptance.v62a.interleavings_fixture import Actor, Scenario, draft
    from gx_core import contracts
    from gx_core.capabilities import shipped
    from gx_core.persistence import store
    from gx_core.errors import GxError
    from gx_core.orchestration import service

    pinned = work.name == "pinned"
    settings = {**EMPTY_GRANT}
    if work.name == "quick_empty":
        settings["research_effort"] = "quick"
    if pinned:
        settings.update(capabilities=[shipped.ARTIFACT_ANALYSIS],
                        capability_options=[{"method": shipped.GRID, "digest": shipped.pin(shipped.GRID)}])
    with Scenario(work, spec_updates=settings, reopen=action == "after") as goal:
        run_id = goal.run_id
        actor_path = work / "original-lead.json"
        if action == "before":
            actor = goal.lead
            actor_path.write_text(json.dumps(asdict(actor)))
        else:
            actor = Actor(**json.loads(actor_path.read_text()))
            goal.roots.active_turns[actor.thread] = actor.turn
        run = goal.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        frozen = goal.brief(actor)
        installed = contracts.contract_digest()["__combined__"]
        pin_matches = store.contract_pin_matches(goal.conn, run["contract_sha256"], installed)
        try:
            service.require_store_pins(goal.conn, run, None)
        except GxError as exc:
            admission = {"ok": False, "error": exc.code, "message": str(exc)}
        else:
            admission = {"ok": True}
        surface = {t['name']: t for t in goal.roots._tools_for(goal.roots._binding(actor.thread))}
        query = surface.get("gx_query_catalog", {}).get("inputSchema")
        decision = {}
        selection = {}
        if action == "after" and pin_matches:
            if pinned:
                selection = goal.call(actor, "gx_select_capability", {"action": "enable", "revision": 0,
                                       "method": shipped.GRID}, "legacy-enable-original-option")
            decision = goal.call(actor, "gx_decide_revision", {"decision": draft()}, "legacy-continue-without-survey")
        from gx_core.p0 import capability_views
        inputs = json.loads(run["inputs_json"])["inputs"]
        opts = capability_views.goal_options(inputs) if hasattr(capability_views, "goal_options") else capability_views.options(inputs.get("p0_spec", {}))
        return {
            "run_id": run_id, "actor": asdict(actor), "historical_contract": run["contract_sha256"],
            "installed_contract": installed, "contract_pin_matches": pin_matches, "admission_pin_check": admission,
            "tool_names": sorted(surface),
            "catalog_input_schema_sha256": hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest(),
            "discovery_policy": inputs.get("discovery_policy"),
            "original_brief_sha256": hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
            "inputs_sha256": hashlib.sha256(run["inputs_json"].encode()).hexdigest(), "activation_options": opts,
            "original_active_methods": [c["capability_version"] for c in inputs["capability_catalog"]],
            "selection": selection, "migration_receipts": [dict(r) for r in goal.conn.execute(
                "SELECT event_id,payload_json FROM events WHERE kind='store.contract_resynced' ORDER BY event_id")],
            "decision": decision,
        }


def run(product, work, action):
    env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.check_output([sys.executable, "-B", str(Path(__file__).resolve()),
                                      "--stage", action, "--product", str(product), "--work", str(work)], env=env, cwd=product)
    return json.loads(result)


def experiment(output, *, baseline_migrations=False):
    if output.exists():
        raise ValueError("Choose a new receipt path")
    with tempfile.TemporaryDirectory(prefix="gx-plugin-legacy-upgrade-") as directory:
        product = Path(directory) / "install"
        product.mkdir()
        paths = ["gx-h", "bin/gx-p0", "acceptance/v62a/interleavings_fixture.py", "examples/v62a-data-inventory"]
        subprocess.run(["tar", "xf", "-", "-C", str(product)], input=git("archive", BASE, *paths), check=True)
        scopes = ("empty", "pinned", "quick_empty")
        pairs = {scope: {"before": run(product, Path(directory) / scope, "before")} for scope in scopes}
        # Emulate normal deployment at the original install path. Never edit the native state marker.
        for path in git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "gx-h", "bin/gx-p0").split(b"\0"):
            if not path:
                continue
            name = Path(os.fsdecode(path))
            dest = product / name
            if not (ROOT / name).exists():
                dest.unlink(missing_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
        if baseline_migrations:
            manifest = "gx-h/config/native_contract_migrations.json"
            (product / manifest).write_bytes(git("show", f"{BASE}:{manifest}"))
        for scope, pair in pairs.items():
            pair["after"] = run(product, Path(directory) / scope, "after")
    for scope, pair in pairs.items():
        before, after = pair["before"], pair["after"]
        unchanged = all(before[k] == after[k] for k in ("run_id", "actor", "historical_contract", "inputs_sha256",
                                                         "original_brief_sha256", "original_active_methods", "activation_options",
                                                         "tool_names", "catalog_input_schema_sha256", "discovery_policy"))
        authorized = (after["activation_options"] == {} and after["original_active_methods"] == []) if scope != "pinned" else (
            set(after["activation_options"]) == {"fixture-grid@1"} and after["selection"].get("revision") == 1
            and after["original_active_methods"] == ["artifact-analysis@1"])
        if scope == "quick_empty":
            authorized = authorized and "gx_query_catalog" not in after["tool_names"]
        pair["passed"] = bool(unchanged and authorized and before["contract_pin_matches"]
                              and after["contract_pin_matches"] and after["admission_pin_check"]["ok"]
                              and after["decision"].get("decision_ref")
                              and len(after["migration_receipts"]) == 1)
    passed = all(pair["passed"] for pair in pairs.values())
    data = {"baseline": git("rev-parse", BASE).decode().strip(),
            "transport": "gx-p0 start, isolated controlled native callback; no inference",
            "scopes": pairs, "migration_manifest": "original" if baseline_migrations else "candidate", "passed": passed}
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("before", "after"))
    parser.add_argument("--product", type=Path)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-migrations", action="store_true", help="Reproduce the missing migration declaration in the disposable candidate")
    args = parser.parse_args()
    if args.stage:
        print(json.dumps(stage(args.product, args.work, args.stage)))
    elif args.output:
        sys.exit(0 if experiment(args.output, baseline_migrations=args.baseline_migrations) else 1)
    else:
        parser.error("specify --output")
