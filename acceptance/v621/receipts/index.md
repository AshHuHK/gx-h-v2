# V6.2.1 acceptance

Candidate `83e8f34d2886a5c4cedc966ec4a7dd7b2addf691` · V6.2 baseline `321b3b9542e986e749b4eab08495b288d09ba4be`

Offline public engineering. Scripted replies have no provider token or fee receipt. Job and reuse counts refer to the linked individual runs; overlapping traces must not be added.

- Verified V6.2.1 checks: 1A, 1B, 2A, 2B, 2C, 3.1, 3.2, 3.3, 3.4, 3.5, 4.goal, 4.gate, 4.contract, 5.trace, 5.legacy-coordinate, 5.upgrade, 5.v6, 5.v62a, 5.v62b
- Entry to the zero-paid rehearsal: READY. Four-slot execution: PASS. Four-slot rating: PASS.
- Public live entry: BLOCKED. Pending: explicit public batch spending authorization, verified pre-dispatch billable limit for every Lead and module request.

| Case | Status | Observed | Evidence |
|---|---|---|---|
| 1A | PASS | supported/limited_support, opposed/contested, mixed/mixed, undetermined/insufficient | [supported.json](<claim-receipts/supported.json>), [opposed.json](<claim-receipts/opposed.json>), [mixed.json](<claim-receipts/mixed.json>), [different-endpoint.json](<claim-receipts/different-endpoint.json>), [command log](<commands/repairs.log>) |
| 1B | PASS | 49/164 (0.299); official level open (undetermined) | [metrics-trace.json](<metrics-trace.json>), [command log](<commands/repairs.log>) |
| 2A | PASS | SCHEMA_INVALID with zero settlements; two distinct matches settled; only the measured coordinate mask closed | [gap-mixed-refusal.json](<gap-mixed-refusal.json>), [gap-two-matches.json](<gap-two-matches.json>), [gap-acquired-analysis.json](<gap-acquired-analysis.json>), [command log](<commands/repairs.log>) |
| 2B | PASS | 49/164 (0.299); official level open (undetermined) | [metrics-trace.json](<metrics-trace.json>), [command log](<commands/repairs.log>) |
| 2C | PASS | One fresh Job, one reuse, zero additional Job or spend after recovery | [named-receipt.json](<named-receipt.json>), [command log](<commands/repairs.log>) |
| 3.1 | PASS | Completed captured command; see its output | [command log](<commands/scoring.log>) |
| 3.2 | PASS | A scoped successor ran one Job after assignment; repeated delivery reused its receipt | [assigned-job.json](<assigned-job.json>), [command log](<commands/repairs.log>) |
| 3.3 | PASS | Completed captured command; see its output | [command log](<commands/scoring.log>) |
| 3.4 | PASS | Both deep Leads measured 8.0; extra delegated tools: gx_cancel_module, gx_open_module | [catalog.json](<catalog.json>), [command log](<commands/catalog.log>) |
| 3.5 | PASS | Both deep Leads measured 8.0; extra delegated tools: gx_cancel_module, gx_open_module | [catalog.json](<catalog.json>), [command log](<commands/catalog.log>) |
| 4.goal | PASS | Goal version change and cold reopen preserved the run budget | [goal-trace.json](<goal-trace.json>), [command log](<commands/repairs.log>) |
| 4.gate | PASS | Fixed negative launched zero extra Jobs; a different reviewed gate admitted the follow-up | [gate-trace.json](<gate-trace.json>), [gate-trace.review.json](<gate-trace.review.json>), [command log](<commands/repairs.log>) |
| 4.contract | PASS | Completed captured command; see its output | [command log](<commands/repairs.log>) |
| 5.trace | PASS | Official Lead received 49/164 (0.299), staged and adopted open; wrong numerator and cross-Run original refused | [coordinate-publication.json](<coordinate-publication.json>), [command log](<commands/chain.log>) |
| 5.legacy-coordinate | PASS | One historical @1 CPU Job on each release; geometry.json SHA-256 and unclassified nature agree | [comparison.json](<legacy-coordinates/comparison.json>), [before.json](<legacy-coordinates/before.json>), [after.json](<legacy-coordinates/after.json>), [command log](<commands/legacy-coordinate.log>) |
| 5.upgrade | PASS | Old receipts replayed, pending cursor advanced, original business records and cursor restored | [before.json](<upgrade/before.json>), [check.json](<upgrade/check.json>), [restored.json](<upgrade/restored.json>), [command log](<compat/commands/upgrade.log.gz>) |
| 5.v6 | PASS | Completed captured command; see its output | [command log](<compat/commands/v6-engineering.log.gz>) |
| 5.v62a | PASS | Completed captured command; see its output | [command log](<compat/commands/v62a.log.gz>) |
| 5.v62b | PASS | Completed captured command; see its output | [command log](<compat/commands/b-four.log.gz>) |
| C.spending | PASS | Simulated Lead and module each overshot by 200 tokens with one settlement; paid admission remains blocked | [spend-preflight-probe.json](<spend-preflight-probe.json>), [command log](<commands/scoring.log>) |
| C.dry | PASS | Four local rehearsals; zero official provider enrollments | [registration.json](<../../v62c/receipts/registration.json>), [result.json](<../../v62c/receipts/result.json>), [command log](<../../v62c/receipts/output.log>) |
| C.parity | PASS | Four official openings, two matched specimen pairs, same methods and 2 Jobs/slot; only gx_open_module/gx_cancel_module differ | [capability-parity.json](<../../v62c/receipts/capability-parity.json>), [registration.json](<../../v62c/receipts/registration.json>), [result.json](<../../v62c/receipts/result.json>), [command log](<../../v62c/receipts/output.log>) |
| C.score | PASS | 4/4 scripted science judgments; 2 complete delegation chains; zero LIVE_MODEL credit | [scores.json](<../../v62c/receipts/scores.json>), [registration.json](<../../v62c/receipts/registration.json>), [result.json](<../../v62c/receipts/result.json>), [command log](<../../v62c/receipts/output.log>) |
| C.live | NOT_RUN | Public paid slots require explicit enrollment authorization, a frozen protocol and a verified final-request spending ceiling. |  |
| C.private | NOT_RUN | The separate custodian must commit a successor protocol and authorize private enrollment. The original outcome remains 0/2. |  |

