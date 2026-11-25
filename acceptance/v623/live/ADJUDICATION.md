# Independent adjudication of a registered public comparison

The live and rehearsal series use separate enrollments. The PI or designated custodian registers one opportunity reader, one or two outcome readers, and an arbitrator for two-reader adjudication. With one outcome reader, an optional preregistered arbitrator can resolve uncertainty. Each named person or separately authorized model must attest independence from study design, software development and the assigned Run. The approved enrollment binds the canonical SHA-256 of the exact roster. Only public verification keys enter the operator workspace. The controller does not allocate a model or generate reviewer identities. A rehearsal seals mechanical evidence and reports independent science as pending.

The roster has schema `gx.live-review-roster/1`, the live `series`, `opportunity_reader`, a `reviewers` array, and optionally `arbitrator`. Each entry has a distinct `id`, an SSH Ed25519 `public_key` consisting of the type and base64 value, `kind` of `human` or `authorized_model`, and a true `independent_of_design_development_execution` declaration. This roster goes to the formal pilot enrollment before any enrolled Run. Review or replay commands below remain offline and incur no provider fees. The control plane retains all four original assignments.

## First reader

Run these commands from the release root, with `GX_STUDY` set to the registered batch and `GX_SLOT` to its two-digit slot. The operator retains the primary records and copies **only** the resulting `opportunity.json` to the opportunity reader. It contains the prior plan and first authenticated delivered value, or the evidence for absence. The reviewer's output identifies the packet by its canonical JSON SHA-256. The canonical encoding is sorted object keys, compact separators, UTF-8, and literal Unicode.

```bash
python=.venv/bin/python
judge=acceptance/v623/live/adjudication.py
"$python" -B "$judge" prepare --work "$GX_STUDY" --slot "$GX_SLOT"
```

The reader's JSON uses `schema: gx.live-review/1`, the registered `series`, their exact `reviewer` ID, `stage: opportunity`, `rubric_sha256` from the packet, `packet_sha256`, their `basis`, `measurement_lines` (such as `["M1"]`), and `state` (`DELIVERED`, `NO_ELIGIBLE_DELIVERY`, `NO_OPENING_COMPARISON`, or `NO_ASSIGNMENT`). Run the signing command on the independent reader's own machine and return the original UTF-8 JSON bytes and detached signature to the custodian.

```bash
ssh-keygen -Y sign -f "$REVIEWER_KEY" -n gx-live-scientific-review-v1 "$REVIEW_JSON"
"$python" -B "$judge" lock --work "$GX_STUDY" --slot "$GX_SLOT" --submission "$REVIEW_JSON" --signature "$REVIEW_JSON.sig"
"$python" -B "$judge" outcome --work "$GX_STUDY" --slot "$GX_SLOT"
```

The first reader's signed lock is required before the operator can emit the outcome packet. Replaying the command verifies the original registration, software pins and packet. A changed record cannot replace an earlier one.

## Outcome readers and final score

Each outcome reader receives only the frozen rubric and `outcome.json`. It contains pseudonymized prior plan, actual delivered counts, rationale, affected scope, declared intention, GX-enacted effect, and independent raw-material count. Numeric thresholds use original unreduced counts. Formal publications and archive excerpts remain available to the custodian for dispute resolution. Deliver packets individually using neutral filenames. Do not send the slot directory, live registration, accounting, raw session, other readers' reviews or prior slots. The observed number and author wording may still suggest the condition or delegation; this is disclosed in each packet.

Each signed reason review uses the same envelope with `stage: reason` and a `criteria` object keyed by all four items in `rubric.json`. For each item give a `mark` of `PASS`, `FAIL` or `UNCLEAR`, a written `basis`, and the exact `lines` supporting it. For example, the reason-and-scope judgment refers to `R` and `C`; actual enactment refers to `R` and `A`. Judge negations, terminology and alternative proposals in context. A model's typed disposition or a correct number alone cannot establish good reasoning.

```bash
ssh-keygen -Y sign -f "$REVIEWER_KEY" -n gx-live-scientific-review-v1 "$REVIEW_JSON"
"$python" -B "$judge" review --work "$GX_STUDY" --slot "$GX_SLOT" --submission "$REVIEW_JSON" --signature "$REVIEW_JSON.sig"
"$python" -B "$judge" score --work "$GX_STUDY" --slot "$GX_SLOT"
"$python" -B "$judge" summary --work "$GX_STUDY"
```

After both original reviewers submit, agreement is the number of identical item marks out of four. A single review has no agreement statistic. A signed arbitration uses `stage: resolution`, cites the canonical digest of every original signed review envelope in `originals` keyed by reviewer ID, and gives the same four reasons and marks. The custodian supplies both original comments to the preregistered arbitrator. Submit it with `resolve` in place of `review`. Unresolved judgments remain pending and both original reviews are retained. The machine receipts cap credit where the cited endpoint or enacted effect fails verification. A budget stop, unread stop, absence of delegation or missing inspection remains attached to the original assigned slot; signed judgments and actual provider costs are reported separately.
