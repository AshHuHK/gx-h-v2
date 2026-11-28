# Cross-topic provenance and route reinterpretation

Two deposited archive records under an ordinary `bin/gx-p0 research` Goal exercise the unchanged native tools. The recorded researchers and their decisions are controlled callbacks. No provider calls are involved.

## Results

| Test | Full 4b6aa93 | Slim 6ec604d | New Goal |
| --- | --- | --- | --- |
| Two authorized parents in different topics | Second parent refused | Second parent refused | Recipient receives both original IDs and source hashes. A restricted recipient receives no summary, private ID or wake. Narrowed author grants block a cached publication response. |
| Forward one parent into another topic | Parent refused | Parent refused | A late authorized subscriber receives the original question and a single root. After its author retracts it, the subscriber receives a withdrawal. |
| Reopen a converged route using old, previously unused material | Reopen refused | Reopen refused | Revision 3 reopens the same intent. The decision cites the newly considered original and records the revision, citation and decision it changed. |

The first two failures on each baseline were `SCOPE_DENIED`, and the route failure was `SCHEMA_INVALID`. The raw baseline tracebacks retain the original exceptions; checkout directory prefixes were removed. `green.log` and `compatibility.log` contain the candidate results, including an existing cold recovery test with an outstanding CPU Job.

The synthesis fixture closes its first database and native transport, resumes the same leases, verifies the durable cursor, and then corrects a parent. A subscriber to the synthesized interpretation receives only the withdrawal of the item it previously read. An independent reviewer and a researcher lacking a parent's materials cannot learn the private items. Inspect `synthesis-receipt.json` for the original submissions, the authorized reader's entry and the post-restart withdrawal.

The route fixture freezes a context containing both records. Two routes in one Lead decision use different records. A later decision on one route cites the other record and explains how the sampling window changes its earlier conclusion. The old graph version, the other route and the Job count remain intact. `reopen-receipt.json` shows the stored graphs and basis. This revision records reconsideration of previously visible evidence; it creates no new observation.

## Reproduce

Run from either product root after its regular Python installation.

```sh
export GX_CODEX_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=gx-h:.:gx-h/tests:acceptance/v62a
.venv/bin/python acceptance/harness_final/topics/test_scoped_lineage.py -v
.venv/bin/python -m unittest -v acceptance.harness_refactor.routes.test_routes acceptance.harness_refactor.topics.test_topic_handoff
```

Set `GX_TOPIC_SYNTHESIS_RECEIPT` and `GX_ROUTE_REINTERPRETATION_RECEIPT` to writable paths to save fresh Run-specific JSON. The inline cases cover public archive material with different scope grants. The real-model field studies have separate evidence and scores.
