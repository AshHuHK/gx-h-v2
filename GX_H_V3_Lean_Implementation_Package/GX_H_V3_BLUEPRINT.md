# GX-H v3 — Lean Plugin Architecture (P0–P3)

**Repository:** AshHuHK/gx-h-v2  
**Reviewed v2.1 HEAD:** `61ed0067448658e02d181b3cb85d7f68e39b6616`  
**Frozen product code:** `a05dcef64522fbbbc12120b1d78da518e6886b10`  
**Codex baseline:** official App Server 0.169.0  
**Scope:** v3 P0–P3 only. These hashes are audit anchors, not reset instructions. Inspect actual HEAD and preserve later work.

## 0. Decision

v3 inherits v2.1. It adds only:
1. Lean plugin loader.
2. Minimal `register(ctx)` Author API.
3. Workflow / Tool / Evaluator extension contracts.
4. Run-level composition and exact version pins.

v2.1 remains the sole owner of Workspace, Run, Task, Attempt, Lease, Fencing, Intent, Budget, Metering, Job, Capability Registry/Manifest/Governance, Artifact, Workflow Registry, Operator, Coordinator and Outbox. Codex remains owner of Agent Loop, native Subagents, Thread and Session internals.

### Four phases plus mandatory baseline

| Gate | Scope |
|---|---|
| Gate 0 | 2-hour real-model multi-Root v2.1 baseline before hot-path changes |
| P0 | Manifest, digest, resolver, versioned import namespace, Registry, `register(ctx)` |
| P1 | Migrate real `science_grid@2`, including its tools and existing Capability Manifests |
| P2 | Independent plugin proof: Tool + existing-format Capability or Evaluator |
| P3 | Run composition, exact pins, same-process multi-version coexistence |

## 1. Non-goals

Do not build:
- Agent Loop, Subagent Manager, Team Manager/Scheduler, Message Bus.
- New Task/Lease/Budget/Job/Artifact/Capability/Workflow Registry.
- Generic lifecycle state machine, health manager, Grants engine, EventHandler framework or plugin event bus.
- Worker Process per plugin.
- Arbitrary shell, unrestricted Python, remote marketplace or agent self-installation.
- Untrusted in-process plugins or a Python sandbox claim.
- Research Intelligence algorithms.
- Codex fork or upgrade during v3.

## 2. Preserve v2.1 owners

| Existing area | Rule |
|---|---|
| `p0/driver.py`, `p0/coordinator.py` | Keep single durable loop and Outbox |
| `p0/roots.py`, `p0/root_delivery.py` | Keep Root/Turn delivery and fencing |
| Codex App Server | Keep Agent Runtime and native Subagents |
| `jobs/`, `jobs/leases.py` | Existing Job/Lease owners |
| `artifacts/` | Existing Artifact owner |
| `budgets/`, `p0/metering.py` | Existing Budget/Metering owners |
| `capabilities/manifest.py` | Canonical Capability Manifest |
| `capabilities/registry.py`, `shipped.py`, Governance | Canonical Capability registry and pins |
| `orchestration/registry.py`, `service.py` | Canonical Workflow Registry |
| `orchestration/hooks.py` | Legacy built-in hook compatibility |
| `config/shipped.json`, `workflow_packages.json` | Reuse existing pin/catalog conventions |
| `runtime/plugin-host/` | Separate optional Node/Cordis evaluator runtime, not the Python GX Plugin Host |

Existing v2.1 projects open without plugin metadata. Existing Run/Workflow/Capability/Task/Job/Artifact history and pins remain valid. New external plugins never receive raw `conn`. Built-in legacy hooks may temporarily keep `conn` signatures through an explicitly internal compatibility path.

## 3. Target architecture

```text
Workspace
  +-- Run A: pinned workflow + plugin versions
  +-- Run B: pinned workflow + plugin versions
                |
          Lean Plugin Loader
          Manifest / Digest / Resolver
          Versioned Import Namespace
          Registry / register(ctx)
                |
       +--------+----------------+
       |        |                |
    Workflow   Tool      Existing Capability Manifest
    Extension Extension  + Registry/Governance
       |        |                |
       +--------+----------------+
                |
          GX Author API (ctx)
                |
       Existing v2.1 service owners
       Task / Intent / Job / Budget /
       Artifact / Workflow / Evaluation
                |
       Existing Coordinator / Outbox
                |
       Official Codex App Server + Job Runtime
```

