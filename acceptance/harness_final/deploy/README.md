# Dual deployment and cold re-open

The full release began at `4b6aa93` on `science/multi-workflow`. The lean release began at `6ec604d` on `integration/science-assets`. Both trees were clean at entry. `baselines.json` records the two local and remote revisions, Python versions, source-tree hashes, contract digest, role projections, package registrations and optional database hashes from each installation.

## Reproduced baselines

| Source at entry | Historical suite | 92 harness tests |
|---|---:|---:|
| Full, as found | 121 passed | 86 passed, 6 distinct methods failed |
| Lean, as found | 121 passed | 92 passed |
| Full, detached, with verified archive | — | 92 passed |
| Lean, detached, with verified archive | — | 92 passed |
| Full, detached, archive absent | — | 90 passed, 2 failed |
| Lean, detached, archive absent | — | 90 passed, 2 failed |

The first full-site harness suite reported six failing subcases and three errors. Four of those failures belonged to one parameterized method. The local installed yeast reader contained an untracked compiled `archive.cpython-311.pyc`. Its SHA-256 was `a2d007acd6eca5bfa4f0082aa5967437b5bd9d02ee9ac92ad33441311fce9952`. This changed the installed package digest and suppressed multiple intended capabilities. A detached checkout with the same four mounted SQLite files passed. The cache was quarantined by digest outside the release and the original local Goal-to-CPU-Job regression passed. The package contents and pin were retained. A second intermediate observation is marked explicitly in `baseline-tests.json`. Running Python with `-B` avoids writing compiled Python into the immutable installed provider.

The clean unmounted baselines expose a different problem. One test required the archive dependency to appear ready, and one required a successful real archive Job. Both exercised an absent licensed data mount. `baseline-tests.json` identifies these methods, links each run to its raw and published log digest and preserves distinct method counts and durations. The snapshot members are installed site data. Clean-checkout tests can exercise the genuine missing-mount path, and a provisioned site can assert every registered byte digest and execute an actual read.

## Rerun

From each release root use its own Python and checkout. The historical cohort is the exact 121 recorded test IDs.

```bash
.venv/bin/python -B acceptance/harness_final/deploy/runtime_check.py .
.venv/bin/python -B acceptance/harness_final/deploy/site_tests.py . historical
.venv/bin/python -B acceptance/harness_final/deploy/site_tests.py . harness
.venv/bin/python -B acceptance/harness_final/deploy/site_tests.py . final
.venv/bin/python -B acceptance/harness_refactor/integration/cold_upgrade.py
```

For a recent Goal, supply the starting SHA for that installation. The in-place upgrade fixture creates an ordinary Goal with an explicit empty scope, closes it, starts the candidate in a fresh process at the same release path and checks the report hash, Run ID, stored scope and zero additional work.

```bash
.venv/bin/python -B acceptance/harness_refactor/integration/cold_upgrade.py --base 4b6aa93 --expected-policy gx.harness/2
```

For the lean installation substitute `6ec604d` as the base. For an existing closed research state run `cold_reopen.py` with the original release, original state directory and Run ID. It verifies the original Job artifact bytes, provider identities, decision and report hashes, original recipe digests, absence of added work and the original release/state binding. An absent external Job on a particular prior Run stays explicit.

```bash
.venv/bin/python -B acceptance/harness_final/deploy/cold_reopen.py . /absolute/path/to/original/state gxr_...
```
