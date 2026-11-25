# Reviewing a V6 decision window

The capture command opens the project database in SQLite read-only mode. It reads one consistent snapshot of the selected Run's GX records and budget, then verifies the immutable artifact bytes against that snapshot. Keep this private observer directory outside the Scientific Lead's workspace. It contains actual prompts, traces, file locations and research material.

```bash
python -B acceptance/v6/export.py capture --db "$STATE/gx.db" --run "$RID" --out "$REVIEW/before"
```

Capture before the scored decision, again once its work or stop settles, then a third time after a separate public `gx-p0 resume` process. The live runners generate the three captures, record the command, PID, timestamps, exit status and stdout of that process, locate its original per-thread Codex rollout and write a window descriptor. A bounded open-run resume may return 75 with `driver=time_limit`. The exporter checks the identities and accounting in both database snapshots.

```bash
python -B acceptance/v6/export.py bundle --mode observed --window "$REVIEW/window.json" --out "$OUT"
python -B acceptance/v6/export.py bundle --mode controls --window "$W1" --window "$W2" --window "$W3" --out "$OUT3"
python -B acceptance/v6/verify.py "$OUT" --anchor "$REVIEWER_ANCHOR"
```

The descriptor contains private paths to `before`, `settled`, `reopened`, `rollout`, `resume_receipt` and the selected `decision_ref`. In controls it also records the published `packet_id` and the evaluator's mapping of native evidence IDs to that packet. Source locators can select a measured JSON object by its RFC 6901 pointer for an observed window. The exporter checks that the cited value belongs to the original Job and that the bound native turn actually saw it. Each window must bracket exactly one accepted decision. Reads of a later source do not establish that it was available when the original decision was made.

Successful projection records `bundle.json`, content-addressed original objects and `projection-verdict.json`. Exit 0 indicates consistency. Exit 2 retains an authentic but unsuccessful experimental outcome, including per-run reasons. Exit 1 indicates the source window could not be projected. A separate reviewer computes the SHA-256 of the bytes of `bundle.json`, stores the detached JSON anchor outside the export and checks the original native turns, Job records and scientific rationale. Snapshot directories must be retained for independent provenance checks. A detached hash by itself does not identify a provider or certify a scientific conclusion.

## Checker versions

The Agent Kit checker has SHA-256 `ed873c209d4017f9de6d1cb5770f9dbfa80f57a183c4637b6612a259e4dff0f8`. The product checker also verifies the complete tree of referenced object hashes and accepts a newly returned scientific claim or re-evaluation cited later in the same native turn. These new references are recorded in `turn.same_turn_products`. For each, the product verifies the producing tool, thread, turn, call, request digest, earlier rollout position, actual native output and stored artifact. A nested GX call must appear in the official host events and its returned data in the model-facing output. The scored decision must still cite the presented observations. A claim reference generated during the turn is absent from the pre-turn list.

Both checkers pass the independent engineering inventory and stop witness. Check each live export with both versions. The kit rejects decisions citing legitimate same-turn products when that reference was absent from the pre-turn brief. The product retains such calls verbatim. The comparison is auditable by inspecting the exact source and the corresponding detached anchors.

## Replaying the engineering witness

```bash
python -B -m unittest discover -s acceptance/v6 -p 'test_*.py'
python -B acceptance/v6/verify.py acceptance/v6/engineering/loopback-bundle \
  --anchor acceptance/v6/engineering/loopback-review/detached-anchor.json
"$GX_PYTHON" -B acceptance/phase2a/regression/loopback.py \
  --clash "$GX_CLASH" --receipt "$REVIEW/net.json" -- \
  "$GX_PYTHON" -B acceptance/v6/engineering/loopback_export.py \
  --out "$REVIEW/engineering" --binary "$GX_CODEX_BINARY"
```

The engineering run uses an official 0.169.0 host talking to an isolated local scripted peer. It performs a real CPU coordinate inventory and verifies cold restart. Its bundle and independently computed inventory report are in `engineering/loopback-bundle/` and `engineering/loopback-review/`. Its provider and costs remain attributed to the controlled experiment.
