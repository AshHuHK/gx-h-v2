"""The sections every brief has, whatever the task: what, what back, how judged, when to stop.

Published through ``gx_core.registry.brief_section`` like any other stream's sections, and
laid out by their ``order``:

      0  task               what the task is and what it was given
      5  previous_attempts  what was handed in before and why it was refused (attempt 2 on)
   10-79 (other streams)    evidence, gaps, prior searches, skills, prior failures,
                            capabilities, the evaluation specification ...
     80  deliverable        the bundle to return: its schema, from the frozen contract
     85  acceptance         the rule this task is judged by and what the backend checks
     90  policy             the model and effort this work requires, and how that is shown
     95  stop_conditions    when to stop, and what to do instead of working around something
     99  handing_in         the commands, with this task's own identifiers in them

Each section reads what the backend holds - the task row, the run row, the contract, the
agent policy - and nothing a caller sent. None of them holds advice that depends on the
science of a task; that belongs to the sections the services publish.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .. import contracts
from ..context import Ctx
from ..hostobs import policy as agent_policy
from ..registry import BriefSection, brief_section

GX_BIN = str(contracts.HARNESS_ROOT / "bin" / "gx")

_TYPE_NAME = re.compile(r"[A-Z][A-Za-z0-9]*")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)


def _run(ctx: Ctx, run_id: str) -> dict[str, Any]:
    row = ctx.conn.execute(
        "SELECT objective, status, host_json, inputs_json, agent_policy_sha256, project_ref FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return {}
    data = {key: row[key] for key in row.keys()}
    try:
        data["host"] = json.loads(row["host_json"] or "{}")
    except ValueError:
        data["host"] = {}
    return data


# -------------------------------------------------------------------------------- task

@brief_section("task", order=0)
def task_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection:
    run = _run(ctx, task["run_id"])
    lines = [
        f"One `{task['kind']}` task of run `{task['run_id']}`, for a worker in the role "
        f"`{task['role']}`.",
        "",
        "**The objective of the run**",
        "",
        str(run.get("objective") or "(the run records no objective)").strip(),
    ]
    given = task.get("input") or {}
    lines += ["", "**What this task was given**", ""]
    if given:
        lines += ["```json", _json(given), "```"]
    else:
        lines.append("Nothing beyond the sections of this brief.")
    refs = [task["task_id"], task["run_id"]]
    if task.get("path_id"):
        lines += ["", f"It belongs to path `{task['path_id']}`."]
        refs.append(task["path_id"])
    if task.get("subject_kind"):
        lines += [
            "",
            f"**What it is about**: the {task['subject_kind']} `{task.get('subject_id')}` at sha256 "
            f"`{task.get('subject_sha256')}`. What you hand in is about exactly those bytes; if the "
            "content you are shown hashes to something else, say so and stop.",
        ]
    if task.get("independent_of_task_id"):
        lines += [
            "",
            f"**Independence**: this task must be done by a worker that did not do task "
            f"`{task['independent_of_task_id']}`. The backend compares the principal, the lease and "
            "the worker the host recorded; if you did that task, do not claim this one.",
        ]
        refs.append(task["independent_of_task_id"])
    dependencies = list(task.get("dependencies") or [])
    if dependencies:
        lines += ["", "It became READY when these tasks were accepted: "
                  + ", ".join(f"`{dep}`" for dep in dependencies) + "."]
    return BriefSection(title="The task", body="\n".join(lines), refs=refs)


# ------------------------------------------------------------------- previous attempts

@brief_section("previous_attempts", order=5)
def previous_attempts_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    history = list(task.get("previous_attempts") or [])
    if not history:
        return None
    attempt = int(task.get("attempt") or 0) + 1
    if task.get("bundle_kind"):
        instructions = (f"This is attempt {attempt}. The earlier attempts ended as "
                        "follows. What was refused will be refused again unless it changes: correct the record, "
                        "or leave it out and say why in the fields the bundle has for that.")
        whole = "the bundle as a whole"
    else:
        instructions = f"This is attempt {attempt}. Review the reasons below and correct the affected product."
        whole = "the submitted product"
    lines = [instructions]
    refs: list[str] = []
    for item in history:
        outcome = str(item.get("outcome") or "?")
        lines += ["", f"### Attempt {item.get('attempt')}: {outcome}"]
        if outcome == "LAPSED":
            lines.append(
                "The lease ran out before anything was handed in"
                + (f" (the task was {item['status_at_lapse']})" if item.get("status_at_lapse") else "")
                + ". Claim with a lease long enough for the work."
            )
            if item.get("summary"):
                lines.append(f"Recorded reason: {item['summary']}")
            continue
        counts = []
        if item.get("accepted_count") is not None:
            counts.append(f"{item['accepted_count']} record(s) accepted")
        if item.get("rejected_count") is not None:
            counts.append(f"{item['rejected_count']} refused")
        if counts:
            lines.append(", ".join(counts) + ".")
        if item.get("policy_verdict"):
            lines.append(f"The worker stood against the agent policy as `{item['policy_verdict']}`.")
        if item.get("summary"):
            lines.append(f"The backend's summary: {item['summary']}")
        refused = list(item.get("refused") or [])
        for record in refused:
            index = record.get("index")
            where = whole if index in (None, -1) else f"record {index}"
            lines.append(f"- {where}: `{record.get('code')}` - {record.get('message')}")
        total = int(item.get("refused_total") or len(refused))
        if total > len(refused):
            lines.append(f"- and {total - len(refused)} more refusal(s) not listed here.")
        if item.get("records_ref"):
            lines.append(f"Every record's verdict of that attempt: artifact `{item['records_ref']}`.")
            refs.append(item["records_ref"])
        if item.get("submission_id"):
            refs.append(item["submission_id"])
    return BriefSection(title="What was handed in before, and why it was refused",
                        body="\n".join(lines), refs=refs)


# --------------------------------------------------------------------------- deliverable

def named_types_in(type_text: str) -> list[str]:
    """The named types a field's type mentions: ``array<Locator>?`` mentions ``Locator``."""
    types = contracts.bundle_types()
    return [name for name in _TYPE_NAME.findall(type_text) if name in types]


