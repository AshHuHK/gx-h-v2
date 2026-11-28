"""Observe a normal GX study, restart its driver during inference, then release frozen references."""
from contextlib import closing
import csv
import fcntl
from datetime import datetime, timezone
from hashlib import sha1, sha256
from pathlib import Path
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
STOCK = ROOT / "acceptance/science_assets/deployment/start_native.py"


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)


def log(work, action, **facts):
    with (work / "observations.jsonl").open("a") as events:
        events.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(), action=action, **facts)) + "\n")
        events.flush()
        os.fsync(events.fileno())


def ticks(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def deployment_pin(release, candidate):
    # Compare the runnable installation with the selected git object; only these
    # two documented site-specific GPU configuration files are overlaid.
    sha = subprocess.check_output(["git","rev-parse","--verify",candidate + "^{commit}"], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output(["git","ls-tree","-rz",sha,"--","bin","gx-h"], cwd=ROOT)
    checked = 0
    for record in tree.split(b"\0"):
        if not record:
            continue
        head, rawname = record.split(b"\t",1)
        mode, kind, oid = head.split()
        name = os.fsdecode(rawname)
        if name in {"gx-h/config/limits.json","gx-h/config/approvals.json"}:
            continue
        file = release / name
        body = os.fsencode(os.readlink(file)) if mode == b"120000" else file.read_bytes()
        blob = b"blob " + str(len(body)).encode() + b"\0" + body
        if kind != b"blob" or sha1(blob).hexdigest() != oid.decode():
            raise ValueError(f"Deployment differs from the field candidate at {name}.")
        checked += 1
    return {"commit":sha, "verified_source_files":checked, "limits_sha256":digest(release/"gx-h/config/limits.json"),
            "approval_policy_sha256":digest(release/"gx-h/config/approvals.json")}


def owns_driver_lock(state, pid):
    target = state / ".native.lock"
    for fd in Path(f"/proc/{pid}/fd").glob("*"):
        try:
            if fd.samefile(target) and "FLOCK" in Path(f"/proc/{pid}/fdinfo/{fd.name}").read_text():
                return True
        except FileNotFoundError:
            continue
    return False


def stock_driver(base, invocation, work, label):
    with (work / f"{label}.json").open("w") as stdout, (work / f"{label}.log").open("w") as stderr:
        return subprocess.Popen([*base, *invocation], stdout=stdout, stderr=stderr)


def snapshot(state):
    db = state / "gx.db"
    if not db.exists():
        return {}
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            runs = list(conn.execute("SELECT run_id,status FROM v6_goals"))
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}
            raise
        if not runs:
            return {}
        if len(runs) != 1:
            raise ValueError("Use a private Run workspace for the prospective B study.")
        rid, status = tuple(runs[0])
        jobs = [dict(j) for j in conn.execute("""SELECT job_id,status,capability_id,capability_version,
            scheduler_ref,runner_pid,runner_started,staging_ref,measured_json,idempotency_key
            FROM jobs WHERE run_id=? ORDER BY rowid""", (rid,))]
        published = [dict(a) for a in conn.execute("""SELECT a.artifact_ref,a.namespace,a.rel_path,a.job_id,a.sha256
            FROM artifacts a JOIN jobs j USING(job_id,run_id) WHERE a.run_id=? AND a.published=1
            AND j.status='SUCCEEDED' AND j.capability_id='natural-food-fold' AND a.rel_path LIKE '%/model.cif'""", (rid,))]
        decision = conn.execute("""SELECT d.decision_ref FROM v6_decisions d JOIN v6_decision_tasks t
            USING(task_id,run_id) WHERE d.run_id=? AND t.scope='run' ORDER BY d.rowid DESC LIMIT 1""", (rid,)).fetchone()
        assignments = [dict(a) for a in conn.execute("""SELECT event_id,subject_id,payload_json FROM events
            WHERE run_id=? AND kind='v621.material.assigned' ORDER BY event_id""", (rid,))]
        return dict(run_id=rid, status=status, jobs=jobs, published=published, decision=decision and decision[0],
                    assignments=assignments)


def physical(state, job):
    if not job["staging_ref"]:
        return None
    path = state / job["staging_ref"] / "provider-events.jsonl"
    if not path.exists():
        return None
    lines = [json.loads(s) for s in path.read_text().splitlines() if s]
    if len(lines) != 1 or lines[0].get("event") != "accepted":
        return None
    event = lines[0]
    if job["status"] != "RUNNING" or not job["scheduler_ref"] or ticks(event["pid"]) != event["start_ticks"]:
        return None
    # GPU process IDs may use the host PID namespace. Compare NSpid aliases and
    # retain an owned physical-device descriptor when NVML uses a distinct namespace.
    process = Path(f"/proc/{event['pid']}")
    status = process.joinpath("status").read_text().splitlines()
    aliases = {event["pid"]}
    for item in status:
        if item.startswith("NSpid:"):
            aliases.update(int(v) for v in item.split()[1:])
    smi = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory,gpu_uuid",
                          "--format=csv,noheader,nounits"], text=True, capture_output=True, timeout=5)
    rows = []
    for row in csv.reader(smi.stdout.splitlines()):
        if len(row) == 3 and row[0].strip().isdigit() and int(row[0]) in aliases:
            rows.append(dict(pid=int(row[0]), memory_mib=row[1].strip(), uuid=row[2].strip()))
    descriptors = []
    for item in process.joinpath("fd").glob("*"):
        try:
            target = os.readlink(item)
        except FileNotFoundError:
            continue
        if target.startswith("/dev/nvidia") and target[len("/dev/nvidia"):].isdigit():
            descriptors.append(target)
    if not rows and not descriptors:
        return None
    return dict(job_id=job["job_id"], scheduler_ref=job["scheduler_ref"], runner_pid=job["runner_pid"],
                runner_started=job["runner_started"], provider=event, compute_app_rows=rows,
                owned_gpu_descriptors=sorted(set(descriptors)))



