# V6 → V6.1 in-place recovery

The old production CLI created the Goal at a private installation path. Its code comes from Git commit `47fc728d32bc10ba478df6a063529a8d98f98828`. The fixture generator uses scripted native identities to commit the first graph and a process-local, synthetic 73-token metering receipt. The coordinate inventory is a real, detached CPU Job using the archived 2RE9 mmCIF and the older protein pin. No provider was contacted. The two archives in this directory are the staging and terminal images of the same Run. SHA-256 digests and origin are in [source.json](source.json).

Run from the candidate repository root, using CPython 3.11. The source checkout must contain the reviewed V6 Git commit; `--baseline` may point to a separate local Git object store. No shared installation or live project state is modified.

```bash
python3.11 -B acceptance/v61/migration.py --baseline . --out /tmp/v61-migration-check
```

The command stages the actual old code and generates a new independent Run, with newly allocated IDs. The baseline owns the original native marker and its absolute source and state paths. The test first records all business-row digests and checks every registered artifact's bytes. The candidate replaces the installation at the very same path. Two independent public `resume` processes must retain terminal state, graph head, claim revisions, settlement and every Job identity.

The pre-dispatch snapshot retains the committed decision and outbox, with zero Jobs. On a cold candidate resume the original pin must produce its CPU result and the same operation key must prevent an additional submission. In a separate copy the instrumented **old** CLI exits with code 83 just after the actual JobService submission returns. At capture the Job has its original reservation, while its node still has no result. A candidate process then reconciles that same external process. It must retain a single Job and reservation through the second resume. Both tests use a disconnected model.

Fresh databases and an older paused static workflow are opened separately. For rollback the test reinstalls the reviewed V6 source at its original path and restores the entire pre-upgrade state copy. A final V6 `resume` must return the original business rows and recorded cost. Operators should quiesce new admissions, keep the complete state/Job/artifact directory and matching Codex thread store, and back up SQLite with its online backup API or an equivalent consistent snapshot. Restore matching code, configuration, state and native home to the same absolute paths.

The raw P1 results are [the terminal before/after comparison](../migration-results/terminal.json), [pending dispatch](../migration-results/pending.json), [the real worker interruption](../migration-results/inflight.json) and [rollback](../migration-results/result.json). The independently retained live V6 archive has a [separate observed-state receipt](../migration-observed.json). The old-worktree tests do not repeat any paid model turn.