def reachable_types(fields: Mapping[str, str]) -> list[str]:
    """Every named type reachable from a bundle's fields, in the order first met."""
    types = contracts.bundle_types()
    order: list[str] = []
    queue = [name for text in fields.values() for name in named_types_in(text)]
    while queue:
        name = queue.pop(0)
        if name in order:
            continue
        order.append(name)
        queue.extend(found for text in types[name].values() for found in named_types_in(text))
    return order


def enums_in(all_fields: list[Mapping[str, str]]) -> list[str]:
    axes: list[str] = []
    for fields in all_fields:
        for text in fields.values():
            for axis in re.findall(r"enum:([a-z_]+)", text):
                if axis not in axes:
                    axes.append(axis)
    return axes


def _field_lines(fields: Mapping[str, str]) -> list[str]:
    lines = []
    for name, text in fields.items():
        optional = text.endswith("?")
        lines.append(f"- `{name}`: `{text.rstrip('?')}`" + (" (optional)" if optional else ""))
    return lines


@brief_section("deliverable", order=80)
def deliverable_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    kind = task.get("bundle_kind")
    if not kind:
        required = task.get("required_artifacts") or []
        if required:
            return BriefSection(title="What to hand back", body="Publish each required kind under "
                                "this task ID: " + ", ".join(f"`{name}`" for name in required) + ".")
        return None
    spec = contracts.bundle_spec(kind)
    document = contracts.bundles()
    types = contracts.bundle_types()
    reachable = reachable_types(spec["fields"])
    lines = [
        f"One JSON object, a `{kind}`. Its fields, from the frozen contract "
        "(`contracts/bundles.json`); a field that is not listed is refused, not ignored:",
        "",
        *_field_lines(spec["fields"]),
    ]
    for name in reachable:
        lines += ["", f"`{name}`:", *_field_lines(types[name])]
    axes = enums_in([spec["fields"], *[types[name] for name in reachable]])
    if axes:
        lines += ["", "Values of the enumerations:"]
        for axis in axes:
            lines.append(f"- `enum:{axis}`: " + ", ".join(f"`{value}`" for value in contracts.label_values(axis)))
    lines += ["", f"Notation: {document['notation']}"]
    rules = document.get("rules", {})
    if rules:
        lines += ["", "What holds for every bundle:"]
        lines += [f"- {text}" for text in rules.values()]
    limits = document.get("limits", {})
    if limits:
        lines += ["", f"At most {limits.get('max_records')} records. {limits.get('note', '')}".rstrip()]
    return BriefSection(title=f"What to hand back: a {kind}", body="\n".join(lines),
                        refs=[f"contracts/bundles.json#{kind}"])


