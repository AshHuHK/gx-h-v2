# Matched Scientific Lead controls

The evaluator creates three isolated workspaces from the published `inputs/goal.json` and `inputs/initial.json`. The active commitments are a geometry investigation and a separate citation-index branch. Each citation-index Job really extracts the deposited `_citation.id` identifiers using `artifact-analysis@1` and an immutable input table derived from the pinned 2RE9 coordinates. Its submission is captured before reconciliation. The subsequent Lead sees the same Goal, input files, methods, prior state and limits, plus exactly one synthetic intervention. Fresh OS-visible working directories, Codex homes and native threads isolate the three Lead sessions. The runner uses the observed test's official-host environment and preflight.

The initial graph uses an explicitly labelled `fixture-*` binding and acknowledged setup delivery with no provider reservation. The real adoption service validates that setup and `JobService` admits the work. The three *subsequent* decisions go through authenticated Codex callbacks and the public `gx-p0 run` entry. `fixture-*` identities are excluded from live scoring. Numeric observations in the matched interventions remain synthetic.

From the product root, use two new destinations on a private filesystem. The `prepare` step records the input digests and implementation hashes before any paid turn. `seed` makes the initial graphs and captures all three starting states. The current default permits one paid native turn per workspace. The time limit is per native Run. This example runs the three sequentially.

```bash
work=$(mktemp -d)
python3 -B acceptance/v6/controls/run.py prepare --private-dir "$work/private" --review-dir "$work/reviewer"
python3 -B acceptance/v6/controls/run.py seed --private-dir "$work/private" --review-dir "$work/reviewer"
python3 -B acceptance/v6/controls/run.py live --private-dir "$work/private" --review-dir "$work/reviewer" --seconds 240
windows=( "$work"/reviewer/*/window.json )
python3 -B acceptance/v6/export.py bundle --mode controls \
  --window "${windows[0]}" --window "${windows[1]}" --window "${windows[2]}" --out "$work/export"
```

The custodian independently retrieves the bundle and its original source records, stores the `bundle_sha256` in a detached anchor outside the export, and compares the model's rationale and downstream action with `reviewer/rubric.md`. `verify.py` returns nonzero if a recorded case violates its criterion. `review.py` retains the per-case verifier result and independently recalculates each input measurement's coverage and tolerance for interpretation.

```bash
python3 -B acceptance/v6/verify.py "$work/export" --anchor "$custodian/controls.anchor.json"
python3 -B acceptance/v6/controls/review.py "$work/export" --anchor "$custodian/controls.anchor.json" --out "$work/numeric-review.json"
python3 -B acceptance/v6/controls/results.py "$work/reviewer" --out "$work/results.json"
python3 -B acceptance/v6/controls/meter.py --private-dir "$work/private" --review-dir "$work/reviewer" --out "$work/later-meter.json"
```

`meter.py` reads previously admitted turns using their official thread histories and invokes GX's ordinary maintenance. It issues no model turn. Cost figures separate confirmed provider readings, estimated bookings, remaining holds and USD. The original capture remains available after later settlement.

## First trial

| Input | Durable claim | New geometry Jobs | Outcome |
|---|---|---:|---|
| p17 | Limited support on A/B and A/C for the two supplied masks | 2 | Passed the case check |
| p42 | Mixed for A/B and A/C. A/C exceeds both tolerances at eligible coverage | 2 | Passed the case check |
| p63 | No change | 2 | Failed the neutral-event check |

The `gpt-6.2-astra` Lead at `xhigh` recognized the unchanged coordinate bytes in p63. It spent both available compute submissions to investigate a question that was already open. The frozen test requires preserving scientific work on this metadata-only event. All three pre-existing citation Jobs completed with the same identities, and all six new geometry Jobs completed. The three public cold resumes recorded the original nine Jobs.

This first trial had a four-call ceiling per Run. Three scored turns received a total of 130,952 provider-attributed tokens. The three interrupted follow-up turns each provisionally booked 65,536 tokens after passive reconciliation and grace. GX recorded 327,560 booked tokens and six native calls in total. The provider supplied no USD charge. All nine Jobs together measured 0.738 CPU seconds. Source and cost records are under `runs/first`. The separately held reviewer anchor is `controls-first.anchor.json`.

## Matched repeat on fe7ff69

The repeat used a frozen checkout, three fresh workspaces and one paid decision per workspace. The selected cases, 240-second cutoff, cost ceilings and reason for repeating previously exposed packets were recorded before model dispatch. Each Lead received the previous choice, its reconsideration conditions and the newly arrived evidence. [Preregistration](runs/repeat/preregistration.json)

| Input | Durable decision | New geometry Jobs | Provider-attributed tokens |
|---|---|---:|---:|
| p17 | Limited support on A/B and A/C for the two supplied masks | 2 | 64,222 |
| p42 | Mixed across A/B and A/C, with sensitivity on A/C | 2 | 64,059 |
| p63 | Resolve this event with the current scientific plan and claim intact | 0 | 27,910 |

The matched repeat used 156,191 confirmed tokens, with zero additional reservations or estimates and no provider-reported USD amount. All seven Jobs succeeded and consumed 0.484 measured CPU seconds. Each public cold resume found its original Jobs. The detached custodian anchor is `controls-repeat.anchor.json`. These results cover the three named regression cases. [Case and Job results](runs/repeat/results.json) · [Usage](runs/repeat/meter.json) · [Numeric review](runs/repeat/matched-numeric-review.json)

The original kit verifier rejects same-turn references to newly created claims. The shipped verifier checks their creation in a preceding tool callback of the same official decision turn, binds the callback to its scope and verifies the immutable artifact. The independent reviewer also examined the original records. [Exported repeat](runs/repeat/bundle/bundle.json)

## Physical overlap

In both paid batches the protected citation-index Job had been admitted at the initial snapshot and its identity and reservation survived the scientific decision and cold resume. The native runner finished these short Jobs before the scored model sessions started. [First-trial timing](runs/first/citation-job-lifecycle.json) · [Repeat timing](runs/repeat/citation-job-lifecycle.json)

A separate zero-token engineering experiment committed a scripted stop to the geometry branch while the citation Job's real tool process was still alive. The same PID and kernel start tick were present on both sides of the commit, with the runner recording a start at 02:09:56.702 UTC, the GX commit at 02:09:56.714 and a successful finish at 02:09:56.920. The public cold resume retained one Job and its result. This measures the scheduler property with a scripted decision. [OS, GX and cold-recovery receipt](runs/repeat/physical-overlap.json)

Run the Linux process-lifetime check against a checkout with its release Python environment installed. Both output destinations must be new.

```bash
checkout=$PWD
work=$(mktemp -d)
PATH="$checkout/.venv/bin:$PATH" "$checkout/.venv/bin/python" -B acceptance/v6/controls/overlap.py \
  --product "$checkout" --work "$work/state" --out "$work/receipt.json"
```