Plugin Host only discovers trusted packages, validates metadata/digests/dependencies, imports exact versions, calls `register(ctx)`, collects contributions and freezes the registry. It does not own tasks, jobs, budgets, events, agents or scientific execution.

## 4. Trust and import model

The only trust authority for in-process Python is operator-approved installation plus exact content digest pinning. Static import inspection is lint only; it is not a security boundary because Python can access `os`, `sqlite3`, `importlib` and process resources.

- Only operator-installed, reviewed, digest-pinned packages load in-process.
- Agents may propose plugins but cannot install/activate them.
- Untrusted or expensive scientific code runs in existing isolated GX Job/Capability environments.
- Do not claim in-process Python is sandboxed.

Package layout:
```text
plugins/installed/<plugin-id-safe>/<version>/
  gx_plugin.json
  plugin.py
  package/                 # optional plugin-local package
  capability-manifests/    # existing canonical format
```

## 5. Same-process multi-version coexistence — decided in P0

**Use a deterministic versioned import namespace; do not create one Worker Process per plugin.**

Namespace derives from `(plugin_id, version, content_digest)`, e.g.:
`gx_ext_<normalized_id>_<version_slug>_<digest_prefix>`.

Requirements:
- Each version has its own install directory.
- Load with package spec and `submodule_search_locations`.
- Insert the unique package into `sys.modules` before executing it.
- Plugin internal imports are relative to that package.
- Support nested relative imports.
- Never mutate global `sys.path`.
- Never use a globally shared entrypoint name like `plugin:Class`.
- Failed load removes only modules under that exact namespace.
- In-process plugin imports are limited by policy to stdlib + GX Author API + plugin-local relative modules.
- Third-party Python dependencies, native extensions, GPU libraries and heavy compute go through existing isolated Capability/Job environments.
- Static import lint checks policy but is not a security sandbox.
- If a plugin genuinely needs third-party in-process dependencies, stop for ADR; do not silently use global `sys.path` or Worker Processes.

P0 must test two versions of the same plugin in one Python process, including relative/nested imports, isolated module globals and failure cleanup.

## 6. Manifest and pin integration

Minimal Plugin Manifest:
- `id`, `version`, `api_version`, `kind`, `entrypoint`, `gx_compat`, `dependencies`, `extensions`.
- Optional description/author/license/labels.

Plugin Manifest is not a Capability Manifest. Do not duplicate capability schemas, launch config, resources, weights or Method Cards.

Reuse:
- `config/shipped.json`
- `config/workflow_packages.json`
- existing Capability Manifest bundles and Governance
- existing Workflow Registry and content digests

There must be one authoritative source for each plugin package identity, Workflow pin and Capability pin. If an installed-package catalog is required, it records installation locations and package digests only; it must not become a second Workflow/Capability content registry. Do not create parallel lockfiles that can disagree.

## 7. Minimal Author API: `register(ctx)`

No generic lifecycle manager. Import each selected plugin version and call `register(ctx)` once; collect contributions; freeze registry.

Illustrative:
```python
def register(ctx):
    ctx.workflows.register(...)
    ctx.tools.register(...)
    ctx.capabilities.add_manifest(...)
    ctx.evaluators.register(...)
```

Final API is derived from migrating `science_grid@2`, not invented in advance.

### Transaction-bound context

Existing WorkflowHooks often receive `(conn, run_id, ...)` and execute inside an open SQLite transaction. New plugin hooks receive `ctx` bound to the current Run and transaction context, but no raw connection/cursor/SQL/DB path/transaction control.

- `ctx` exposes named domain operations/repositories only.
- It must not open nested transactions.
- Operations that participate in the current transaction use the same underlying transaction internally.
- No external side effects, network, model calls or Job execution inside transaction-bound hooks.
- Job submission and other external commands use existing GX Service boundaries with receipts/idempotency where needed.
- Read-only operations do not need artificial idempotency keys.
- Task creation, Job submission, Artifact publication and Evaluation submission use existing owners and their existing receipt semantics.
- Context is an API/correctness boundary, not a security sandbox against malicious trusted Python.

Legacy built-in hooks may temporarily retain `conn` signatures internally; external plugins never receive `conn`.