# ---------------------------------------------------------------------------- acceptance

@brief_section("acceptance", order=85)
def acceptance_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    kind = task.get("bundle_kind")
    rule = task.get("acceptance") or {}
    if not kind and not rule:
        return None
    lines: list[str] = []
    if rule:
        lines += ["The rule this task carries:", "", "```json", _json(rule), "```", ""]
    if not kind and rule.get("validate_workflow_product") is True:
        lines.append("The workflow judges the submitted artifacts against this assignment.")
        return BriefSection(title="How it is judged", body="\n".join(lines))
    if kind:
        spec = contracts.bundle_spec(kind)
        lines.append(f"A `{kind}` is accepted on this basis: {spec.get('acceptance', '')}".rstrip())
        if spec.get("per_record"):
            lines += ["", f"How each record is answered: {spec['per_record']}"]
        if spec.get("backend_checks"):
            lines += ["", "What the backend checks, against bytes and rows it holds:"]
            lines += [f"- {check}" for check in spec["backend_checks"]]
    lines += [
        "",
        "The backend judges record by record and keeps the records that pass. It never declares "
        "scientific success, and neither does acceptance of this task: an accepted bundle is a "
        "bundle whose records could be checked.",
    ]
    return BriefSection(title="How it is judged", body="\n".join(lines),
                        refs=[f"contracts/bundles.json#{kind}"] if kind else [])


# -------------------------------------------------------------------------------- policy

_OBSERVATION = {
    "claude_code": (
        "This run is driven from Claude Code, where the host keeps a record of every turn. Your "
        "submission is accepted when that record shows every turn you took on this task on the "
        "required model at an allowed effort. A worker the record cannot show - one that did not "
        "bind, or named no session or agent - has its submission held for the user."
    ),
    "codex": (
        "This run is driven from Codex. The backend has no reader for a Codex session's record, "
        "so what you report when you bind is taken as a report and recorded as one. A report "
        "that names another model or a lower effort holds the submission for the user."
    ),
    "other_verified": (
        "This run is driven by a script or a person; there is no model to observe. What you "
        "report when you bind is recorded with the submission."
    ),
}


