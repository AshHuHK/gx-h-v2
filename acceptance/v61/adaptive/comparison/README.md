# Matched comparison of three research modes

Enroll one run per arm against the same deposited coordinate files, question, method implementation and cumulative resource limits. The program freezes the verifier, input and program digests before opening the first Run. Each arm gets its own workspace budget and store. The prospective sample size is one run per arm. Results describe that enrollment. A failed, incomplete or disobedient Lead stays in its assigned arm.

| Arm | Execution |
|---|---|
| Static | A fixed GX Workflow runs the published common Cα, common backbone and ordered backbone selections for both deposits. The static Engine schedules six actual detached Jobs, reads their observations and produces a scoped conclusion. Its first two submissions fit within the opening ceiling. |
| Single Lead | The regular Goal CLI assigns a single logical Scientific Lead to one branch. Each decision uses a fresh native thread. The scorer verifies one branch and absence of delegated decisions. |
| GX multibranch | The regular Goal CLI permits the Lead to open and staff branches. The scorer records actual branch count, delegation and overlapping CPU processes. Opening a branch confers no credit for delegation or physical overlap. |

The historical static `tl1a_geometry@3` was published for 2RE9. The matched static arm registers an acceptance-local two-deposition adaptation with its own workflow and portable method pin in a private installation. All three arms invoke the identical `deposited-chain-geometry@1` Job. A separate run of the unchanged historical recipe on its own accepted input exercises its original planning, CPU measurements, review Jobs, decisions and replay. Its result has a separate denominator. [Original recipe run](receipts/legacy-static.json)

## Run and reproduce

Use the V6.1 release environment, its official Codex binary and a fresh working directory outside the product. Each command resolves symlinks before checking the directory. An existing directory must be accessible only to its owner. This keeps the native session store and the live OAuth login link in the operator's private workspace. The model-shaped requests in a rehearsal terminate in a scripted local Responses server. The measurements, native tool calls, durable decisions and cold reopen use the normal product paths. The operator needs no OAuth login for rehearsal.

```bash
source .venv/bin/activate
runner=acceptance/v61/adaptive/comparison/run.py
binary="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
cohort=$(mktemp -d)
python -B "$runner" prepare --work "$cohort"
python -B "$runner" rehearse --work "$cohort" --binary "$binary" --seconds 90
python -B "$runner" score --work "$cohort"
```

For a separately funded live cohort, the same command accepts `live` in place of `rehearse`. Isolated official sessions use the existing OAuth login and verified Clash endpoints. The capture includes the actual model and effort. The receipts from each accepted callback and the outcomes of its actual Jobs establish which enrolled mode ran. The static arm uses no inference. Single Lead and GX each have an admission ceiling of eight turns and 1,000,000 tokens, ten Jobs, 300 CPU seconds and zero GPU. Actual usage of an admitted turn can exceed its reservation. On two historical V6 runs, three actual turns consumed 259,718 and 353,579 tokens. Three to five turns at these rates suggest around 0.26–0.59M per live Lead arm. Provider price and invoice are unknown.

```bash
paid_cohort=$(mktemp -d)
python -B "$runner" prepare --work "$paid_cohort"
python -B "$runner" live --work "$paid_cohort" --arm static
python -B "$runner" live --work "$paid_cohort" --arm single --seconds 900
python -B "$runner" live --work "$paid_cohort" --arm multi --seconds 900
```

One enrollment may be run at a time per cohort directory. Commands reject a second enrollment of the same arm. Use the original store and `score` to re-evaluate the same evidence. The recorded rehearsal pins the launcher at commit `063fdbf8096a037cc8679204b573973696174668`. Use that checkout when rescoring its original private cohort. Fresh enrollments pin the launcher with the external-workspace check. Live and loopback use separate cohort directories. The standalone adaptive study is a separate enrollment with a distinct prospective denominator.

## Metrics

