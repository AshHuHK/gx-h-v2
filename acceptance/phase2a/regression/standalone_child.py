"""Observe the legacy combined dropped-start probe and its original accounting rows."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import time


TABLES = ("p0_meter_threads", "p0_meter_responses", "p0_meter_turns", "p0_native_child_meters", "budget_reservations")


def now():
    return datetime.now(timezone.utc).isoformat()


def snapshot(work):
    for db in work.glob("gx-child-access-*/gx.sqlite"):
        try:
            conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=.2)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                return {name: [dict(row) for row in conn.execute("SELECT * FROM " + name + " ORDER BY rowid")]
                        for name in TABLES}
            finally:
                conn.close()
        except sqlite3.Error:
            # A new fixture may still be installing tables, or it may just have closed.
            continue
    return None


def run(stage, out):
    out.mkdir(parents=True, exist_ok=False)
    stage = stage.resolve()
    temp = Path(tempfile.mkdtemp(prefix="gx-double-loss-"))
    (temp / "home/.codex").mkdir(parents=True)
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL")
           or key.lower() in ("all_proxy", "http_proxy", "https_proxy", "no_proxy")}
    env.update(HOME=str(temp / "home"), CODEX_HOME=str(temp / "home/.codex"), TMPDIR=str(temp), PYTHONDONTWRITEBYTECODE="1")
    binary = stage / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    argv = [str(stage / ".venv/bin/python"), "-B", "acceptance/native_child_access.py", "--drop-spawn",
            "--binary", str(binary), "--out", str(out / "child.json")]
    report = {"started_utc": now(), "gate": "supplemental standalone warm spawn loss and cold start loss",
              "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=stage, text=True).strip(),
              "argv": argv, "fixture_timeout_seconds": 25, "outer_timeout_seconds": 90,
              "fixture_dir": str(temp), "meter_snapshots": []}
    deadline = time.monotonic() + report["outer_timeout_seconds"]
    with (out / "child.log").open("w") as log, subprocess.Popen(
            argv, cwd=stage, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True) as proc:
        previous = None
        while proc.poll() is None:
            state = snapshot(temp)
            if state and state != previous:
                report["meter_snapshots"].append({"utc": now(), "rows": state})
                previous = state
            if time.monotonic() > deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                report["outer_timeout"] = True
                break
            time.sleep(.2)
        report["returncode"] = proc.wait()
    text = (out / "child.log").read_text()
    status = "PASS" if report["returncode"] == 0 and (out / "child.json").is_file() else "FAIL"
    if "ChildProbeTimeout: cold timed out" in text and "until(roots, settled)" in text:
        status = "COLD_SETTLEMENT_TIMEOUT"
    report.update(observed_status=status, finished_utc=now())
    (out / "observation.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"revision": report["revision"], "observed_status": status,
                      "child_returncode": report["returncode"], "snapshot_count": len(report["meter_snapshots"])}))
    # The outer command records an observation; the inner returncode and status grade the boundary.
    return 0 if report["meter_snapshots"] and status in ("PASS", "COLD_SETTLEMENT_TIMEOUT") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.stage, args.out.resolve()))