@brief_section("policy", order=90)
def policy_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection:
    from ..hostobs.observation import policy_of_run
    policy_doc, _sha = policy_of_run(ctx.conn, task["run_id"])
    requirement = agent_policy.requirement_of_task(task, policy_doc)
    run = _run(ctx, task["run_id"])
    host = (run.get("host") or {}).get("kind")
    efforts = " or ".join(f"`{level}`" for level in requirement["allowed_efforts"])
    scoped_codex = policy_doc.get("policy_host") == "codex"
    observation_words = (
        "This Codex run uses a backend-verified native Host binding. The backend reads your own "
        "Host-applied per-turn model and effort over this lease. A matching report cannot "
        "replace missing or contradictory native observations; only CONFORMS permits submission."
        if scoped_codex else _OBSERVATION.get(host, _OBSERVATION["other_verified"]))
    lines = [
        f"This task is {str(requirement.get('task_class') or 'unclassified').lower()} work. It is "
        f"done on `{requirement['model']}` at effort {efforts}. That is the user's rule, pinned on "
        "the run when it started; it is not a preference a worker may trade against speed.",
        "",
        observation_words,
        "",
        "If a safeguard refuses one of your requests, or you find yourself on another model: stop. "
        "Do not rephrase the request, do not retry it, do not continue on the other model and do "
        "not switch on your own. Report to whoever started you when it happened, from which model "
        "to which, the category and the request id. The task is held and the user decides: the "
        "brief may be reworded, the task skipped, or another model authorised for this one task.",
    ]
    return BriefSection(title="Which model does this work", body="\n".join(lines),
                        refs=["config/role_policy.codex.json" if scoped_codex else "config/role_policy.json"])


# ----------------------------------------------------------------------- stop conditions

@brief_section("stop_conditions", order=95)
def stop_conditions_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection:
    attempt = int(task.get("attempt") or 0) + 1
    limit = int(task.get("max_attempts") or 0)
    bundled = bool(task.get("bundle_kind"))
    lines = [
        ("**Hand in** when the rule above is met - or when you have established that it cannot be "
        "met from what exists. In that case hand in what you have, with the gaps and negative "
        "results in the fields the bundle has for them. A bundle that reports less, honestly, is "
        "judged record by record and its good records are kept; a result made up to fill a "
        "field is refused and costs the attempt." if bundled else
        "**Hand in** the required artifacts after completing the assignment. Report any limitations in the product."),
        "",
        "**Stop and report, without handing in**, when:",
        "- the backend answers `IMPLEMENTATION_GAP`, `CAPABILITY_NOT_APPROVED`, `APPROVAL_REQUIRED`, "
        "`HOLD_ACTIVE` or `MODEL_POLICY_VIOLATION`. Each is an answer about what exists or what "
        "is allowed. None is worked around, and none is replaced by something you produce yourself;",
        "- a safeguard refuses a request or your model changes (the section above);",
        "- `LEASE_EXPIRED`: your claim ran out and the task may be someone else's. Do not submit "
        "under the old lease;",
        "- the brief contradicts itself or the bytes you are shown do not match a sha256 it names.",
        "",
    ]
    outcome = ("A refused bundle ends the attempt. The next brief lists the reasons for refusal. "
               "See **How to hand it in** for this task's `gx_bundle_check` instructions." if bundled else
               "A refused submission ends the attempt. The next attempt's brief lists the reasons for refusal.")
    lines.append(f"This is attempt {attempt}" + (f" of {limit}" if limit else "") + ". " + outcome)
    return BriefSection(title="When to stop", body="\n".join(lines))


#: How every command of a brief names the worker it runs as. After the subcommand, where the
#: client reads it today; in front of it the option is dropped without a word.
SUBJECT_OPTION = "--as <your subject>"

# ---------------------------------------------------------------------------- handing in

