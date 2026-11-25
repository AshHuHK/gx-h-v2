# Reopen a released V6.2 study with V6.2.1

The baseline is the published commit `321b3b9542e986e749b4eab08495b288d09ba4be`. Its original public deep/change script starts a Goal through the official local Codex App Server, runs two CPU coordinate measurements, publishes a scoped report, then closes the Run with an accepted claim. A second deep Goal registers a small operator observation and remains active for a later measurement. These public fixtures require zero provider inference.

The probe preserves the original conclusions, grading records, tool and native receipts, plugin and contract pins, Job admission/completion digests, artifact bytes, budgets and SQLite state. Two candidate CLI processes reopen the same paths. The receiver replays the exact captured module report twice. A second observation advances the old running Goal's maturity cursor. A fresh Goal on the same candidate selects the repaired policy. The final phase restores the complete released state and original code and verifies both the old conclusion and original cursor.

With the locked dependencies installed, run the following from a clean candidate release. `GX_BASE` selects the local V6.2 installation containing the locked Cordis packages and Codex executable. `GX_CLASH` must point to the local Clash binary used by the reject-only network fixture.

```bash
scratch=$(mktemp -d)
export GX_BASE="$(realpath ../product-v6.2-release)"
export GX_CLASH="$(realpath ../../../clash/clash.new)"
bash acceptance/v621/compat/replay.sh "$scratch/upgrade" "$(git rev-parse HEAD)"
```

`before.json` contains the original Run, assessed claim, report, pins, manifest SHA-256s and metering. `check.json` records the candidate reopens and second observation. `restored.json` records the genuine rollback. Each fixture has its own local run IDs and authoritative state. The original B public scientific report, protocol and scores are compared by content digest even when the candidate changes its implementation.

The original A6 gate expects an unsatisfied condition to keep waiting. The candidate's new evidence policy permits a terminal false condition. To replay the original assertions with fresh Goals, run `python -B acceptance/v621/compat/legacy_a6.py --work /fresh/external/path` in the same offline test environment. Only the admission documents receive the explicit `gx.evidence/6.2` selection. The receipt reads the stored policy from each of the three Run databases.
