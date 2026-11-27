"""Read the scientific role profiles selected for the deployment or saved Run."""
from contextlib import closing
import json
from pathlib import Path

from ..hostobs import host_policy
from . import profiles


def _local_profiles():
    routes = host_policy.p0_policy()[0]["p0_role_profiles"]
    config = profiles.codex_config()
    providers = {}
    for name in sorted({route["provider"] for route in routes.values()}):
        try:
            profiles.provider_endpoint(name, config, test_mode=profiles._testing(None))
            providers[name] = {"configured": True, "wire_api": "responses"}
        except ValueError as exc:
            providers[name] = {"configured": False, "error": str(exc)}
    return {"roles": routes, "providers": providers, "configured": all(p["configured"] for p in providers.values()),
            "custom_provider_ids": sorted(set(config.get("model_providers", {})) - {"openai"}),
            "inference": "not_run", "mode": "local_configuration"}


def _saved_profiles(directory, run_id=None):
    from .cli import existing_db
    from .driver import project
    with closing(existing_db(directory.expanduser().resolve(), readonly=True)) as conn:
        run = project(conn, run_id)
        if run is None:
            raise ValueError("There is no GX project in this state directory")
        document, _ = host_policy.current_for_run(run)
        host = json.loads(run["host_json"] or "{}").get("policy_selection", {})
        roots = []
        for record in conn.execute("SELECT * FROM p0_roots WHERE run_id=? ORDER BY rowid", (run["run_id"],)):
            row = dict(record)
            roots.append({"role": row["role"], "task_id": row["task_id"], "attempt": row["attempt"],
                          "thread_id": row["thread_id"],
                          "requested": {"provider": row.get("provider") or host.get("model_provider"),
                                        "model": row["model"], "effort": row["effort"]},
                          "gx_tools": json.loads(row.get("tool_names_json") or "null"),
                          "codex_effective": json.loads(row.get("effective_profile_json") or "null")})
        return {"mode": "saved_project", "run_id": run["run_id"],
                "default_provider": host.get("model_provider"), "roles": document.get("p0_role_profiles"), "roots": roots}


def _show(args):
    if args.state is not None:
        report = _saved_profiles(args.state, args.run_id)
    else:
        report = _local_profiles()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return int(not report.get("configured", True))


def add_command(subparsers):
    parser = subparsers.add_parser("profiles", help="Show the model and tools selected for each GX role")
    parser.add_argument("--run-id", help="Select a saved Run")
    parser.add_argument("--state", type=Path, help="Read the profiles saved in this workspace")
    parser.set_defaults(deployment_handler=_show)
