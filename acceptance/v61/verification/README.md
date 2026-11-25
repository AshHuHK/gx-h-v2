# V6.1 evidence

The engineering candidate is `48af72c3861299350047dd3bae25acfe6a0bc1bd`. The [version manifest](revisions.json) records the Git parents, changed paths, and core and contract tree IDs for the CSV-only commit. The [release report](../../../V61_ACCEPTANCE.md) links the engineering tests, paid historical migrations, scripted trials and prospective scientific enrollment.

- `baseline/` contains the independent recheck of the released V6 environment.
- `first-candidate/` preserves the original regression results for `063fdbf`, including the failed historical operator opening and concurrent reactor callback.
- `final-candidate/` contains the independent inventory, named V6 and inherited test results, actual observed-project recovery and cold verifications.
- `runs/` contains the three complete runner directories. Each includes the summary, reproduction information, command and environment records, file manifests and compressed raw outputs. Each run ID is its directory name.
- `comparison/` contains the independent interpretation of fixture decisions and numeric checks. The separately enrolled static comparison arm has a [dedicated receipt](../comparison-live/enrollment.json).
- The [portable release receipt](portable-release.json) names every original installer record. The [archive](portable-records.tar.gz) preserves those paths. Linux clean-install checks include the official executable, generated schema, pinned plugins, CSV Goal recovery and the actual CPU methods. The [verification program](portable_release.py) accepts explicit source, interpreter, Node directory, official archive and output paths.

Run from the product root to validate the archived records:

```bash
for id in candidate-48af72c38-01-v6 candidate-48af72c38-01-v61 candidate-48af72c38-01-reg; do
  bin/gx-acceptance verify "$id" --runs-dir acceptance/v61/verification/runs
done
```

REG01's four environment-dependent skips come from the optional DeepSeek backend. The two `DSHActivityTest` cases require its dedicated SDK environment. The two `DeepSeekRuntimeProcess` cases require its Python SDK and runtime wheel pair. The final report and raw logs preserve the selected release profile.