### Counterexamples

- Publish limited support for the opposed prediction — [trace](<claim-receipts/opposed.json>)
- Close unrelated gaps with an authorized partial reading — [trace](<gap-mixed-refusal.json>)
- Publish 149/164 for the named 49/164 output — [trace](<metrics-trace.json>)

The original unseen protocol remains at 0/2. [Original cohort status](<../../v62b/study/not-run.json>) · [New public protocol](<../../v62c/protocol.json>)

Full commands, trace event IDs, input and manifest digests are in [index.json](index.json).

Reported skips within successful test groups

- inherited-discovery — test_gx_run_uses_sdk_model_then_returns_to_native_decision (test_dsh_activity.DSHActivityTest.test_gx_run_uses_sdk_model_then_returns_to_native_decision) ... skipped 'install runtime/dsh-sdk/.venv first'
- inherited-discovery — test_reported_model_mismatch_keeps_response_and_refuses_delivery (test_dsh_activity.DSHActivityTest.test_reported_model_mismatch_keeps_response_and_refuses_delivery) ... skipped 'install runtime/dsh-sdk/.venv first'
- inherited-discovery — test_close_cold_resume_and_observe_prior_evidence (test_dsh_runtime.DeepSeekRuntimeProcess.test_close_cold_resume_and_observe_prior_evidence) ... skipped 'install the optional DSH Python SDK/runtime wheel pair'
- inherited-discovery — test_close_while_recorded_response_is_pending (test_dsh_runtime.DeepSeekRuntimeProcess.test_close_while_recorded_response_is_pending) ... skipped 'install the optional DSH Python SDK/runtime wheel pair'
