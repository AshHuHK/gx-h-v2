---
name: gx-native-run
description: Advance a GX run through its controlled Codex native tools, dispatch READY tasks, and resume after actual worker results.
---

Codex owns native workers, messages, waiting and context. GX owns business teams,
assignments, tasks, leases, budgets and evidence. Use the existing tools; this
procedure grants no additional execution permissions.

Coordinator:

1. Dispatch from a fresh READY row and its plan's `as_of_event_id` when both are
   already available. Otherwise call `gx_next_actions` once and select a row.
   On `STALE_PLAN`, refresh the plan and select again. Consult `gx_run_status`
   when a pause, terminal state, budget or anomaly needs a run-wide explanation.
   Send the complete row and its plan cursor with the native task message,
   including `task_id`, `role`, required model and effort, `attempt`,
   `expected_attempt` and the frozen brief's reference and hash. Read existing
   briefs by reference.
2. Match the actual Host model/effort and installed role to the task and pinned
   policy before dispatch; a mismatch stops for the user's decision. On pinned
   Codex 0.169.0 V2, a fresh author dispatch is:
   `spawn_agent({task_name:"author", agent_type:"gx_skill_author", fork_turns:"none", message:"<complete action row, plan cursor, task/brief references and required guidance>"})`.
   Use the actual matching `agent_type`, such as `gx_verifier` for review.
   Omitted, null or empty `fork_turns` defaults to `all`; role selection alone is
   not fresh, and `fork_context:false` is unsupported. Verify observed
   `fresh_context` in the CORRELATION_ONLY native roster when required; unknown is
   not true. Do not invent an effort argument, identity or fresh-context evidence.
3. Use actual registered Root/worker subjects from the controlled native
   connection for GX team management. Reuse the logical team for the same business
   authorization domain, including same-task repair; do not create a team per
   worker, thread or retry. Settle the required membership and `ASSIGNED` task
   record for the actual worker before it claims. Native creation and GX assignment
   are separate effects: retain and resolve partial failure through existing tools.
   A roster correlation or requested role grants no assignment or authority.
4. For edits within one team, pass its latest `expected_team_revision` along with the last known run revision. For cross-team writes, use the latest successful envelope `revision` as
   `expected_revision`. Re-read when it is missing or stale or a write conflicts.
5. Use Codex native messages/follow-up/wait for ordinary start, progress, completion
   and wakeup notices. Keep required durable business handoff, exact-version
   references and approvals in GX. Run independent work within both actual Host
   capacity and GX budgets. Send substantive review to a different non-author
   worker using `gx-native-review`, then act on the actual GX submission result.

### Selecting the workflow

Register the objective with the exact project idempotency key supplied by the
driver. For a new run select a shipped workflow whose methods and completion
evidence answer the question. On continuation use the saved workflow, frozen
inputs and actual open tasks.

For a synthetic numerical grid investigation select `science_grid@1`. Before
the first Job, write the testable predictions, prospective threshold, and
versioned grids in `inputs.proposal`, and set `inputs.max_rounds`. Scope each
prediction to the samples the method can distinguish. The request example is
in `examples/science_native/question.md`. GX runs each accepted grid. Assign
the evidence assessment to an independent reviewer who reads the original
measurements. Assign the evidence-based next-round decision to a separate
planner. Respect completed Jobs, earlier negative evidence and round budgets.

For independent module deliverables select `module_collaboration@1` with
`inputs.modules` containing the module keys and questions. A module may
specify a lead role. Its handoff shapes are in `workflows/module_collaboration/`.

### Module leads

Use this pattern when the project requests `module-leads` or when Root chooses it for independently deliverable parts of a larger objective. Group work into modules with a clear question and inputs. Root chooses the modules, appoints one native lead for each and gives each lead the scope of its GX team. Use the existing team leader and task grants when the lead manages assignments. Ordinary work, messages and follow-ups use Codex's native parent and child threads. Keep room for workers in the shared concurrency limit and start further modules in waves when needed.

Each lead chooses the work items and dispatches independent READY items in parallel. Include the action rows and relevant inputs in the child briefs. Each worker submits its complete result to its GX task and tells the lead where to find it. The lead reads the results, explains agreements, conflicts and open questions, and hands Root the module's conclusion, recommended next step and original result references. A discrepancy local to one module can go back to its original worker. Surface a finding that changes another module's inputs promptly.