- An evidence unit is a distinct deposit and atom mask with an actual successful Job, an immutable completion-manifest link and a digest-checked output. Biopython parses the original mmCIF and NumPy independently recomputes selection identities, coverage and three proper-rotation fits. Fit tolerances are 0.01° and 0.001 Å.
- Conclusion quality has nine prespecified units. There are six within-deposition chain-pair interpretations and three cross-deposition comparisons using only commonly eligible masks. Full coverage credits conclusions supported by evidence resolving all enrolled mask scopes, including measured exclusions. The thresholds and limitations are in the shared research rubric.
- Unsupported assertions are counted against the actual number of coded scientific assertions, including out-of-scope claims. An absence of claims yields a null fraction. Free prose remains pending until blind coding. The `--interpretation FILE` mapping is keyed by arm and original claim revision ID. Its value must enumerate each scientific assertion. Use the structured fields from a corresponding `claim_units`/statement example or an entry of the form `{"type":"other","quote":"…","rationale":"…","supported":false,"evidence_refs":[]}` for extra assertions. An affirmative extra assertion also requires at least one cited, independently verified output. Keep the original claim text and blind reviewer judgments.
- Each adaptive opportunity comes from the shared study verifier. It checks returned observations in the frozen native brief, the accepted decision, prior and new graph heads, and subsequently executed or withdrawn work. Report the number of verified changes divided by decisions with a prespecified stimulus. The fixed recipe makes no scientific decisions, giving zero opportunities.
- Resource amounts come from the GX ledger and each detached Job's real CPU measurements. Confirmed provider tokens, unresolved exposure, booked CPU and measured CPU remain separately visible. Per-evidence ratios divide by the count of unique verified outputs. Local-peer tokens are labeled separately from paid tokens. The dollar field remains null without an invoice. Human interventions count explicit scientific messages after the original Goal divided by one enrolled Run.

The native loopback's transparent scripted choices exercise the harness and scorer. Conclusions about comparative model performance or scientific autonomy require fresh model inference. [Frozen protocol](receipts/preregistration.json) · [Observed A/B/C metrics](receipts/results.json)

## Recorded rehearsal

| Measure | Static | Single Lead | GX multibranch |
|---|---:|---:|---:|
| Enrolled and completed runs | 1/1 | 1/1 | 1/1 |
| Independently reproduced observations / submitted Jobs | 6/6 | 6/6 | 6/6 |
| Full-scope conclusion units / possible | 9/9 | 9/9 | 9/9 |
| Unsupported assertions / assertions | 0/9 | 0/9 | 0/9 |
| Verified changes / decisions with a registered stimulus | 0/0 | 2/4 | 2/4 |
| Measured CPU seconds / verified evidence units | 0.550/6 | 0.554/6 | 0.528/6 |
| Scripted fixture tokens / verified evidence units | 0/6 | 805/6 | 805/6 |
| Provider-paid tokens / verified evidence units | 0/6 | 0/6 | 0/6 |
| Scientific intervention messages / enrolled runs | 0/1 | 0/1 | 0/1 |
| Scientific delegations | 0 | 0 | 0 |
| Overlapping cross-branch Job pairs | 0 | 0 | 0 |
| Provider USD | null | null | null |

The scientific snapshots report the matched masks, outcome, source, Job, digest and claim refs. All three pairs met the frozen agreement margins in both deposits on the commonly eligible masks. The ordered subset's limited coverage constrains that conclusion. Single Lead used one branch and the multibranch fixture opened two. None of the latter's detached Job process intervals overlapped during this enrollment. The fixed program and local peers used the public development data. Their equal scores do not establish equivalent autonomous research performance.

Cold resume preserved all three snapshots and the second complete scoring pass reproduced the first result byte for byte. In a separate scorer challenge, adding 1 Å to one fit dropped verified evidence from six to five, and six of nine claims then lacked full cited support. The original measurement and score were preserved. [Static snapshot](receipts/static/snapshot.json) · [Single Lead decision audit](receipts/single/evidence-to-graph.json) · [Multibranch decision audit](receipts/multi/evidence-to-graph.json)

At this registration's handoff, the separate live comparison cohort had no funded model allocation or enrollment. Its three assigned rows have status `NOT RUN`. The static actual CPU comparison above is available independently of live inference. The three live commands above enroll the arms and preserve the registered denominator without substituting rehearsals for fresh observations.
