# Independent scientific controls

Four offline Goal runs exercised instrument ingestion, claim adoption, a real local Job, publication and cold replay. The native communication was scripted. The observers used public inputs and assessed the actual GX records. The paired true-model protocol recorded zero admissions across two planned slots. [Recorded outcomes](results.json)

| Control | Observed outcome |
| --- | --- |
| Two instruments measured separate aliquots of one field sample | Both readings supported the registered threshold. The common bottle and field source kept the claim at `tested`. Reopening the store reproduced both claim revisions and created zero extra assessments. |
| A module finished a local calculation and later published it | The Run claim acquired the result reference after release. GX recorded the mismatch between the coordinate calculation and the registered thermometer measurement. |
| An unreleased instrument reading predated registration | A reading on the registered population prevented prospective status. A reading on a different population left the claim preregistered. Both registrations excluded the hidden artifact reference. |
| A known measurement was republished through a renamed data descriptor | The linked source made the outcome retrospective. A separate population with its own genuine adverse reading reached `tested` and `opposed`. |
| Two independent instrument receipts encoded identical numerical bytes | The observation CLI preserved the two sources and replayed each receipt exactly. GX labelled replication ambiguous when only the two identical readings were available. A previously attested measurement on an unrelated population left a fresh prospective test eligible. |
| A later report exposed a different statistic from the same source file | The scorer recognized the first delivery of the new statistic with the independent reader's explanation. An exact redelivery of the same finding was rejected. |

The scripted paired replay observed 49 of 164 positions in one public inventory and 164 of 164 in the other. The follow-up Jobs used `common_ca` and `common_backbone`, respectively. The recorded hashes identify the delivered briefs, native transcripts, reports and resulting Job outputs. [Paired engineering evidence](results.json)

## Replay

Run from the installed release with a fresh state directory. The first command produces the four Goal states and their measurement records. The second exercises the numerical rules, source review, rounds, output binding, brief delivery and paired scoring.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B acceptance/v62b/receipts/scientific-controls/replay.py --work /tmp/gx-v62b-independent-controls
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:gx-h:acceptance/v62b/study .venv/bin/python -B -m unittest acceptance.v62b.test_rounds tests.test_v62_maturity tests.test_v62b_goal_brief tests.test_v62_sources tests.test_v62_outputs test_pair
```

The public paired study validator can reconstruct the two behavioral records from the engineering runner's original states. Its command and the private study's separate denominator are documented in the [paired study protocol](../../study/README.md).