## 8. P0 — Lean Plugin Loader

Deliver:
1. Strict Manifest parser/schema.
2. Operator-installed catalog.
3. Content digest verification before import.
4. GX/API compatibility validation.
5. Deterministic dependency resolver and cycle detection.
6. Versioned import namespace.
7. Minimal Registry keyed by `(plugin_id, version, digest)`.
8. `register(ctx)` contribution collection.
9. Registry freeze after bootstrap.
10. Load-failure diagnostics.
11. Static import lint.
12. Example plugin with relative/nested imports.

Do not build lifecycle states, activate/health/deactivate, health service, Grants engine, event bus, hot reload or background workers.

Failure isolates the plugin and dependent Runs; unrelated Runs and read-only status continue. Existing workspace without plugin metadata starts unchanged.

## 9. P1 — Migrate real `science_grid@2` plus capabilities/tools

Select `science_grid@2` as the first real migration target. The cited size (~254 lines) is only an estimate; audit the transitive imports and actual call graph.

Required:
1. Trace all imports/callers of `science_grid@2`.
2. Identify its five P0 science modules and every generic core module importing them.
3. Separate generic contracts/data from science-specific behavior.
4. Move workflow-specific behavior behind plugin entrypoint.
5. Keep core free of direct imports of `workflows.science_grid_v2` and private helpers.
6. Register Workflow via existing Workflow Registry bridge.
7. Register Worker Tools via `ctx.tools`.
8. Include/reference its existing Capability Manifest bundles in canonical format.
9. Register capabilities through existing Registry/Governance.
10. Keep Job execution in existing Job/Capability Runtime.
11. Keep Artifact and Evaluation/Evidence owners authoritative.
12. Preserve old pins and cold-resume.

Author API must support what the real migration needs:
- Workflow registration.
- Tool registration.
- Run/Task/Intent context.
- Task creation/proposal through existing Task owner.
- Existing Capability Manifest attachment/registration.
- Job inspect/propose/wait/cancel through existing Job owner.
- Artifact read/publish through existing Artifact owner.
- Evaluation/Result proposal through existing owner.

Do not expose raw connection or private GX modules.

### Gate 0 performance baseline

Before changing the 13 hook call sites, `_claim()` ordering, `prepare_dispatch`, Root tool wiring or hot-path behavior:
- Run at least 2 hours of real-model multi-Root v2.1 workload.
- Record exact model/provider/Codex SHA/hardware/workload/concurrency.
- Measure throughput, completed Turns, Root concurrency, token usage, queue wait, dispatch latency, callback latency, SQLite transaction latency, Jobs, recoveries and errors.
- Preserve logs and artifact hashes.
- Scripted/loopback tests are labeled separately.

If credentials/environment prevent this, mark BLOCKED. Loader-only work may proceed if it does not change hot paths, but do not migrate science_grid or reorder hooks before baseline.

After P1, repeat the same workload/environment and compare. No unexplained regression beyond agreed tolerance.

## 10. P2 — Independent Plugin Proof

Create a separately installed plugin independent of science_grid. It must contribute:
- one Tool usable by a Codex Root; and
- one existing-format Capability Manifest, or a real Evaluator extension if more appropriate.

No GX Kernel source edits to add it. It uses Author API + stdlib + plugin-local imports. Capability uses existing Manifest/Registry/Governance/Job Runtime. Tool uses existing Root/Attempt fencing. End-to-end Job/Artifact works. Removal affects only dependent Runs.

## 11. P3 — Run Composition and version pinning

A Workspace remains one state directory/one process; multiple Runs may select different plugin versions.

Each Run records:
- Plugin ID/version/digest.
- API version.
- Workflow ID/version/digest.
- Capability ID/version/manifest digest references.
- Resolved dependency closure.
- Composition digest.

Rules:
- Reuse existing shipped pins and registries; no parallel lockfile sources.
- Existing Run never changes composition in place.
- To use a new version, create a new Run.
- Two versions of the same plugin coexist in one process under unique namespaces.
- Restart restores correct versions.
- Missing pin blocks only dependent Run.
- Rollback restores exact prior digest/composition.
- Legacy workspace opens without destructive migration.

## 12. `prepare_dispatch` hot path

