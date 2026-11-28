# Independent catalog holdout

An agent outside the catalog implementation selected four installed research method families on the original full deployment. The keeper wrote one natural Chinese and one natural English question per family, one archival reference question and one genuinely unsupported question. The keeper sent the SHA-256, counts and scoring rule to the implementation and integration owners before receiving the candidate. The questions were first scored on candidate `8a5fe74`. The frozen file is `sealed.json`, SHA-256 `d4be93a938559e281cc022b55d47c9eeae11a8117ec28e7a5f83514f322b6407`.

Each question made one `gx_query_catalog` call through a newly admitted ordinary Goal's Lead identity and native tool callback. The query requested five results over all item types. For a /3 Goal with its PI scope still pending, the first callback also returns the empty method-restriction list corresponding to this same frozen Goal. The scoring rule required an installed, available or selectable named acceptable method. Reference cards and unrelated results occupy rank positions. A true zero requires a catalog total of zero. These probes measure retrieval. A returned item does not establish scientific execution or an available external database.

| Tested source | Chinese method hit at 5 | English method hit at 5 | Both languages hit | Historical status correct | True zero |
|---|---:|---:|---:|---:|---:|
| Full baseline 4b6aa93 | 1/4 | 4/4 | 1/4 | 1/1 | 1/1 |
| Lean baseline 6ec604d | 1/4 | 4/4 | 1/4 | 1/1 | 1/1 |
| First frozen catalog 8a5fe74 | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |
| Full field product 362157a | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |
| Lean field product e9e4183 | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |
| Full legacy-compatibility release fd1aa75 | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |
| Lean legacy-compatibility release 8759016 | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |
| Full late-PI follow-up 6077e26 | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |
| Lean late-PI follow-up ccd0513 | 4/4 | 4/4 | 4/4 | 1/1 | 1/1 |

The JSON receipt from each source records all returned references with their positions and deployment facts. The candidate and baseline receipts retain the first observations. Repeats use the original frozen questions and count as regressions. Receipt commit IDs include acceptance-only follow-ups where the installed product tree matches the listed product. The category, provider, named output and activation knobs were left unset. The research turn used controlled callbacks. The field studies supply separate evidence of actual model behavior.

Run from the release root using the site's own Python.

```bash
.venv/bin/python -B acceptance/harness_final/holdout/evaluate.py . acceptance/harness_final/holdout/sealed.json /tmp/catalog-holdout.json
```
