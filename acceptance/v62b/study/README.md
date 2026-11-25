# V6.2B matched decision study

V62B-UNSEEN-PAIR-01 reserves two Goal runs. One tests a change in subsequent work after a material inventory gap. The other tests a justified decision to continue after a neutral inventory result. Both use the same initial question, source pool, tools, research effort and budget. The case key is held outside the execution environment. This pair measures preliminary behavior. [Prospective protocol](protocol.json)

| Series | Planned | Evidence available with this checkout |
| --- | ---: | --- |
| New, independently held gap case | 1 | NOT_RUN |
| New, independently held neutral case | 1 | NOT_RUN |
| Historical V6.1 matched controls | 25 | Original denominator and exposure history preserved. [Historical registration](../../v61/adaptive/controls_receipts/registered-design.json) |
| Public 2RE9 and 2O0O development material | Separate | Reproducible deposited-coordinate regression. [Original study and scorer](../../v61/adaptive/STUDY.md) |
| Public V6.2B scripted pair | Separate | Official-host engineering rehearsal. [Reproduction command](../engineering/run.py) |

The two private commitments, an independent custody receipt and any approved admission limits must come from the custodian. Their values are absent from this checkout. The public protocol and the two reserved slots can be replayed without a provider, login, or private inputs.

```bash
python -B acceptance/v62b/study/pair.py status
python -B -m unittest discover -s acceptance/v62b/study -p test_pair.py -v
python -B acceptance/v62b/study/public_check.py --work "$PUBLIC_ENGINEERING_WORK"
```

The official 0.169.0 catalog receipt for gpt-6.2-astra includes max and ultra. Its ordinary inference path passes max through directly. Ultra is a native multi-agent preset and may map to the catalog's xhigh setting. This study registers max for the Lead and every module decision root. The launcher checks the installed binary, reruns the official catalog preflight with Clash, and the verifier checks the model, provider and effort in each original Codex turn. Provider tariffs or unreported cached-token components remain unknown. [Pinned host audit](../../../audit/OAUTH_EFFORT.md)

## Independent preparation

Run the custodian and reviewer procedures in their separately permissioned evaluation storage. The developer, Lead and module workers receive no answer-key access. Independently archive the returned registration digest before starting either slot. A receipt records the custodian's statement of historical non-exposure. Directory placement by itself cannot establish that history. The program also rejects the recorded historical packet digests, including the tracked h81 and h92 controls when renamed or wrapped.

The inputs mount contains one `goal.md`, one GX `grant.json`, its raw source files and `cases/01.json` and `cases/02.json`. Both Goals receive exactly the same question and all the same starting files. The common question must state the scientific task and intended initial course, tell the Lead a raw sample or stratum assignment will arrive after the opening decision, and allow it to select its own investigation. Give the raw sources neutral names. The grant selects deep research, the pinned installed methods and modules, a turn reservation, and explicit equal workspace and Run limits. Pin every input file by SHA-256.

Each case document contains a distinct opaque `version` and an `intervention` object accepted by GX as `{"kind":"supplement","text":"…","source_ref":"…"}`. Supply only a raw assignment from the common source pool. The custodian checks that it discloses neither an inventory finding nor the answer. An already completed finding supplied in the assignment compromises the series.

The separate answer mount contains `01.json` and `02.json`. Register one `condition` of `change` and one of `maintain`, with descriptions in `acceptable_actions`, `scope_rule`, and `insufficient_support_rule`. Each answer's `eligible_measurements` lists permissible source and method selectors. A Job selector identifies the immutable original input SHA, capability version, allocated parameter values, exact output name and observation or analysis nature. A raw source-read selector identifies its source SHA, tool and segment locator. Distinct outputs from one Job retain their own nature and file digest. Only the reviewer receives these keys.

```json
{
  "kind": "job",
  "capability": "<pinned-method>@<version>",
  "parameters": {"<scope-parameter>": "<assigned value>"},
  "input_sha256": "<original raw source digest>",
  "output_name": "<exact published output filename>",
  "nature": "analysis"
}
```

The custody JSON records the series, a privately fixed order of the two opaque slots, and the actual reviewer identities. Supply `custodian`, `opportunity_reader`, `adjudicator`, `order`, `developer_access=false`, `unexposed=true` and `raw_assignment_contains_no_result_or_answer=true`. List additional known prior public case or key commitments in `prior_exposure_sha256`. An optional funding receipt identifies the same `series`, the externally authorized `reference`, and `caps` in GX units. The sum of both per-Run limits must fit each authorized unit. Omit funding to register an unfunded pair. Freeze any funded variant before enrollment.

```bash
pair=acceptance/v62b/study/pair.py
python -B "$pair" register --work "$COHORT" --inputs "$EXECUTION_INPUTS" \
  --keys "$REVIEW_KEYS" --custody "$CUSTODY_RECEIPT" --funding "$AUTHORIZED_LIMITS"
```

