# GX-H v3 Lean Acceptance Matrix

Every pass requires source path, test name, exact command, exit code, evidence path and commit SHA.

## Gate 0
- [ ] >=2h real-model multi-Root run before changing hook call sites/hot path.
- [ ] Model/provider/Codex SHA/hardware/workload/concurrency recorded.
- [ ] Throughput, Turns, Root concurrency, tokens, queue wait, callback/SQLite latency, Jobs, recovery, errors recorded.
- [ ] Logs/artifact hashes preserved; loopback tests labeled separately.

## P0
- [ ] Strict Manifest; operator-approved install root.
- [ ] Digest verified before import; rejected package import canary never runs.
- [ ] Deterministic dependencies and cycle path.
- [ ] Namespace derives from plugin ID + version + digest.
- [ ] Two versions coexist in one process.
- [ ] Relative/nested imports work; no global sys.path mutation.
- [ ] Failed import cleanup removes only its namespace.
- [ ] register(ctx) once per version; Registry frozen.
- [ ] Legacy v2.1 workspace starts without plugin metadata.

## P1
- [ ] science_grid@2 loaded via plugin entrypoint.
- [ ] Author API derived from actual migration.
- [ ] Plugin imports only stdlib, Author API and plugin-local modules.
- [ ] Existing Capability Manifest bundles use existing Registry/Governance.
- [ ] Generic core no longer imports science_grid-specific modules.
- [ ] Existing Job/Artifact/Evaluation owners remain authoritative.
- [ ] Existing pinned Runs cold-resume.
- [ ] 2h real-model post-change comparison vs Gate 0.

## P2
- [ ] Independent plugin has no science_grid dependency.
- [ ] Adds Tool without Kernel source edits.
- [ ] Adds existing-format Capability Manifest or real Evaluator.
- [ ] Tool bound to intended Run/Root/Attempt.
- [ ] Capability discoverable through existing Registry.
- [ ] Job/Artifact end-to-end works.
- [ ] Removal affects only dependent Runs.

## P3
- [ ] Run-level exact plugin ID/version/digest pin.
- [ ] Same plugin ID v1/v2 coexist in one process.
- [ ] Hooks/tools/capability refs do not cross versions.
- [ ] New version does not change old Run; new Run selects new version.
- [ ] Cold restart restores both versions.
- [ ] Missing pin blocks only dependent Run.
- [ ] Rollback restores exact digest/composition.
- [ ] Legacy workspace remains compatible.

## Cross-cutting
- [ ] Full v2.1 regression green.
- [ ] Existing Workflow/Capability pins remain authoritative.
- [ ] No duplicate Capability Manifest/Registry or Job/Budget/Lease/Artifact/Workflow owner.
- [ ] No generic EventHandler, lifecycle manager, Grants engine or Worker Process.
- [ ] prepare_dispatch after cheap capacity checks.
- [ ] No long-run claim without actual elapsed evidence.
