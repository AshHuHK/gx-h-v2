"""Exercise production role selection and cold recovery on two local Responses peers."""

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile

from e2e_grid import fixture_env
from e2e_responses import local_responses
from gx_core.p0.cli import demo_spec


PACKAGE = Path(__file__).resolve().parents[1]
MAIN_MODEL = "gx-scripted-main"
REVIEW_MODEL = "gx-scripted-review"
REVIEW_TOOLS = ["gx_read_artifact", "gx_submit_product"]


def exercise(work, output, binary):
    with local_responses(output / "primary.jsonl") as first, local_responses(output / "review.jsonl") as second:
        env = fixture_env(work, first, binary)
        version = subprocess.check_output([binary, "--version"], env=env, text=True, timeout=15).strip()
        assert version == "codex-cli 0.169.0"
        env.pop("GX_P0_TEST")
        env.pop("GX_P0_TEST_PROVIDER")
        profile = work / "roles.json"
        env["GX_P0_PROFILE"] = str(profile)
        profile.write_text(json.dumps({
            "default": {"provider": "gx_loopback", "model": MAIN_MODEL},
            "roles": {"evidence_reviewer": {"provider": "review_peer", "model": REVIEW_MODEL, "tools": REVIEW_TOOLS}},
        }))
        home = Path(env["CODEX_HOME"])
        cfg = home / "config.toml"
        # These peers implement direct Responses calls. Describe their own behavior
        # in Codex's catalog schema, independently of upstream model tool modes.
        native_catalog = PACKAGE / "vendor/codex/codex-rs/models-manager/models.json"
        template = json.loads(native_catalog.read_text())["models"][0]
        catalog = home / "models.json"
        catalog.write_text(json.dumps({"models": [
            {**template, "slug": name, "display_name": name, "tool_mode": "direct", "use_responses_lite": False}
            for name in (MAIN_MODEL, REVIEW_MODEL)]}))
        cfg.write_text("model_catalog_json = " + json.dumps(str(catalog)) + "\n" + cfg.read_text() +
                       '\n[model_providers.review_peer]\nname = "GX local review peer"\n' +
                       f'base_url = "http://127.0.0.1:{second.server_port}/v1"\nwire_api = "responses"\n')
        spec = work / "spec.json"
        spec.write_text(json.dumps({**demo_spec(), "max_rounds": 1}))
        state = work / "project"
        def cli(*args):
            proc = subprocess.run([str(PACKAGE / "bin/gx-p0"), *args], cwd=PACKAGE, env=env, capture_output=True,
                                  text=True, timeout=120)
            if proc.returncode:
                raise RuntimeError(f"gx-p0 {args[0]} exited {proc.returncode}: {proc.stderr.strip()}")
            return json.loads(proc.stdout)
        initial = cli("start", "--spec", str(spec), "--state", str(state))
        cli("stop", "--state", str(state))
        # A fresh operator shell can resume without the original role-selection file.
        profile.unlink()
        before_restart = len(first.rows) + len(second.rows)
        replay = cli("start", "--spec", str(spec), "--state", str(state))
        assert initial["run_id"] == replay["run_id"] and before_restart == len(first.rows) + len(second.rows)
        del env["GX_P0_PROFILE"]
        final = cli("resume", "--state", str(state), "--timeout", "90")
        if final["status"] != "COMPLETED" or initial["run_id"] != final["run_id"]:
            raise RuntimeError(f"Project did not complete its original run: {final['status']}")
        before = len(first.rows) + len(second.rows)
        cli("run", "--state", str(state), "--timeout", "5")
        assert len(first.rows) + len(second.rows) == before
        saved = cli("profiles", "--state", str(state))
        assert saved["run_id"] == final["run_id"] and saved["roots"]
        assert all(root["requested"] == root["codex_effective"] for root in saved["roots"])

        with sqlite3.connect(state / "gx.db") as conn:
            conn.row_factory = sqlite3.Row
            roots = [dict(row) for row in conn.execute(
                "SELECT r.root_key,r.thread_id,r.task_id,r.role,r.provider,r.model,r.effort,"
                "r.tool_names_json,r.effective_profile_json,t.required_model FROM p0_roots r "
                "LEFT JOIN tasks t USING(task_id) ORDER BY r.rowid")]
            receipts = [dict(row) for row in conn.execute(
                "SELECT r.role,d.thread_id,d.turn_id,u.total_tokens,b.status "
                "FROM p0_root_deliveries d JOIN p0_roots r USING(thread_id) "
                "JOIN p0_root_turn_usage u USING(thread_id,turn_id) "
                "JOIN budget_reservations b USING(reservation_ref) WHERE d.method='turn/start'")]
        for root in roots:
            effective = json.loads(root.pop("effective_profile_json"))
            root["gx_tools"] = json.loads(root.pop("tool_names_json"))
            assert effective == {key: root[key] for key in ("provider", "model", "effort")}
            root["codex_effective"] = effective
            if root["task_id"]:
                assert root["required_model"] == root["model"]
        reviewer, = [r for r in roots if r["role"] == "evidence_reviewer"]
        assert (reviewer["provider"], reviewer["model"], reviewer["effort"]) == ("review_peer", REVIEW_MODEL, "max")
        assert sorted(reviewer["gx_tools"]) == REVIEW_TOOLS
        assert second.rows and all(r["model"] == REVIEW_MODEL and r["reasoning_effort"] == "max" and
                                   r["gx_tools"] == REVIEW_TOOLS for r in second.rows)
        assert first.rows and all(r["model"] == MAIN_MODEL and r["peer"] == "127.0.0.1" for r in first.rows)
        assert receipts and all(r["total_tokens"] > 0 and r["status"] == "SETTLED" for r in receipts)
        env["GX_P0_PROFILE"] = str(profile)
        profile.write_text(json.dumps({"default":{"provider":"review_peer", "model":"gx-unlisted-model"}}))
        before_rejection = len(first.rows) + len(second.rows)
        denied = subprocess.run([str(PACKAGE / "bin/gx-p0"), "doctor", "--role", "main", "--smoke"],
                                cwd=PACKAGE, env=env, capture_output=True, text=True, timeout=30)
        rejected = json.loads(denied.stdout)
        assert denied.returncode == 1 and not rejected["ready"]
        assert rejected["missing_efforts"] == [{"provider":"review_peer", "model":"gx-unlisted-model", "effort":"ultra"}]
        assert len(first.rows) + len(second.rows) == before_rejection
        return {"run_id": final["run_id"], "status": final["status"], "codex": version.removeprefix("codex-cli "),
                "runtime": "official Codex, two scripted loopback Responses endpoints, production GX effort policy",
                "external_inference": False, "review_requests": len(second.rows), "primary_requests": len(first.rows),
                "after_terminal_reopen_new_requests": 0, "roots": roots, "turn_usage": receipts,
                "unadvertised_model": {"ready":False, "new_requests":0, "missing":rejected["missing_efforts"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    binary = shutil.which(args.codex)
    if not binary:
        raise RuntimeError("Select the pinned 0.169.0 Codex binary")
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="gx-role-profiles-") as work:
        result = exercise(Path(work), args.output, binary)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
