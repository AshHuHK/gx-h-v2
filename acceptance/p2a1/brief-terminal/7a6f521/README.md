# Recovery of the interrupted brief-wire turn

The original `1f8fc82c500bc51c9409a01b10b8a4769345030d` public run failed the two-confirmed-turn requirement. On the B3 review arm, the fixture issued six responses of 35 tokens. GX confirmed 105 for the reviewer. The planner settled at its 180,000-token policy estimate after observing 105 from three distinct raw responses and receiving a last public cumulative count of 70. The original configuration left rollout diagnostics disabled. The failed run, both independent SQLite snapshots and all four original B2/B3 rollouts are retained in the [original archive](../../../hardening/runs/p2a1-baseline-1f8fc82/raw.tar.gz). Individual member digests are in the [original manifest](../../../hardening/runs/p2a1-baseline-1f8fc82/raw-files.sha256).

## The original planner turn

Thread `01a20ebc-325f-7101-a9b2-a9a0d000742d`, turn `01a20ebc-326f-72c3-9077-19da02752eed`, starts with zero prior thread tokens. Line 21 of its official rollout contains the direct, namespaced `gx_submit_product` with the GX-accepted call ID `call-fixture-6`. Line 22 holds the scoped `token_usage_record` for `fixture-6`, with 35 response tokens and 105 in each of the turn and thread cumulative counters. Line 23 is thread-goal bookkeeping. Line 24 records an exact empty cancellation of this call under the same thread and turn. Lines 25 and 26 record the user-visible abort marker and the official interrupted turn boundary. The last `token_count` notification is absent.

The pinned Codex 0.169.0 core persists each official usage-bearing completion under its response ID. In `core/src/state/session.rs`, `record_token_usage` accumulates the per-turn and per-thread values. In `core/src/session/turn.rs`, the public cumulative notification is emitted later. Source revision `d32063f96c2dff1dc96af839b3952abb6acfeda4` and archive and binary digests are documented in the [pinned usage contract](../../../v31/attribution/codex-usage-contract.json).

The reader at the failed revision produced 105 on the reviewer's complete mirrored history and no terminal proof on the planner's missing mirror. The diagnostic correction at `8b21844846e5d0122e8d234c772e67a9728cb4cc` binds the scoped per-response and cumulative records to the last committed submit, the exact cancellation with empty content, and the interrupted terminal boundary. The normal recovery path also checks receipt consistency and child costs before changing the ledger. [Original reader replay](original-reader.json)

With diagnostics enabled on a copy of the failed database and the original bytes, two fresh attachments produced one adjustment of −179,895 to turn event 116. Its 180,000-token estimate and original payload remained byte-for-byte identical. Confirmed run spending became 210 including the reviewer's 105. Replaying the original public-only policy retained the estimate and run spending of 180,105. Eleven additional controls exercised a missing or foreign receipt, contradictory totals, invalid cancellation data, extra model work, a missing abort, an additional unmeasured transport response and an unsettled child. Each retained the original estimate. Three controls checked the ordinarily mirrored turn with its original cancellation and with a foreign thread or nonempty result. The latter two yielded no exact proof. [13 database replays and 3 mirrored controls](controls.json)

## Named-build loopback experiment

Each arm staged the public runtime from its own Git revision. Both used the same official Codex binary, scientific fixture and per-mode spec with scoped rollout diagnostics explicitly enabled. Each HTTP response supplies 35 tokens. The totals below sum values recorded by that HTTP fixture. The ledger column was independently read from the corresponding GX database. [Named builds, task receipts and outcomes](brief-wire.json) · [Loopback namespace and exit status](network.json)

| Mode | Build | HTTP requests | HTTP request bytes | Fixture tokens | Confirmed GX tokens by turn |
| --- | --- | ---: | ---: | ---: | --- |
| Review | B2 `a691a401` | 13 | 559,559 | 455 | 245 + 210 |
| Review | B3 `7a6f521a` | 6 | 236,934 | 210 | 105 + 105 |
| Deterministic | B2 `a691a401` | 6 | 260,921 | 210 | 210 |
| Deterministic | B3 `7a6f521a` | 3 | 120,405 | 105 | 105 |

All four arms ran two CPU grid jobs and reached the same path-level scientific findings. Each terminal cold replay made zero further requests. The B3 late-source exercise retained the original measurements, incorporated the later source, recorded the rejection and made zero replay requests. The standalone repair entry point also exercised the same policy on B3. It issued seven responses, one recoverable schema correction and two provider-confirmed turns of 140 and 105. Its terminal replay issued zero requests. [Standalone repair](public-repair.json) · [Repair network receipt](repair-network.json)

The first isolated launcher imported GX across a user-namespace boundary and failed resource-profile validation before the official host or fixture received work. The recorded A/B used the normal controlled fixture launcher inside the isolated namespace and ran each named arm once. [Setup traceback](setup-failure.log) · [Setup exit status](setup-failure-network.json) · [A/B argv](invocation.json) · [In-namespace launcher](launch.py)

The archive contains the raw per-request fixture rows, specs, scoped official rollouts and SQLite backups for all four comparison arms and standalone repair. It also contains late-source request rows and spec. The interrupted-meter and recovery unit suites passed 28 tests. [Wire archive](wire.tar.gz) · [Wire member hashes](wire-files.sha256) · [Unit tests](meter-tests.log)

## Reproduce the saved failure against the corrected reader

Run from a checkout of `7a6f521a2dd58a6ac5240cf3383a58f954926884` or a descendant with the original failed-run archive. The replay uses local copies and creates no official model requests.

```sh
scratch=$(mktemp -d)
tar -xzf acceptance/hardening/runs/p2a1-baseline-1f8fc82/raw.tar.gz \
    -C "$scratch" --wildcards 'v31-regression/evidence/diagnostics/review-b3/*'
python -B acceptance/p2a1/brief_receipt.py \
    --source "$scratch/v31-regression/evidence/diagnostics/review-b3" \
    --output "$scratch/controls.json"
PYTHONPATH=gx-h:gx-h/tests python -B -m unittest -q \
    test_v31_interrupted_meter test_p0_meter_recovery
```