Root compares the module handoffs against the original deliverables. It can conclude, return selected modules with a concrete question, or report the remaining gap. Reuse accepted work in other modules. Use `module_collaboration@1` when the project needs this coordination as its own deliverable.

The controlled entry supplies complete installed canonical procedures as ordinary
Root guidance. A child may use the complete, version-identified body supplied in
its native task message;
this is neither child Core Skill activation nor identity evidence. `$skill` text
in a V2 inter-agent message does not activate that loader. With complete guidance
and the task brief available, separate Core activation proof is not an acceptance
condition. Record warnings and continue, including experimental notices or optional
catalog failures; notification wording is not an admission or approval gate.
If required guidance/tools are unavailable, identity or model policy is affected, or
a business write is UNKNOWN, apply the existing business constraints and recovery.
Retain the current sandbox permissions.

### Worker entry and continuation

When the installed catalog offers `gx_task_acquire`, call it with `task_id` on the
assigned worker. Read the complete brief and referenced source material. Deliver the
prepared manifest through `gx_task_finish({task_id, manifest_ref})`. For a single
authored result, publish it with its task_id and use the returned artifact_ref as
manifest_ref. Retain the submission receipt and consult its scientific status.
Give this procedure to each child using the same installed catalog.

For the native_core catalog:

1. For a new attempt, claim the assigned task using the supplied task role,
   `expected_attempt` and `as_of_event_id`. Use the attempt, lease, `brief_ref`
   and `brief_sha256` returned by the claim. This brief includes any assignment
   added after the READY row was selected.
2. Bind a newly claimed lease once. Save the request and its idempotency key.
   `gx_task_bind({lease_ref:"<actual claim lease>", host:"codex", delegation_mode:"native_subagents", evidence_level:"CLIENT_REPORTED", idempotency_key:"<new key>"})`.
   GX supplies observed execution evidence.
   If the original response was lost, follow the business-effect recovery below.
   Replaying a completed bind with its original arguments and key returns the recorded receipt.
3. Read the complete attempt brief and referenced frozen inputs, including overflow
   sections, acceptance requirements, exact versions and prior rejection receipts.
   Complete the work and required checks, publish the artifacts and call
   `gx_task_submit` directly with their references. For a bundle, use
   `gx_bundle_check` before submission if the frozen brief or dispatched assignment
   requires it, the format is new, you are repairing a rejection or losing a
   remaining attempt would be costly. Follow the frozen instructions for an
   explicitly assigned fault-injection exercise.

Read text artifacts with `gx_artifact_get`. When a response is truncated, continue
from `metadata.next_offset`. The public project profile gives Codex a read-only
local environment and returns metadata for binary artifacts. Publish authored
results with `gx_artifact_put` and retain the returned reference in the task.
If the available input cannot support the requested work, tell the coordinator
which artifact or readable format the task needs.

When the original worker resumes a RUNNING task with its still-valid, already-bound
lease, continue the saved work on that lease and call `gx_task_submit` when ready.
Skip claim and bind on this path. A fresh bind key requests another start.
Submission checks the current native worker, model and effort against the original
lease and retains earlier adverse observations and any gap between connections.

Preserve a rejected candidate and its receipt. Repair the same task's next legal
attempt, using native follow-up to the existing author when appropriate; retain
failures and budget consumption. Resume the same workflow only when its actual
interrupt gates are satisfied, with its current revision and interrupt.

For UNKNOWN business effects, inspect the original request, idempotency receipt and recorded effects
before another side effect. Persistent business-effect uncertainty permits only supported read/
reconcile operations. Resume the same owned state and Root through the official
connection after owned-process cleanup and resolution of pending business effects.
Use the original worker and the task's current lease disposition. For a released
or expired lease, follow the task owner's recovery and reuse saved work on the
next legal claim.

A failed transport with no pending GX business effects remains a failed protocol
record. After confirmed owned-process cleanup, the native entry verifies the same
Root and current model/effort through the official connection before allowing
ordinary continuation. Reuse accepted tasks and actual ready work; do not rebuild
the run or redo research merely for that closed connection. Actual pending writes,
unknown Jobs, missing identity/model facts or incomplete cleanup retain their
existing owner checks. A status read alone does not settle those effects.

Stop and report missing identity, policy mismatch, safeguard refusal or exhausted
budget. Do not use private Host readers, identity markers or manual authorization
handshakes on this route. Read final task, asset, check and run states separately;
submitted, accepted, ACTIVE, workflow completion and scientific support are distinct.