def _codex_root_session(run: Mapping[str, Any], policy_doc: Mapping[str, Any]) -> str | None:
    """A display value from the backend-pinned run, never a caller's claimed identity.

    The section already read ``policy_of_run``. Reuse its scope validator here rather
    than treating a declared host or a provenance-shaped object as verification.
    """
    from ..hostobs import codex, host_policy

    if policy_doc.get("policy_host") != "codex":
        return None
    if host_policy.scope_of_run({"agent_policy_json": policy_doc,
                                 "agent_policy_sha256": run.get("agent_policy_sha256"),
                                 "host_json": run.get("host_json")}) != "codex":
        return None
    host = run.get("host") or {}
    selected = host.get("policy_selection") or {}
    session = selected.get("session_ref")
    identity_sha = selected.get("identity_sha256")
    if (host.get("kind_basis") != "verified_native_invocation"
            or selected.get("host") != "codex"
            or selected.get("native_agent_ref") != session
            or not codex.valid_thread_ref(session)
            or not isinstance(identity_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", identity_sha) is None):
        return None
    return session


@brief_section("handing_in", order=99)
def handing_in_section(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection:
    run = _run(ctx, task["run_id"])
    host = (run.get("host") or {}).get("kind") or "other_verified"
    attempt = int(task.get("attempt") or 0) + 1
    task_id, run_id, role = task["task_id"], task["run_id"], task["role"]
    kind = task.get("bundle_kind")
    product_kinds = [kind] if kind else task.get("required_artifacts") or []
    from ..hostobs.observation import policy_of_run
    policy_doc, _sha = policy_of_run(ctx.conn, task["run_id"])
    requirement = agent_policy.requirement_of_task(task, policy_doc)
    from ..hostobs import native_context
    if native_context.source_of_run(run) == native_context.SOURCE:
        if (run.get("host") or {}).get("policy_selection", {}).get("tool_profile") == "native_v2":
            instructions = [
                "Use the assigned native worker's gx_task_acquire with the task_id. Read its complete "
                "brief and referenced inputs. Keep the returned attempt and lease for recovery.",
                "Use gx_task_finish with the task_id and prepared manifest_ref. A single published "
                "task artifact is also a valid manifest_ref. Check the returned submission, "
                "structural result and independent scientific review status.",
                f"Task {task_id}. Deliver the frozen product types: {', '.join(product_kinds)}.",
            ]
            inputs = json.loads(run["inputs_json"])
            kind = (task.get("input") or {}).get("output_kind")
            if kind and kind in inputs.get("inputs", inputs).get("execution_profiles", {}):
                instructions.insert(1, f"This {kind} uses the project-selected model. Call "
                                    "gx_model_activity with this task_id. If pending, poll it again after a "
                                    "short wait. Use its completed artifact_ref as the manifest_ref for "
                                    "gx_task_finish. Report an unknown outcome to the coordinator.")
            return BriefSection(title="How to hand it in", body="\n".join(instructions),
                                refs=["contracts/mcp_tools.json"])
        examples = [
            ("gx_task_claim", {"task_id": task_id, "worker_role": role,
                               "expected_attempt": "<integer from the selected READY row>",
                               "as_of_event_id": "<integer from that row's dispatch plan>"}),
            ("gx_task_bind", {"lease_ref": "<returned lease>", "host": "codex",
                              "delegation_mode": "native_subagents", "evidence_level": "CLIENT_REPORTED",
                              "idempotency_key": f"bind-{task_id}-{attempt}"}),
        ]
        for product_kind in product_kinds:
            examples.append(("gx_artifact_put", {"run_id": run_id, "task_id": task_id, "kind": product_kind,
                "media_type": "application/json", "content": "<actual bundle JSON>" if kind else "<actual product JSON>",
                "idempotency_key": "<new key for these exact bytes>"}))
        submit_refs = ["<published reference>"] if kind else [f"<published {name} reference>" for name in product_kinds]
        examples.append(("gx_task_submit", {"task_id": task_id, "lease_ref": "<returned lease>",
            "artifact_refs": submit_refs, "idempotency_key": f"submit-{task_id}-{attempt}"}))
        lines = [
            "Call the exposed GX dynamic tools directly on the controlled native connection.",
            "The callback supplies actual identity and Host-applied configuration. Do not use "
            "the legacy CLI, choose a subject, copy an identity marker or run an authorization handshake.",
            "For a new claim, use the selected READY row's expected_attempt and its plan's as_of_event_id. "
            "Include the row's lease_seconds when supplied. On STALE_PLAN, request a fresh dispatch row.",
            "Bind the returned lease once. Continue saved work directly on an existing valid, bound lease.",
            "Read the complete brief identified by the claim's brief_ref and brief_sha256 and its "
            "required sources. Complete the required checks, publish the artifacts and call "
            "`gx_task_submit` directly with their published references.",
        ]
        for tool_name, arguments in examples:
            lines.extend(["", tool_name, "```json", _json(arguments), "```"])
        if kind:
            lines.extend(["", "Use `gx_bundle_check` before submission when the task explicitly requires "
                          "a precheck, the bundle format is new, you are repairing a rejection or losing a "
                          "remaining attempt would be costly. Supply the published artifact_ref, bundle_kind "
                          f"`{kind}`, task_id and current lease_ref. Follow a fault-injection exercise's "
                          "frozen instructions for the expected refusal. The formal submission records "
                          "each verdict and the reasons for refusal."])
        lines.extend(["", "For large or binary project files, use gx_artifact_get with artifact_ref and "
                      "materialize=true when offered, then open data.metadata.workspace_path. On threads with "
                      "paged text reads, save each page as it arrives, follow metadata.next_offset until "
                      "truncated=false and verify the full hash. Pass filenames to local commands. "
                      "Report actual submission/verdict/references.",
                      f"Required Host configuration: {requirement['model']}; allowed efforts: "
                      + ", ".join(requirement["allowed_efforts"]) + ". This is not upstream model attestation."])
        return BriefSection(title="How to hand it in", body="\n".join(lines),
                            refs=["contracts/mcp_tools.json"])

    bind: dict[str, Any] = {"lease_ref": "<lease_ref from the claim>", "host": host}
    if host == "claude_code":
        bind.update(model="<the id of the model you are running on>", effort="<the effort level you run at>",
                    delegation_mode="native_subagents")
        who = (
            "With the host's GX hooks installed the backend already knows which of the host's agents "
            "you are: the host said so when you ran the claim. If your context holds a line beginning "
            "\"GX host adapter facts\", add `agent_ref` and `host_session_ref` with the two ids it "
            "gives. What you say here about your model and effort is a report; the verdict is read "
            "from the host's record of your turns. If you are the session itself doing the task, "
            "use `single_agent_sequential`."
        )
    elif host == "codex":
        root_session = _codex_root_session(run, policy_doc)
        bind.update(host_session_ref=root_session or "<enclosing Root session UUID from verified claim evidence>",
                    agent_ref="<native worker UUID from verified claim evidence>",
                    model="<the id of the model you are running on>",
                    effort="<your reasoning effort>", delegation_mode="native_subagents")
        identity_words = (
            "The example's host_session_ref is the enclosing Root session UUID verified when "
            "this run started. " if root_session else
            "This brief has no backend-verified enclosing Root session UUID to put in the example. "
            "Resolve it from backend-verified claim or dispatch evidence before binding; do not guess it. "
        )
        who = (identity_words
               + "agent_ref identifies the native thread doing this task: for a subagent, its own "
                 "child UUID. Do not use that child UUID as host_session_ref, or copy the run-start "
                 "coordinator's native_agent_ref as the worker identity. Replace every placeholder "
                 "with its actual value before running the literal command. "
               + ("Report the model and reasoning effort you actually run at. The backend verifies "
                  "your native claim binding and actual Host-applied turns; a report cannot satisfy "
                  "this run's strict observation gate."
                  if policy_doc.get("policy_host") == "codex" else
                  "Report the model and the reasoning effort you actually run at. These identity "
                  "fields are reports and cannot establish native verification."))
    else:
        bind.update(delegation_mode="single_agent_sequential")
        who = "A script or a person has no model to report; leave `model` and `effort` out."
    bind.update(evidence_level="CLIENT_REPORTED", idempotency_key=f"bind-{task_id}-{attempt}")

    def call(tool: str, arguments: Mapping[str, Any]) -> str:
        # A placeholder that stands for a number is written without quotes, so that putting
        # the number in its place leaves valid JSON.
        text = json.dumps(arguments, ensure_ascii=False)
        for placeholder in ("<expected_attempt>", "<as_of_event_id>"):
            text = text.replace(f'"{placeholder}"', placeholder)
        return f"{GX_BIN} call {SUBJECT_OPTION} {tool} --json '{text}'"

    lines = [
        f"The client is `{GX_BIN}`. Every command prints one JSON envelope and exits 0 for ok, 1 "
        "for a refusal (read `error.code` and `error.suggested_recovery`), 2 if the call was not made.",
        "",
        f"Every call names your subject, right after the subcommand: `{SUBJECT_OPTION}`. Your subject is "
        "the name you were given with this task; a lease belongs to the subject that claimed it, and a "
        "call under another subject is refused. Write each command out in full, with the values in "
        "place: a command with a shell variable or a command substitution in it cannot be checked by "
        "the host before it runs, and is refused where nobody is there to approve it.",
        "",
        "1. Claim the task, if you have not, with the `expected_attempt` you were handed with it (its plan "
        "row's: the attempts the task had started when the plan was made) and the plan's `as_of_event_id` "
        "(leave that out only when you were given none). If the claim answers `STALE_PLAN`, the plan has "
        "moved on: do not claim again with another number; say so and stop. Ask for a lease as long as the "
        "work needs.",
        "   ```",
        "   " + call("gx_task_claim", {"task_id": task_id, "worker_role": role,
                                       "expected_attempt": "<expected_attempt>", "as_of_event_id": "<as_of_event_id>",
                                       "lease_seconds": 3600}),
        "   ```",
        f"2. Say who is doing it. {who}",
        "   ```",
        "   " + call("gx_task_bind", bind),
        "   ```",
    ]
    if kind:
        lines += [
            "3. Write the bundle to a file and hand the file over. The answer names its `artifact_ref`.",
            "   ```",
            f"   {GX_BIN} put {SUBJECT_OPTION} --run {run_id} --task {task_id} --kind {kind} --file <path to the bundle>",
            "   ```",
            "4. Have it judged without changing anything, and correct what is refused.",
            "   ```",
            "   " + call("gx_bundle_check", {"artifact_ref": "<artifact_ref>", "bundle_kind": kind,
                                             "task_id": task_id, "lease_ref": "<lease_ref>"}),
            "   ```",
            "5. Submit. One bundle of this kind per submission; other artifacts it refers to may be "
            "named beside it.",
        ]
        submit_refs = ["<artifact_ref>"]
    else:
        lines.append("3. Publish each required artifact. Keep the references for submission.")
        for name in product_kinds:
            lines.extend(["   ```", f"   {GX_BIN} put {SUBJECT_OPTION} --run {run_id} --task {task_id} "
                          f"--kind {name} --file <path to the product>", "   ```"])
        lines.append("4. Submit the published references and any accompanying artifacts.")
        submit_refs = [f"<published {name} reference>" for name in product_kinds]
    lines += [
        "   ```",
        "   " + call("gx_task_submit", {"task_id": task_id, "lease_ref": "<lease_ref>",
                                        "artifact_refs": submit_refs,
                                        "summary": "<one sentence>",
                                        "idempotency_key": f"submit-{task_id}-{attempt}"}),
        "   ```",
        ("6." if kind else "5.") + " Tell whoever started you the `submission_id`, the task status and how many records were "
        "accepted and refused. Not the content.",
        "",
        "A long artifact is read as a file: "
        f"`{GX_BIN} get {SUBJECT_OPTION} <artifact_ref> --out` prints the path of a verified copy.",
        "",
        f"Required of the worker: model `{requirement['model']}`, effort "
        + " or ".join(f"`{level}`" for level in requirement["allowed_efforts"]) + ".",
    ]
    return BriefSection(title="How to hand it in", body="\n".join(lines),
                        refs=["contracts/mcp_tools.json"])