def release_materials(base, state, work, vault, snap, digests):
    if not snap["published"] or not snap["decision"] or snap["status"] != "OPEN":
        return
    if len(snap["published"]) != 1:
        raise ValueError("Unexpected additional coordinate output on a single-prediction study.")
    pub = snap["published"][0]
    if digest(state / "artifacts" / pub["namespace"] / pub["rel_path"]) != pub["sha256"]:
        raise ValueError("Published model does not match its receipt.")
    companion = work / "companion.json"
    write(companion, dict(source="operator-position-map:P00711:field-v1", path=str(vault / "mapping.json"),
                          sha256=digests["mapping.json"], role="mapping", media_type="application/json",
                          metadata_path=str(vault / "map-origin.json")))
    cmd = [*base, "assign-material", "--state", str(state), "--run-id", snap["run_id"],
           "--after-decision", snap["decision"], "--source", "wwPDB:1F6S/revision/1.4", "--input", str(vault / "1F6S.cif"),
           "--sha256", digests["1F6S.cif"], "--role", "reference", "--acquisition", str(vault / "acquisition.json"),
           "--media-type", "chemical/x-mmcif", "--companion", str(companion)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    with (work / "assignments.log").open("a") as out:
        out.write(result.stdout + result.stderr)
    log(work, "release_originals", decision=snap["decision"], model=pub, returncode=result.returncode)
    if result.returncode and "STALE_REVISION" not in result.stdout + result.stderr:
        raise RuntimeError("Original-material assignment failed; inspect the preserved normal-entry receipt.")


def observe(opts):
    work, vault = opts.work.resolve(), opts.vault.resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = work / "state"
    pin = deployment_pin(opts.release.resolve(), opts.candidate)
    sys.path.insert(0, str(ROOT))
    from acceptance.v623.oauth.host import _pin
    official_binary, pin["official_codex_sha256"] = _pin(opts.binary)
    base = [sys.executable, str(STOCK), str(opts.release.resolve()), str(official_binary), str(work)]
    frozen = json.loads((HERE / "preregistration.json").read_bytes())
    descriptor_fix = json.loads((HERE / "preflight-addendum.json").read_bytes())
    vault_digests = dict(frozen["custody"]["vault_digests"], **{descriptor_fix["file"]: descriptor_fix["replacement_sha256"]})
    for directory, pins in ((vault, vault_digests), (HERE, frozen["visible"])):
        if any(digest(directory / f) != pin for f, pin in pins.items()):
            raise ValueError("Registered material bytes differ from the frozen field series.")
    condition = work / "field-condition.json"
    if condition.exists():
        if json.loads(condition.read_bytes())["candidate"] != pin["commit"]:
            raise ValueError("Resume the original frozen candidate.")
    else:
        if snapshot(state):
            raise ValueError("Refuse to relabel a previously admitted Run.")
        write(condition, dict(candidate=pin["commit"], deployment=pin, utc=datetime.now(timezone.utc).isoformat(),
                              preregistration=digest(HERE / "preregistration.json"),
                              deadline=time.time() + frozen["engineering"]["deadline_s"]))
    deadline = json.loads(condition.read_bytes())["deadline"]
    restore_proof = work / "driver-recovery.json"
    first = snapshot(state)
    if first.get("status") == "CLOSED":
        return first
    remaining = lambda: str(max(0.001, deadline - time.time()))
    named = ["research", "--state", str(state), "--run-key", "food-mature-P00711",
             "--goal-file", str(HERE / "question.md"), "--goal-config", str(HERE / "goal.json"), "--timeout", remaining()]
    def resume(rid):
        return ["research", "--state", str(state), "--run-id", rid, "--timeout", remaining()]
    if time.time() >= deadline:
        return first
    command = resume(first["run_id"]) if first else named
    driver = stock_driver(base, command, work, "drive-" + str(time.time_ns()))
    log(work, "driver_opened", driver_pid=driver.pid, mode=command[3])
    seen = set()
    while time.time() < deadline:
        current = snapshot(state)
        for job in current.get("jobs", []):
            if (job["job_id"], job["status"]) not in seen:
                seen.add((job["job_id"], job["status"]))
                log(work, "job", job=job)
            if job["capability_id"] != "natural-food-fold" or restore_proof.exists():
                continue
            receipt = physical(state, job)
            if not receipt or driver.poll() is not None:
                continue
            # Only this GX client is killed. The provider process and its Job owner stay alive.
            before = driver.pid
            driver.kill()
            if driver.wait(timeout=15) != -9:
                raise RuntimeError("The observed GX driver did not exit by SIGKILL.")
            if ticks(receipt["provider"]["pid"]) != receipt["provider"]["start_ticks"]:
                raise RuntimeError("The live scientific provider disappeared with the driver.")
            driver = stock_driver(base, resume(current["run_id"]), work, "restored-" + str(time.time_ns()))
            write(restore_proof, dict(run_id=current["run_id"], old_driver=before, new_driver=driver.pid,
                                      at_kill=receipt, after_reopen_ticks=ticks(receipt["provider"]["pid"])))
            log(work, "reopened_same_run", old_driver=before, driver=driver.pid, job_id=job["job_id"])
        post = work / "running-after-restore.json"
        if restore_proof.exists() and not post.exists() and owns_driver_lock(state, driver.pid):
            before = json.loads(restore_proof.read_bytes())
            prior = before["at_kill"]
            job = next((j for j in current.get("jobs", []) if j["job_id"] == prior["job_id"]), None)
            if job:
                after = physical(state, job)
                if after and (after["scheduler_ref"],after["provider"]) == (prior["scheduler_ref"],prior["provider"]):
                    write(post, dict(run_id=current["run_id"], actual_driver=driver.pid, flock_observed=True,
                                     current_job=after, utc=datetime.now(timezone.utc).isoformat()))
        if current.get("published") and not current["assignments"]:
            release_materials(base, state, work, vault, current, vault_digests)
        if current.get("status") == "CLOSED" or driver.poll() is not None:
            break
        time.sleep(1)
    if driver.poll() is None:
        try:
            driver.wait(timeout=45)
        except subprocess.TimeoutExpired:
            driver.terminate()
            driver.wait(timeout=15)
    final = snapshot(state)
    log(work, "operator_observation_ended", status=final.get("status"), driver_exit=driver.returncode)
    return final


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("release", "binary", "work", "vault"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--candidate", required=True, help="Frozen integrated product commit used for this deployment")
    args = p.parse_args()
    args.work.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (args.work / ".field-observer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(observe(args), ensure_ascii=False, indent=2))
