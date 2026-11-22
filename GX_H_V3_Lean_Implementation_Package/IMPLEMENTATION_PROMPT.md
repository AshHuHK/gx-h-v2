# Coding Agent Prompt — GX-H v3 Lean Implementation (P0–P3)

You are the Principal Architect, Python Runtime Engineer, GX Integration Engineer and Acceptance Owner. Implement the lean v3 architecture in `AshHuHK/gx-h-v2`, inheriting v2.1. Read `GX_H_V3_BLUEPRINT.md` fully first.

## A. Baseline and branch safety

Reference anchors:
- v2.1 reviewed HEAD `61ed0067448658e02d181b3cb85d7f68e39b6616`
- frozen product code `a05dcef64522fbbbc12120b1d78da518e6886b10`
- Codex baseline 0.169.0

These are not reset instructions. Inspect actual checkout and preserve later commits/local work:
```bash
git status --short
git branch --show-current
git rev-parse HEAD
git log --oneline -25
```
Create v3 from verified current v2.1 HEAD only after audit. Never force-push or discard work.

## B. Mandatory audit before code

Inspect at minimum:
- `gx-h/gx_core/p0/{driver,roots,impact,root_delivery,coordinator}.py`
- `gx-h/gx_core/capabilities/{manifest,registry,shipped,environments}.py`
- `gx-h/gx_core/orchestration/{hooks,registry,packages,service}.py`
- `gx-h/gx_core/jobs/`, `artifacts/`, `budgets/`
- `gx-h/gx_core/teams/service.py`
- `gx-h/config/{shipped.json,workflow_packages.json}`
- actual `science_grid@2` package and all transitive P0 science modules
- `gx-h/runtime/plugin-host/`
- v2.1 acceptance/deployment/rollback/dependency closure reports.

Create:
- `V3_SOURCE_AUDIT.md`
- `V3_IMPORT_AND_VERSIONING_DECISION.md`
- `V3_AUTHOR_API_FROM_SCIENCE_GRID.md`
- `V3_MIGRATION_MAP.md`
- `V3_IMPLEMENTATION_PLAN.md`
- `V3_ACCEPTANCE_PLAN.md`

Audit all 13 hook call sites, transaction boundaries, raw `conn` use, workflow-specific imports, capability pin ownership and actual callers. Do not infer unused code from grep alone. Commit audit and import/version ADR before coding.

## C. Non-negotiable rules

- Do not fork/modify Codex or replace its Agent Runtime.
- v2.1 remains sole owner of Task/Attempt/Lease/Fencing/Intent/Budget/Metering/Job/Capability/Artifact/Workflow/Operator/Coordinator.
- No duplicate Scheduler, Job Manager, Capability Manifest/Registry, Workflow Registry, Event Bus or Team Manager.
- No generic lifecycle state machine, health manager, Grants engine or EventHandler framework.
- No plugin Worker Process.
- Static import lint is not a security boundary.
- Only operator-installed, reviewed and digest-pinned in-process packages load.
- Agent cannot install/activate code.
- Untrusted/expensive scientific code uses existing isolated GX Job/Capability Runtime.
- Keep Node/Cordis `runtime/plugin-host` separate unless audit proves a concrete adapter need.
- Keep Codex pinned to 0.169.0.
- No Scientific Intelligence implementation in this release.

If a requirement conflicts with these rules, stop and submit a narrow ADR with evidence and alternatives.

## D. Gate 0 — real-model baseline before hot-path changes

Before modifying the 13 hook call sites, `_claim()` ordering, `prepare_dispatch`, Root tool wiring or hot-path behavior:
1. Run at least 2 hours of real-model multi-Root v2.1 workload.
2. Record model/provider/Codex SHA/hardware/workload/concurrency.
3. Measure throughput, completed Turns, Root concurrency, token usage, queue wait, dispatch/callback latency, SQLite transaction latency, Jobs, recoveries and errors.
4. Preserve logs and artifact hashes.
5. Label loopback/scripted tests separately.

If credentials/environment prevent it, mark BLOCKED. Loader-only work may proceed if it does not alter hot paths; do not migrate science_grid or reorder hooks before baseline.

## E. V3-P0 — Lean Loader

Implement:
- strict Plugin Manifest parser/schema;
- operator-installed catalog;
- content digest verification before import;
- GX/API compatibility;
- deterministic dependency resolver/cycle detection;
- versioned import namespace;
- minimal Registry;
- `register(ctx)` once per plugin version;
- registry freeze;
- load-failure diagnostics;
- static import lint only.

### Same-process multi-version requirement is fixed now

Use a unique namespace derived from plugin ID + version + digest. Each version has its own install directory. Load using package spec and `submodule_search_locations`; insert unique package into `sys.modules` before execution; support relative/nested imports; never mutate global `sys.path`; never use global `plugin` module names. Failure cleanup removes only that namespace.

Plugin imports are limited by policy to stdlib + Author API + plugin-local relative modules. Third-party dependencies/native extensions/heavy compute go through existing isolated Job/Capability environments. If a real need violates this, stop for ADR; no silent global sys.path or Worker Process fallback.

No activate/health/deactivate lifecycle. Call `register(ctx)`, collect contributions, freeze registry.

Deliver `V3_P0_REPORT.md`, tests, commands, exit codes and commit SHA.

## F. V3-P1 — Migrate real `science_grid@2` with tools and capabilities

Trace its full import/caller graph. Identify five P0 science modules and all generic core modules importing them. Separate generic contracts/data from science-specific behavior.

