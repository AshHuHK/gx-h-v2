"""Create a V6.2.3 official-loopback study, upgrade its deployment in place, then restore its backup."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
OLD = "f9231023b5c44c5d96ec1a974d639ea9dfe34613"


def receipts(state):
    from gx_core.domain import ids
    with closing(sqlite3.connect((state / "gx.db").as_uri()+"?mode=ro", uri=True)) as conn:
        return ids.sha256_of_obj([list(conn.execute("SELECT request_json,receipt_json FROM v6_decisions ORDER BY rowid")),
                                  list(conn.execute("SELECT request_json FROM p0_v6_native_calls ORDER BY rowid"))])


def redeliver(install, project):
    sys.path[:0] = [str(install), str(install / "gx-h")]
    from acceptance.v622.test_close import CloseStudy
    from gx_core.p0 import v621_policy
    with CloseStudy(project, reopen=True) as restored:
        assert v621_policy.pinned(restored.conn, restored.run_id) == v621_policy.V623
        row = restored.conn.execute("""SELECT c.request_json,d.receipt_json FROM p0_v6_native_calls c
                JOIN v6_decisions d USING(thread_id,turn_id,call_id) ORDER BY d.rowid DESC LIMIT 1""").fetchone()
        native, saved = map(json.loads, row)
        assert restored.roots._call(native) == saved


def replay(baseline, work):
    if work.is_relative_to(ROOT) or work.is_relative_to(baseline):
        raise ValueError("Select a fresh external workspace")
    sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
    from acceptance.v62a.upgrade import probe
    from gx_core.domain import ids
    assert probe.source_commit(baseline) == OLD
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    install, project = work / "install", work / "project"
    install.mkdir()
    # The launch paths in a native release marker must survive the deployment swap.
    ignore = shutil.ignore_patterns("var", "__pycache__", "*.pyc")
    for name in ("acceptance", "gx-h", "bin", "examples"):
        shutil.copytree(baseline / name, install / name, ignore=ignore)
    for name in ("vendor", "release-assets", ".git"):
        (install / name).symlink_to(baseline / name)
    subprocess.run([sys.executable, "-B", str(install / "acceptance/v623/endpoint_host.py"),
                    "--work", str(project)], check=True, cwd=install)
    state = project / "state"
    before, original = probe.snapshot(state), receipts(state)
    assert before["schema"]["contract_sha256"] == "cdb1d8e5704193422e9ec1f341b064d02ee71d440aea33dc5f840e3c0700f56f"
    assert before["jobs"] and before["receipts"] and before["turns"]
    probe.backup(state, work / "backup")
    rid = before["run"]["run_id"]
    old_harness = work / "old-harness"
    (install / "gx-h").rename(old_harness)
    shutil.copytree(ROOT / "gx-h", install / "gx-h", ignore=ignore)
    for _ in range(2):
        resumed = probe.cli(install, work, state, "resume", "--run-id", rid, "--timeout", 5)
        assert resumed["goal_status"] == "CLOSED"
    subprocess.run([sys.executable, "-B", __file__, "_redeliver", str(install), str(project)],
                   cwd=install, check=True)
    after = probe.snapshot(state)
    business = {k:v for k,v in before.items() if k not in ("schema", "resync")}
    assert all(after[k] == v for k,v in business.items()) and receipts(state) == original
    assert len(after["resync"]) == len(before["resync"]) + 1
    state.rename(work / "migrated-state")
    probe.backup(work / "backup", state)
    (install / "gx-h").rename(work / "candidate-harness")
    old_harness.rename(install / "gx-h")
    resumed = probe.cli(install, work, state, "resume", "--run-id", rid, "--timeout", 5)
    assert resumed["goal_status"] == "CLOSED" and probe.snapshot(state) == before and receipts(state) == original
    out = dict(baseline=OLD, source="new V6.2.3 official-loopback D1/D2 study", original_policy="gx.evidence/6.2.3",
               original_run=rid, business_sha256=ids.sha256_of_obj(business), raw_requests_and_receipts_sha256=original,
               before=before["schema"], after=after["schema"], decisions=len(before["receipts"]), jobs=len(before["jobs"]),
               turns=len(before["turns"]), cold_reopens=2, archived_call_replays=1, restored_backup_reopens=1,
               additional_jobs=0, additional_turns=0, paid_calls=0)
    target = work / "migration.json"
    target.write_text(json.dumps(out, indent=2)+"\n")
    print(target)


if __name__ == "__main__":
    if sys.argv[1:2] == ["_redeliver"]:
        redeliver(*(Path(p) for p in sys.argv[2:]))
    else:
        args = argparse.ArgumentParser(description=__doc__)
        for name in ("baseline","work"):
            args.add_argument("--" + name, type=Path, required=True)
        config = args.parse_args()
        replay(config.baseline.resolve(), config.work.resolve())
