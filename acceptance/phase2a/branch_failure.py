"""Capture P2A-08's isolated branch failure, committed effects and explicit retry."""
import argparse
from collections import Counter
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.persistence import store
from tests.test_phase2a_engine import StaticBranches


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    directory = parser.parse_args().output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    git = lambda *args: subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()
    commit, dirty = git("rev-parse", "HEAD"), git("status", "--porcelain")
    before = Counter()
    phases = {}
    spawns = []
    def audit(event, args):
        if event in {"subprocess.Popen", "os.exec", "os.posix_spawn", "os.system"}:
            spawns.append({"event": event, "executable": str(args[0])})
    sys.addaudithook(audit)

    class Capture(StaticBranches):
        def snapshot(self, label):
            latest = store.latest_checkpoint(self.db, self.run)
            state = self.engine.state(self.run)
            events = [dict(row) for row in self.db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY event_id", (self.run,))]
            for event in events:
                event["payload"] = json.loads(event.pop("payload_json"))
            completed = Counter(e["payload"]["node"] for e in events if e["kind"] == "workflow.node_completed")
            state.update(checkpoint=store.loads(latest, "state_json", None), completion_counts=dict(completed),
                         events=len(events), last_event_id=events[-1]["event_id"])
            phases[label] = state
            with tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / "state.sqlite"
                with closing(sqlite3.connect(source)) as db:
                    self.db.backup(db)
                with source.open("rb") as data, gzip.open(directory / f"{label}.sqlite.gz", "wb") as zipped:
                    shutil.copyfileobj(data, zipped)
            if label == "after_failure":
                before.update(completed)
            if label == "after_retry":
                write(directory / "events.json", events)

        def start(self):
            self.snapshot("before")
            reply = super().start()
            self.snapshot("after_failure")
            return reply

        def tearDown(self):
            self.snapshot("after_retry")

    name = "test_failed_branch_rolls_back_and_manual_retry_reuses_other_branches"
    with (directory / "unittest.log").open("w") as log:
        outcome = unittest.TextTestRunner(stream=log, verbosity=2).run(unittest.TestSuite([Capture(name)]))
    passed = outcome.wasSuccessful()
    after = phases["after_retry"]
    events = json.loads((directory / "events.json").read_text())
    duplicates = sum(max(count - 1, 0) for count in after["completion_counts"].values())
    failure_effects = sum(event["kind"] == "fixture.uncommitted" for event in events)
    codex = [item for item in spawns if "codex" in Path(item["executable"]).name or
             "app-server" in Path(item["executable"]).name]
    passed = (passed and before == {"C": 1} and after["completion_counts"] == {"C": 1, "bad": 1, "bad_child": 1}
              and failure_effects == 0 and not duplicates and not codex)
    files = ["before.sqlite.gz", "after_failure.sqlite.gz", "after_retry.sqlite.gz", "events.json", "unittest.log"]
    write(directory / "evidence.json", {
        "acceptance": "P2A-08", "evidence_class": "SCRIPTED", "test": name, "commit": commit,
        "dirty_state": dirty, "cwd": str(Path.cwd()), "command": sys.orig_argv, "exit_code": 0 if passed else 1,
        "run_id": after["run_id"], "job_ids": [], "branches": {"bad": ["bad", "bad_child"], "A": ["A"], "C": ["C"]},
        "subprocess_launches": spawns, "codex_spawns": len(codex), "duplicate_completions": duplicates,
        "durable_failed_node_effects": failure_effects, "phases": phases, "evidence_path": str(directory),
        "files_sha256": {file: hashlib.sha256((directory / file).read_bytes()).hexdigest() for file in files},
    })
    print(f"{'PASS' if passed else 'FAIL'} P2A-08 {after['run_id']} {directory}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
