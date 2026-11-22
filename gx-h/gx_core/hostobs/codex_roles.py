"""Generate the pinned Codex-native role configurations for GX Harness.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping
from .. import contracts
from ..errors import SchemaInvalid
from . import host_policy, policy as role_policy, roles

VERSION = "shipped-gx-native-v1"
NATIVE_FRAME = Path(__file__).with_name("codex_native_frame.md")
PIN_NAMESPACE = "role_intents_by_host"


def projector_sha256() -> dict[str,str]:
    own=Path(__file__).resolve()
    return {**roles.projector_sha256(),str(own.relative_to(contracts.HARNESS_ROOT)):
            hashlib.sha256(own.read_bytes()).hexdigest(),
            str(NATIVE_FRAME.relative_to(contracts.HARNESS_ROOT)):
            hashlib.sha256(NATIVE_FRAME.read_bytes()).hexdigest()}


def _intent(role: str, policy: Mapping[str,Any]) -> dict[str,Any]:
    content=roles.shipped_intent(role,policy)
    content["manifest"].update(version=VERSION,hosts=["codex"])
    return content


def shipped(policy: Mapping[str,Any] | None = None) -> dict[str,dict[str,Any]]:
    policy=policy if policy is not None else host_policy.for_host("codex")[0]
    if policy.get("policy_host") != "codex" or policy.get("policy_scope") != host_policy.SCOPE:
        raise SchemaInvalid("Codex role generation requires the independently scoped Codex policy")
    out={}
    for role in policy["roles"]:
        content=_intent(role,policy)
        problems=roles.problems_in(content,policy)
        if problems:
            raise SchemaInvalid("invalid Codex shipped intent",details={"problems":problems})
        required=roles.requirement_of(content,policy)
        body={"host":"codex","intent":content,"requirement":required,
              "projector":projector_sha256()}
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),
                         ensure_ascii=False).encode()).hexdigest()
        out[roles.intent_id(role)]={"sha256":digest,"intent":content,"requirement":required}
    return out


def pins() -> dict[str,str]:
    try:
        value=json.loads(roles.SHIPPED_PINS.read_text())[PIN_NAMESPACE]["codex"]
    except (OSError,ValueError,KeyError,TypeError):
        return {}
    return {str(k):str(v) for k,v in value.items()} if isinstance(value,dict) else {}


def pin_problems(policy: Mapping[str,Any] | None = None) -> list[str]:
    expected={k:v["sha256"] for k,v in shipped(policy).items()}
    admitted=pins()
    return [f"CODEX PIN MISMATCH {key}: computed={expected.get(key)} pinned={admitted.get(key)}"
            for key in sorted(set(expected)|set(admitted)) if expected.get(key)!=admitted.get(key)]


def body(content: Mapping[str,Any], policy: Mapping[str,Any], gx: str) -> str:
    manifest=content["manifest"]
    required=roles.requirement_of(content,policy)
    coordinator = manifest["role"] == roles.COORDINATOR
    mode_instructions = (
        "Select a READY row from the fresh dispatch plan already in hand. If the plan "
        "is missing or stale, read gx_next_actions once. Pass the row's expected_attempt "
        "and that plan's as_of_event_id to the worker for its claim. Use gx_run_status "
        "for overall state, holds, budgets, terminal outcomes or diagnosis. GX owns tasks, "
        "teams, dependencies, leases and budgets. Codex owns native workers and messages. "
        "Dispatch independent READY work with native tools at the task's required "
        "model/effort. Supply task/brief references rather than copying whole histories. "
        "Use a different non-author native worker for review. Follow up existing workers "
        "for repairs where appropriate; resume the same workflow after actual results. "
        "Do not author, self-review or announce acceptance in place of workers and gates."
        if coordinator else
        "When gx_task_acquire is present, call it with task_id, read the complete returned "
        "brief and deliver the prepared manifest with gx_task_finish. A single task-scoped "
        "published artifact is also a valid manifest_ref. For the legacy catalog only, "
        "use the assigned READY row's expected_attempt and its plan's "
        "as_of_event_id. Keep the returned lease and attempt, bind through gx_task_bind, "
        "and read the complete frozen brief. Continue bound work on its live lease. "
        "Publish the required artifacts with gx_artifact_put and submit their exact "
        "references directly with gx_task_submit. Use gx_bundle_check for an unfamiliar "
        "bundle format, a prior rejection, an expensive retry, or a precheck required "
        "by the frozen brief. Return submission references and any itemized failures. "
        "Reviewers inspect requirements, failed and revised artifacts, "
        "run relevant tests or reproduce results, and submit a reasoned verdict. A missing "
        "mandatory check cannot be waived by a positive review."
    )
    frame=NATIVE_FRAME.read_text(encoding="utf-8")
    text=frame.format(role=manifest["role"],does=manifest["description"],
        model_words=f"`{required['model']}`",
        allowed_efforts=" or ".join(f"`{v}`" for v in required["allowed_efforts"]),
        mode_instructions=mode_instructions).strip("\n")
    instructions=content["instructions"].strip("\n")
    return text+("\n\n"+instructions if instructions else "")+"\n"


def render(content: Mapping[str,Any], digest: str, policy: Mapping[str,Any], *, gx: str) -> str:
    manifest=content["manifest"]; required=roles.requirement_of(content,policy)
    return ("# Independently pinned Codex projection from gx_core/hostobs/codex_roles.py.\n"
            "# Host-applied configuration and callback evidence are distinct; no upstream tier attestation.\n"
            f"name = {json.dumps(roles.role_name('codex',manifest['role']))}\n"
            f"description = {json.dumps(roles._description(manifest),ensure_ascii=False)}\n"
            f"model = {json.dumps(required['model'])}\n"
            f"model_reasoning_effort = {json.dumps(required['default_effort'])}\n"
            "developer_instructions = '''\n"+body(content,policy,gx)+"\n---\n"+
            "".join(f"{k}: {v}\n" for k,v in roles.stamp(manifest["role_id"],VERSION,digest).items())+"'''\n")


def shipped_files(policy: Mapping[str,Any] | None = None, *, gx: str = roles.GX_BIN) -> list[dict[str,Any]]:
    policy=policy if policy is not None else host_policy.for_host("codex")[0]
    return [{"host":"codex","path":roles.role_path("codex",entry["intent"]["manifest"]["role"]),
             "role_id":name,"content":render(entry["intent"],entry["sha256"],policy,gx=gx).encode()}
            for name,entry in shipped(policy).items()]