Migrate:
- Workflow registration through existing Workflow Registry bridge.
- Worker Tools through `ctx.tools`.
- Existing Capability Manifest bundles in canonical format.
- Capability registration through existing Registry/Governance.
- Execution through existing Job/Capability Runtime.
- Artifact and Evaluation/Evidence through existing owners.
- Remove direct science_grid imports from generic core.
- Preserve old pins and cold resume.

Derive Author API from migration. It must support only real needs: Workflow/Tool registration, transaction-bound Run/Task/Intent context, Task proposal, existing Capability Manifest attachment, Job inspect/propose/wait/cancel, Artifact read/publish, Evaluation/Result proposal.

### Transaction-bound context

New hooks receive `ctx` bound to current Run and transaction context, never raw `conn`, cursor, SQL, DB path or transaction control. It exposes named domain operations only; no nested transactions. No external side effects/network/model/Job execution inside transaction-bound hooks. External commands use existing GX Service boundaries and receipt/idempotency where needed. Read-only calls need no artificial idempotency keys.

Legacy built-in hooks may retain `conn` internally as compatibility-only until migration proves they can be retired.

### P1 performance

Use Gate 0 workload/environment and compare throughput, Turns, concurrency, tokens, queue wait, callback/SQLite latency, Jobs, recoveries and errors. No unexplained regression beyond agreed tolerance.

Deliver `V3_P1_REPORT.md`, `V3_AUTHOR_API_FROM_SCIENCE_GRID.md`, migrated package, Capability registration evidence, regression logs, post-change 2-hour comparison and commit SHA.

## G. V3-P2 — Independent Plugin Proof

Create a second plugin independent of science_grid. It contributes:
- one Tool usable by Codex Root; and
- one existing-format Capability Manifest, or a real Evaluator if more appropriate.

No Kernel source edits to add it. Uses Author API + stdlib + plugin-local imports. Capability routes through existing Registry/Governance/Job Runtime. Tool uses Root/Attempt fencing. Job/Artifact flow end-to-end. Removal affects only dependent Runs.

Deliver `V3_P2_REPORT.md` and commit SHA.

## H. V3-P3 — Run Composition and version pinning

Reuse existing `config/shipped.json`, `workflow_packages.json`, Capability Manifest bundles/Governance and Workflow Registry. No parallel lockfile sources. An installation catalog may record install location and package digest only.

Each Run stores plugin ID/version/digest, API version, Workflow pin, Capability manifest refs, dependency closure and composition digest.

Existing Run never changes composition in place; new version means new Run. Two versions coexist in one process. Restart restores correct versions. Missing pin blocks only dependent Run. Rollback restores exact digest/composition. Legacy workspace opens without destructive migration.

Deliver `V3_P3_REPORT.md` and commit SHA.

## I. prepare_dispatch hot-path correction

Only after Gate 0, reorder `_claim()`:
1. Task state/dependencies.
2. Provider capacity.
3. Team capacity.
4. Root/scheduler capacity.
5. `prepare_dispatch`.
6. Lease issuance/Attempt creation.

No cache. Waiting tasks use wait/backoff, not 35 ms retries. Hook remains before Lease issuance, short/deterministic/side-effect-free, no network/model/Job/heavy compute or writes outside current transaction. Record before/after measurements.

## J. Events

No generic Plugin EventHandler/EventBus. Job completion continues through existing Job owner, Outbox, Coordinator and Workflow wake-up. Add a narrow adapter only when a concrete independent consumer exists and has acceptance tests.

## K. Acceptance/deployment

Use `acceptance/V3_ACCEPTANCE_MATRIX.md`. Test on isolated staging and copied v2.1 DB, Artifact store, Job sidecars and Codex state. Never migrate the user's only live workspace.

Full v2.1 regression after each phase. Record source commit/dirty state, OS/Python/SQLite, Codex version/SHA, package digests, exact commands/exit codes, test counts/skips, DB integrity, composition hashes, Job/Artifact receipts, fault injection and rollback. Separate loopback from real OAuth/model tests. No long-run claim without actual elapsed time.

## L. Required reports

- `V3_SOURCE_AUDIT.md`
- `V3_IMPORT_AND_VERSIONING_DECISION.md`
- `V3_AUTHOR_API_FROM_SCIENCE_GRID.md`
- `V3_MIGRATION_MAP.md`
- `V3_IMPLEMENTATION_PLAN.md`
- `V3_P0_REPORT.md`
- `V3_P1_REPORT.md`
- `V3_P2_REPORT.md`
- `V3_P3_REPORT.md`
- `V3_ACCEPTANCE_REPORT.md`
- `V3_DEPLOYMENT_REPORT.md`
- `V3_ROLLBACK_REPORT.md`
- `V3_KNOWN_LIMITATIONS.md`

Every pass needs source path, test name, exact command, exit code, evidence path and commit SHA.

## M. Definition of done

v3 P0–P3 completes only when:
1. Digest verified before import.
2. Plugin ID/version/digest has unique import namespace.
3. Two versions coexist in one process with relative/nested imports.
4. `register(ctx)` works without lifecycle framework.
5. `science_grid@2` migrates using Author API.
6. Existing Capability Manifest/Registry/Governance remain canonical.
7. Independent plugin adds Tool + Capability/Evaluator without Kernel edits.
8. Run composition cold-resumes with exact pins.
9. Existing v2.1 workspaces remain compatible.
10. No duplicate subsystem exists.
11. Gate 0 and post-change real-model comparison exist.
12. All phase gates and v2.1 regressions pass.

Start with source audit and Gate 0. Do not change the 13 hook call sites or hot path before the real-model baseline is captured.