After Gate 0, reorder `_claim()`:
1. Task state/dependencies.
2. Provider capacity.
3. Team capacity.
4. Root/scheduler capacity.
5. `prepare_dispatch`.
6. Lease issuance and Attempt creation.

Do not add a cache. Waiting tasks use existing wait/backoff rather than 35 ms retries.

Hook remains before Lease issuance and must be short, deterministic, side-effect-free, with no network/model/Job/heavy compute or writes outside current transaction.

## 13. Events

Do not implement generic Plugin EventHandler/EventBus in P0–P3. Job completion continues through existing Job owner, Outbox, Coordinator and Workflow wake-up. Add a narrow subscription only when a concrete independent consumer exists and has an acceptance test.

## 14. Suggested layout

Adapt to repository conventions; do not create duplicates:
```text
gx-h/gx_core/plugins/
  manifest.py catalog.py digest.py resolver.py loader.py
  registry.py context.py lint.py errors.py
  author_api/
    workflows.py tools.py capabilities.py tasks.py intents.py
    jobs.py artifacts.py evaluations.py

plugins/installed/<plugin-id-safe>/<version>/
  gx_plugin.json plugin.py package/ capability-manifests/
```

## 15. Acceptance

### P0
- Manifest strict validation.
- Operator-approved install root.
- Digest checked before import.
- Invalid/unapproved/digest-mismatched package import canary never runs.
- Deterministic dependencies/cycle path.
- Namespace derives from ID+version+digest.
- Two plugin versions coexist in one process.
- Relative/nested imports work; no global sys.path mutation.
- Failure cleanup removes only own namespace.
- `register(ctx)` once per version; registry frozen.
- Legacy workspace starts without plugin metadata.

### P1
- `science_grid@2` loaded through plugin entrypoint.
- Author API derived from migration.
- Plugin imports only stdlib, Author API and plugin-local modules.
- Existing Capability Manifest bundles registered through existing Registry/Governance.
- Generic core modules no longer import science_grid-specific modules.
- Existing Job/Artifact/Evaluation owners remain authoritative.
- Existing pinned Runs cold-resume.
- 2-hour real-model post-change comparison vs Gate 0.

### P2
- Independent plugin has no dependency on science_grid.
- Adds Tool without Kernel source edits.
- Adds existing-format Capability Manifest or real Evaluator.
- Tool bound to intended Run/Root/Attempt.
- Capability discoverable through existing Registry.
- Job/Artifact end-to-end works.
- Removal affects only dependent Runs.

### P3
- Run-level exact plugin ID/version/digest pin.
- Same plugin ID v1/v2 coexist in one process.
- Hooks/tools/capability refs do not cross versions.
- New version does not change old Run.
- New Run selects new version.
- Cold restart restores both versions.
- Missing pin blocks only dependent Run.
- Rollback restores exact digest/composition.
- Legacy workspace remains compatible.

Cross-cutting:
- Full v2.1 regression green.
- Existing Workflow/Capability pins remain authoritative.
- No duplicate Capability Manifest/Registry or Job/Budget/Lease/Artifact/Workflow owner.
- No generic EventHandler, lifecycle manager, Grants engine or Worker Process.
- `prepare_dispatch` after cheap capacity checks.
- No long-run claim without actual elapsed evidence.

## 16. Implementation discipline

1. Preserve v2.1 branch and frozen product commit.
2. Create v3 from verified current v2.1 HEAD after auditing later work.
3. Source audit and ADR before coding.
4. One phase per commit; tests, report, evidence and rollback per phase.
5. Do not start next phase before prior gate passes.
6. Use copied DB, Artifact store, Job sidecars and Codex state for migration/rollback.
7. Full v2.1 regression after each phase.
8. Keep Codex at validated 0.169.0.
9. Separate loopback from real OAuth/model tests.
10. Record commands, exit codes, hashes, skips and environment.
11. Stop if a change weakens v2.1 safety/budget/lease/fencing/artifact scope.

**Definition of Done:** a trusted plugin is digest-verified before import; two versions coexist in one process; `science_grid@2` migrates using Author API; an independent plugin adds Tool + Capability/Evaluator without Kernel edits; Run composition survives restart; v2.1 workspaces cold-resume; no duplicate subsystem exists; Gate 0/post-change real-model evidence and all phase gates pass.
