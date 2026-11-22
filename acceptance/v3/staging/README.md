# V3 staging and frozen v2.1 recovery fixture

The private installation uses CPython 3.11.9, SQLite 3.45.3, Biopython 1.88, NumPy 1.26.4 and official Codex 0.169.0. The checked-in wheel hashes and local Codex archive supplied the offline installation. The release verifier checked 10,235 upstream source files, 1,430 experimental protocol files, the native executable and four Cordis packages. Exact commands, output and exit codes are in [runtime.json](runtime.json). The first version probe reported a stale temp directory cleanup warning and exited 0. A second version probe used a fresh private Codex home.

## Captured source

The frozen v2.1 product commit `a05dcef64522fbbbc12120b1d78da518e6886b10` created run `gxr_01m825ktkpt3ewxtdm` from its public `bin/gx-p0 start --demo-grid` entry point. The official App Server used the credential-free `127.0.0.1` Responses fixture. All three CPU Jobs executed through GX. The public stop drained the driver after the third Job was admitted. Its terminal sidecar was ready for reconciliation when the snapshot was taken. Historical project files are under this checkout's ignored `var/v3-staging/frozen-a05dcef/`. The v2.1 checkout supplied the locked Codex package archive only.

| Checkpoint | Observed |
|---|---|
| GX database | SHA-256 `35ac81b908ae1c01bdd8289e23533b839e3addaf6044b6fc41f295affe7a2b2b`, integrity OK, zero foreign-key violations, 20 original P0 migration markers |
| Owned research | 2 accepted submissions, 3 settled native turns, 32 artifact references checked against the retained bytes |
| Job state | 3 launch claims, runner identities and final CPU outcomes retained and hashed |
| Native state | SQLite backup of 6 Codex databases, 3 rollout files hashed, remaining ordinary files copied with the source tree |
| Cold status | Frozen public status/run and the inherited v3 checkout's status reopened the same paused Run with 0 additional fixture responses |

The original source tree, its SQLite backup and both generations of Job receipts stay under the private staging root. The summary includes only fixture identifiers, counts, command receipts and checksums. The first fixture trial requested a seven-second worker delay above the worker's five-second maximum. Its failed Job and receipt remain under `var/v3-staging/legacy-a05dcef/`. The recorded checkpoint uses the supported five-second delay. [Checkpoint and command receipts](cold-baseline.json)

## Replay against a committed candidate

Run from the v3 checkout. For the retained checkpoint, run one replay at a time. The script temporarily restores copies under the fixture's original absolute paths and binds its original localhost port. Each invocation takes a fresh audit directory. On another machine, generate a local checkpoint with `cold_fixture.py` first so absolute paths and the loopback endpoint match the installation.

```sh
.venv/bin/python -B acceptance/v3/staging/cold_fixture.py \
  --work var/v3-staging/new-legacy-fixture \
  --output var/v3-staging/new-cold-baseline.json

.venv/bin/python -B acceptance/v3/staging/replay_checkpoint.py \
  --baseline acceptance/v3/staging/cold-baseline.json \
  --candidate "$candidate_commit" \
  --audit "var/v3-staging/replay-$candidate_commit" \
  --output "var/v3-staging/replay-$candidate_commit.json"
```

Use the newly generated baseline JSON for a newly generated fixture. The second command as written selects the original retained checkpoint. The replay exports the entire candidate commit, uses the checkout's verified independent native binary and Python environment, and opens the database through writable public `stop`. It checks historical ownership, budget balances, workflow pin, published artifact bytes and Job identity. Before candidate continuation, it restores the frozen software and a fresh snapshot, then reopens the run with public v2.1 `status` and `run`. The candidate resumes that restored run once, consumes the third Job's existing outcome, verifies accounting against the fixture and reopens the terminal result.

The control used frozen v2.1 for both software selections. Separate work on the P3 candidate should rerun this procedure with its commit, then exercise runs created with exact v3 compositions in the same process and after restart. For those runs compare plugin ID, version, content digest, closure, API version, workflow and capability pins, and the stored composition digest against the pre-upgrade values. Remove the selected installation in a private copy, check that only its dependent run blocks, then restore its exact bytes and verify the original composition. [Frozen control rehearsal](control-replay.json)
