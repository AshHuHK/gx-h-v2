"""Freeze the two separate public series, original four slots, science inputs, authority and judges."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from acceptance.v62c import rehearse,materials
from acceptance.v622.public.enrollment import read,digest,save,pin_files
from acceptance.v623.live import hosts

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
P = read(HERE/"protocol.json")


def sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def grant(architecture):
    g = rehearse.grant(architecture)
    g.update(evidence_policy=P["evidence_policy"],research_modules=[] if architecture=="single" else [P["module"]])
    for item in g["input_files"]:
        if item["role"]=="workflow_spec":
            item.update(path=str(HERE/"comparison-card.json"),sha256=digest(HERE/"comparison-card.json"))
    return g


def declared(host,roster):
    pins = pin_files()
    for file in sorted((ROOT/"gx-h/gx_core").rglob("*.sql")):
        pins[file.relative_to(ROOT).as_posix()] = digest(file)
    for directory in [HERE, ROOT/"gx-h/plugins/installed/org.gx.data-inventory/3.0.0"]:
        for file in sorted(directory.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                pins[file.relative_to(ROOT).as_posix()] = digest(file)
    frozen = {"source_pins":pins,"protocol":P,"host":host,"reviewer_roster_sha256":sha(roster) if roster else None}
    return sha(frozen), pins


def inspect(host,binary,roster=None,approval=None,authority=None):
    from acceptance.v623.live import deployment,adjudication
    if roster:
        adjudication.validate_roster(roster,mode=host)
    protocol_sha,_ = declared(host,roster)
    candidate = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    receipt = {"candidate":candidate,"protocol_sha256":protocol_sha,"host":host,
               "series":P["series"] if host=="live" else P["rehearsal_series"],
               "reviewer_roster_sha256":sha(roster) if roster else None,
               "official_host":hosts.inspect_binary(binary)}
    if host=="offline":
        return {**receipt,"status":"READY_OFFLINE","reason_codes":[],"paid_requests":0}
    funding = deployment.preflight(series=P["series"],candidate=candidate,protocol_sha256=protocol_sha,
                    binary=binary,model=P["model"],effort=P["model_effort"],approval=approval,authority=authority,
                    probe_host=True)
    blockers=list(funding["reason_codes"])
    if not roster:
        blockers.append("INDEPENDENT_REVIEWER_NOT_ENROLLED")
    return {**receipt,"funding":funding,"status":"BLOCKED" if blockers else "READY","reason_codes":list(dict.fromkeys(blockers))}


@contextmanager
def locked(work):
    with (work/".batch.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield lock.fileno()


def clean_candidate(candidate):
    head = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    if head!=candidate or subprocess.check_output(["git","status","--porcelain"],cwd=ROOT):
        raise ValueError("Fee-bearing work requires its registered clean candidate")


def register(work,host,binary,roster=None,approval=None,authority=None):
    from acceptance.v623.live import adjudication
    pre = inspect(host,binary,roster,approval,authority)
    if pre["reason_codes"]:
        return pre
    if host=="live":
        clean_candidate(pre["candidate"])
    if work.is_relative_to(ROOT) or work.exists():
        raise ValueError("Register into a new directory outside the release")
    work.mkdir(mode=0o700,parents=True)
    with locked(work):
        with TemporaryDirectory(dir=work) as stage:
            materials.prepare(Path(stage)/"custody")
            (Path(stage)/"custody").rename(work/"custody")
        for code in ("P4","K7"):
            src=work/"custody"/code
            if (digest(src/"specimen.cif")!=P["materials"][code]["sha256"] or
                digest(src/"transform.json")!=P["materials"][code]["transform_sha256"]):
                raise ValueError("Prepared specimen or committed transformation differs from registration")
        _,pins=declared(host,roster)
        save(work/"source-pins.json",pins)
        reg={**pre,"slots":P["slots"],"registered_slots":4,"model":P["model"],"effort":P["model_effort"],"wire_model":P["model"],
             "wire_effort":P["model_effort"],"research_effort":P["research_effort"],"assay":P["assay"],"threshold":P["threshold"],
             "source_commit":pre["candidate"],"scope":"OFFICIAL_CODEX_LOOPBACK" if host=="offline" else "CURRENT_AUTHORIZED_PROVIDER",
             "provider":"gx_loopback" if host=="offline" else "gx_admitted_responses",
             "evidence_policy":P["evidence_policy"],"slot_cap":P["token_cap_per_slot"],"batch_cap":P["token_cap_total"],
             "grants":{a:grant(a) for a in ("single","delegation_available")},"oracle_sha256":digest(work/"custody/oracle.json")}
        if host=="live":
            from acceptance.v623.live import deployment
            owner=deployment.open_deployment()
            reg.update(deployment_id=owner.deployment_id,contract_id=owner.contract_id)
            save(work/"approved-roster.json",roster)
        save(work/"registration.json",reg)
        if host=="offline":
            adjudication.prepare_offline(work)
        else:
            adjudication.prepare_live(work,roster)
        save(work/"result.json",{"series":reg["series"],"host":host,"cases":{i["slot"]:{"status":"REGISTERED"} for i in P["slots"]}})
        return reg
