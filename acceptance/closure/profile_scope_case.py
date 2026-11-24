"""Exercise profile selection, Run persistence and replay through packaged entry points."""

from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from unittest.mock import patch

from acceptance.closure.profile import test_environment

REPO = Path(__file__).resolve().parents[2]
OVERRIDE = "GX_TEST_RESOURCE_PROFILE"
SCOPE = "GX_ACCEPTANCE_RESOURCE_SCOPE"


def run(args, work):
    evidence, commands = [], []
    env = test_environment(work / "controlled", python=args.python, binary=args.binary)
    # A normal real-resource probe must survive an inherited acceptance override
    # and respect any device visibility restriction already set by its deployment.
    with patch.dict(os.environ, {OVERRIDE: "deterministic", SCOPE: "invalid"}):
        real = test_environment(work / "host", profile="real", python=args.python, binary=args.binary)
    assert OVERRIDE not in real and SCOPE not in real
    assert real.get("CUDA_VISIBLE_DEVICES") == os.environ.get("CUDA_VISIBLE_DEVICES")

    def invoke(label, argv, child, *, refuse=False):
        proc = subprocess.run(argv, env=child, cwd=REPO, text=True, capture_output=True, timeout=45)
        file = label + ".log"
        (args.out / file).write_text(proc.stdout + proc.stderr)
        evidence.append(file)
        commands.append({"label": label, "argv": [str(value) for value in argv], "exit_code": proc.returncode,
                         "environment_names": sorted(child)})
        if refuse:
            assert proc.returncode and OVERRIDE in proc.stderr, (label, proc.stderr)
            return
        assert proc.returncode == 0, (label, proc.stderr)
        return proc.stdout

    gx = REPO / "bin/gx-p0"
    entries = [gx, *(REPO / "gx-h/bin").iterdir()]
    bare = {**real, OVERRIDE: "deterministic"}
    for entry in sorted(entries):
        for value in ("deterministic", "real"):
            invoke(f"refused-{entry.name}-{value}", [entry, "--help"], {**real, OVERRIDE: value}, refuse=True)
    invoke("refused-guessed-marker", [gx, "--help"], {**bare, SCOPE: f"{os.getpid()}/00:00:0"}, refuse=True)
    module = [args.python, "-B", "-m", "gx_core.p0.cli", "--help"]
    invoke("refused-python-module", module, {**bare, "PYTHONPATH": str(REPO / "gx-h")}, refuse=True)
    # A real lock owned by a sibling, then its expired locator, are insufficient.
    sibling_code = ("from acceptance.closure.profile import controlled_resources; import sys; "
                    "print(controlled_resources()['GX_ACCEPTANCE_RESOURCE_SCOPE'],flush=True); sys.stdin.readline()")
    with subprocess.Popen([args.python, "-u", "-c", sibling_code], env=real, cwd=REPO, text=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as sibling:
        locator = sibling.stdout.readline().strip()
        assert locator
        invoke("refused-unrelated-launcher", [gx, "--help"], {**bare, SCOPE: locator}, refuse=True)
        sibling.communicate("\n", timeout=5)
    invoke("refused-expired-launcher", [gx, "--help"], {**bare, SCOPE: locator}, refuse=True)
    invoke("ordinary-cli", [gx, "--help"], real)
    # Two exec boundaries and closed inherited descriptors test the real nesting path.
    nested = [args.python, "-B", "-c", "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:]))", gx, "--help"]
    invoke("controlled-grandchild", nested, env)

    cases = {}
    for mode, selected, opposite in (("deterministic", env, real), ("real", real, env)):
        state = work / mode
        metrics = args.out / (mode + "-transactions.jsonl")
        selected = {**selected, "GX_CODEX_OFFLINE": "1", "GX_METRICS_FILE": str(metrics)}
        opposite = {**opposite, "GX_CODEX_OFFLINE": "1"}
        start = [gx, "start", "--state", state, "--demo-grid", "--run-key", "profile-scope"]
        first = json.loads(invoke(mode + "-start", start, selected))
        run_id, stamp = first["run_id"], first["resource_profile"]
        assert stamp["mode"] == mode
        assert stamp["cpu"]["threads_allowed"] >= 1 and stamp["memory"]["available_mib"] is not None
        if mode == "deterministic":
            assert (stamp["source"], stamp["cpu"]["threads_allowed"], stamp["memory"]["available_mib"],
                    stamp["gpu"]["devices"]) == ("controlled acceptance launcher", 2, 4096, [])
        else:
            assert stamp["source"] == "host" and stamp["cpu"]["logical_cpus"] == os.cpu_count()
            assert stamp["gpu"]["source"] == "nvidia-smi"
            assert stamp["gpu"]["cuda_visible_devices"] == real.get("CUDA_VISIBLE_DEVICES")
            raw = Path("/sys/fs/cgroup/memory.max")
            if raw.is_file() and (limit := raw.read_text().strip()).isdigit():
                assert stamp["memory"]["cgroup_limit_mib"] == int(limit) // 2**20
        status = [gx, "status", "--state", state, "--run-id", run_id]
        reopen = json.loads(invoke(mode + "-reopen", status, opposite))
        replay = json.loads(invoke(mode + "-replay", start, opposite))
        assert all((row["run_id"], row["resource_profile"]) == (run_id, stamp) for row in (reopen, replay))
        with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as conn:
            event, = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='run.created'",
                                  (run_id,)).fetchall()
            assert json.loads(event[0])["resource_profile"] == stamp
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
            backup = work / (mode + "-snapshot.db")
            with closing(sqlite3.connect(backup)) as copy:
                conn.backup(copy)
        db_name = mode + ".sqlite.gz"
        with backup.open("rb") as source, gzip.open(args.out / db_name, "wb") as archive:
            shutil.copyfileobj(source, archive)
        transactions = [json.loads(line) for line in metrics.read_text().splitlines()]
        assert any(row.get("transaction_owner") == "Run" and row["operation"] == "run_start"
                   and row["run_id"] == run_id and row["commit_or_rollback"] == "commit" for row in transactions)
        evidence.extend([db_name, metrics.name])
        cases[mode] = {"run_id": run_id, "resource_profile": stamp, "creation_events": 1}
    (args.out / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    evidence.append("commands.json")
    return {"status": "PASS", "samples": cases, "evidence": evidence}