Registration starts two fresh Goals through the regular offline CLI. It checks that both stores contain zero native turns, Jobs and decisions, and records each exact opening brief hash, tool-spec hash, common source pins and initial limits. Compare the two normalized opening digests to check that the task, cards and sources match despite run-scoped identifiers. It freezes the product, prompt, scorer and selected extension bytes with the checkout. Use an A-accepted integrated checkout. The runtime eventually produces later frozen briefs from actual reports, and each delivered byte sequence is retained with its native and GX receipts.

## Execution and independent review

Only the custodian runs the next command in the funded environment. The runner uses the existing official OAuth setup and a fresh native home per slot. It queues the registered raw assignment after the first committed Lead decision through `gx-p0 message`, then allows the normal Goal driver to continue under its resource owners. Each slot has a single admission and a fixed 480-second observation window. Preserve any incomplete run, ambiguous admission or timeout under its original slot. The second admission requires independent assessment of the first. An unsupported claim of support stops the batch.

```bash
python -B "$pair" run --work "$COHORT" --slot "$FIRST" --inputs "$EXECUTION_INPUTS" \
  --registration-sha "$REGISTERED_SHA"
python -B "$pair" briefs --work "$COHORT" --slot "$FIRST" --registration-sha "$REGISTERED_SHA"
```

The opportunity reader receives `briefs.json` containing the actual delivered scientific content with current actions withheld. Provide `reviewer`, the returned `packet_sha256`, and an ordered `windows` list covering every delivered Lead brief, including a turn with no accepted decision. Each entry contains `window`, `reason`, and either `report_ref=null` or the delivered report reference plus `quote` with exact decision-relevant text. If an earlier finding came from the same result bytes, supply a `comparison` with its exact `earlier_text`, a scientific `difference`, and a judgment that `earlier_contained_endpoint` is false. Exact repetitions cannot earn another opportunity. A new statistic from the same file can.

```bash
python -B "$pair" lock --work "$COHORT" --slot "$FIRST" --registration-sha "$REGISTERED_SHA" \
  --review "$OPPORTUNITY_REVIEW"
```

Locking seals the independent opportunity judgments and the original evidence in `dossier.json`. The adjudicator receives this dossier, the verified source bytes in the private GX artifact store and the frozen key. The dossier contains exact delivered briefs, native transcripts by digest, report release and arrival, cited findings, graph versions, verified Job outputs, automatic dispatch, costs and elapsed time. A scoped action must follow the delivered raw assignment and precede module publication, and its source, method, sample scope and named output must satisfy the precommitted selector. The adjudicator compares the actual source values to the report and model reasoning and assesses scientific scope. Exact text alone cannot establish a scientific judgment.

Provide `reviewer`, the returned `dossier_sha256`, and one `windows` entry for each released window. Each identifies `window`, `reason`, `unsupported_support` and a `disposition` from CHANGE, MAINTAIN, MISSED, AUTOMATIC_ONLY, UNRESOLVED or NO_OPPORTUNITY. A positive judgment also requires `scope_appropriate`, an exact model `rationale_excerpt` and an `accepted_action` from the frozen key. Record an unlisted but acceptable action as UNLISTED with its `alternative_reason`. When a maintained course commissions a new Job, give its `prior_plan_excerpt` from before the intervention. Supply `novel_endpoint_confirmed` when the opportunity reader compared an earlier finding on the same immutable result. Retain adverse and out-of-scope conclusions.

```bash
python -B "$pair" score --work "$COHORT" --slot "$FIRST" --registration-sha "$REGISTERED_SHA" \
  --key "$REVIEW_KEYS/$FIRST.json" --review "$OUTCOME_REVIEW"
python -B "$pair" run --work "$COHORT" --slot "$SECOND" --inputs "$EXECUTION_INPUTS" \
  --registration-sha "$REGISTERED_SHA" --previous-score-sha "$FIRST_REVIEWED_SCORE_SHA"
```

Apply the same two review steps to the second slot. The independent reviewer archives each returned score hash. To reproduce the public-safe summary, supply a JSON file mapping the reviewed opaque slots to those retained hashes. The verifier reopens the original GX state and official transcripts. A manually placed score file and a local scripted-provider transcript cannot produce a verified scientific pass. Keep the registration digest and reviewer commitments outside the execution workspace under independent custody.

```bash
python -B "$pair" status --work "$COHORT" --registration-sha "$REGISTERED_SHA" \
  --score-anchors "$INDEPENDENT_SCORE_DIGESTS"
```

Report the first qualified opportunity for each assigned case with the separate strict work-change endpoint, failure denominator, automated actions and reasoned maintenance. The original V6.1 study's numerical scorer applies to its deposited-coordinate inputs. Its recorded scores remain in their original series. Per-slot accounting distinguishes directly attributed Lead and delegated input/output/cache tokens, measured CPU, actual Jobs, reservations, unmeasured usage, brief bytes, wake count, elapsed seconds and available provider cost.
