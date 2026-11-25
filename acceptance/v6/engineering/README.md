# V6 acceptance

Run from the release root. The existing acceptance runner provisions the locked Python and Node environment, records the source delta, invokes each selected process and archives its evidence. Supply the released local runtime location if the official host is installed in a separate directory.

```sh
bin/gx-acceptance run v6-engineering --cache "$GX_RUNTIME" --python "$GX_PYTHON" --clash "$GX_CLASH" --run-id v6-check
bin/gx-acceptance report v6-check --json
bin/gx-acceptance verify v6-check --extract-to /tmp/v6-check-inspected
```

| Case | Product behavior under test | Related V5 case |
|---|---|---|
| V6-GOAL | Separate public CLI processes create, reopen and resume one headless Run with Codex offline. The same run key returns the original Run and rejects a different Goal. | A-03, B1-02 |
| V6-MIGRATION | The actual old paused GX ledger and artifact archive opens read-only, migrates and opens twice. It retains the original workflow pin, all old business rows and three settled turns. | A-04, B1-01 |
| V6-STATIC | The existing public science_grid@3 recipe runs through the official host with two real CPU Jobs; a cold terminal replay retains the results and budget. | A-04 |
| V6-DECISION, V6-NATIVE | Decision inputs, graph transaction, scopes, lease, transport identities, replay and Goal/version checks. | B1-06, B1-07 |
| V6-DISPATCH | Accepted operations use the Job owner. Test dispatch identity, recovery and scoped revocation. | B1-07, B3-04 |
| V6-CLAIMS, V6-COSTS, U04, U08 | Claim/usage state, estimated exposure, late receipts and cold reconciliation. | B1-03, B1-08 |
| V6-PROTEIN | Three released capabilities execute against deposited coordinates, including independent numerical review. | B2-01, B3-04 |
| V6-LOOPBACK | The pinned official host sends two separate Lead turns to a scripted peer in a loopback-only network namespace. The CLI accepts the initial revision, executes a real inventory Job, delivers its result, accepts closure and cold-resumes the terminal Run. | A-03, B1-02, B3-08 (transport and compute) |
| V6-EXPORT-NEGATIVES | Source, action and cost tampering in synthetic export fixtures is rejected. | B2-05, B3-07 |

To verify an externally captured experiment:

```sh
bin/gx-acceptance run v6-exports --observed-export "$OBSERVED" --observed-anchor "$OBSERVED_ANCHOR" \
    --controls-export "$CONTROLS" --controls-anchor "$CONTROLS_ANCHOR" --cache "$GX_RUNTIME" --python "$GX_PYTHON"
```

A missing collection or detached reviewer anchor yields `BLOCKED`. An inconsistent export fails. An accepted export authenticates consistency against the supplied digest. Live provider identity, visibility and scientific interpretation each require their own source records and independent review. The combined suite is `v6`. Each suite accepts repeatable `--case` flags. The `all` and `hardening` suites retain their original P2A inventory.

## Deployment receipts

The frozen source commit `fe7ff695a7328c198ffb5bb90b50cc2391f6f397` passed all 13 engineering cases in `v6-release-fe7ff69`. The acceptance verifier checked all 71 raw files. The compressed archive SHA-256 is `4b1fedfcac28d9729c1ac3a0be7413c1ecb1ed8457fa2251568d157fa73c23f3`. The earlier integration run is preserved as `v6-m0-m2-engineering`. The archive includes the CLI outputs, the post-restart databases, the two actual official request catalogs and decision bodies, a real Job output, scientific manifests, independent fit comparisons and budget readings.

```sh
bin/gx-acceptance verify v6-release-fe7ff69 --runs-dir acceptance/v6/engineering/runs
bin/gx-acceptance report v6-release-fe7ff69 --json --runs-dir acceptance/v6/engineering/runs
bin/gx-acceptance verify v6-scripted-observed-export --runs-dir acceptance/v6/engineering/runs
python3 acceptance/v6/verify.py acceptance/v6/engineering/loopback-bundle \
    --anchor acceptance/v6/engineering/loopback-review/detached-anchor.json
```

A separate investigator cross-checked the native rollout, terminal event, provider peer and Job files of the observed inventory export. An independent Biopython recount of the actual input found 164 observed positions and 656 backbone atoms in each chain. The case had two scripted Lead decisions and one real inventory Job. Its source IDs, costs, integer comparison and both verifier receipts are in `loopback-review/review.json`.

The separate formal collection `v6-paid-exports-first` checked the first live observed study and first matched control trio against their independently held anchors. The observed export passed the product verifier. The control trio failed because the neutral event commissioned geometry work. The machine-readable summary retains both verdicts and the failure reason.

```sh
bin/gx-acceptance report v6-paid-exports-first --json --runs-dir acceptance/v6/engineering/runs
```

The same frozen source also passed 62 inherited tests for the engine, resume policy, waiting leases, native crash receipts, root delivery, plugins and geometry. Exact modules, runtime, command and log are in `legacy-fe7ff69/receipt.json` and `legacy-fe7ff69/tests.log`.
