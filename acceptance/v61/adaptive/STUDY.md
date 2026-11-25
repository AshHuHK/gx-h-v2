# Two-deposition coordinate study

The single preregistered study compares the asymmetric units of 2RE9 and 2O0O. Both report three copies of entity 1 in model 1. They were selected from metadata before study measurements, have different reported resolutions and share a publication. The investigator receives the question in `study-goal.md`, both source files, the grant and the method card. The grader's thresholds, known values, other arms and independent review code are outside the investigator's workspace. [Deposition metadata and source digests](data/provenance.json)

The released geometry method matches identical label position, residue and atom name across A/B/C in one coordinate file, then does a proper Horn rotation fit. It uses the hash-verified released fitter. It can measure shared Cα atoms, shared complete backbone atoms, or a subset with full occupancy and B factor up to 40 Å² for all four backbone atoms in all three chains. The method returns actual counts and measured geometry. An independent parser and NumPy SVD recompute the observations from each original file. These data describe deposited coordinates, and their coverage limits the scientific conclusion.

From the product root, use a fresh evidence directory outside the product tree. The preregistration lists the exact source hashes used for that experiment. The live runner refuses changes to those files. Numerical rehearsal runs six local subprocesses, makes no model requests and does not contribute to the one-study live denominator.

```bash
source .venv/bin/activate
evidence=$(mktemp -d)
study=acceptance/v61/adaptive/study.py
python -B "$study" preregister --out "$evidence/preregistration.json"
python -B "$study" numerics --registration "$evidence/preregistration.json" --out "$evidence/numerics.json"
binary="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
python -B "$study" loopback --registration "$evidence/preregistration.json" --binary "$binary" \
  --private-dir "$evidence/loopback-private" --out "$evidence/loopback"
```

The loopback uses the official release host with a local scripted Responses peer. The private state includes isolated native sessions, a GX store and actual CPU outputs. The live path uses the existing official OAuth login with the GX Clash settings. Running the following command starts provider-billed inference under the experiment grant of 10 Job submissions, at most 5 nodes in the first graph, 8 native calls, one million admission tokens and 300 CPU seconds. A native turn can exceed its reservation. If the first graph uses fewer than five nodes, GX preserves the unused headroom.

```bash
python -B "$study" live --registration "$evidence/preregistration.json" \
  --private-dir "$evidence/live-private" --out "$evidence/live" --seconds 480
python -B "$study" score --registration "$evidence/preregistration.json" --source-class LIVE_MODEL \
  --private-dir "$evidence/live-private" --out "$evidence/rescored.json"
```

Use the same private run directory for rescoring. Each new live invocation enrolls a distinct research project. The preregistered primary denominator is one, so retain the first enrollment even if time elapses, no decision is committed, or further computation would have been informative. Exit 2 records failure to establish the required environment or provider, 3 identifies an unsuccessful public start, 4 identifies driver/reopen failure, and 5 indicates that the completed observation window did not verify an adaptive work change. Read the individual records for scientific closure, physical cross-branch overlap and numerical agreement.

The scorer cross-checks the frozen brief, official call and provider identity, a cited Job artifact and its original digest, parent and child graph heads, an actual new Job and result or withdrawal, and post-restart IDs/costs. A changed claim or closure by itself earns no adaptive-work credit. The numerical oracle reports actual differences from the independent fit. The result includes model rationale so a separate reviewer can assess whether the change was scientifically justified. [Prespecified scientific rubric](study-rubric.json)

The frozen reference for this implementation includes the complete selected protein package, the public contract digest and the scorer. Direct CPU rehearsal returned six measurements and 18 independent fit comparisons. The largest absolute deviations were 1.55×10⁻¹⁵ Å and 8.53×10⁻¹⁴°. On the frozen package, the official loopback took three fresh scripted native turns, executed two real Jobs and closed. Its first returned coverage triggered a second graph version with a distinct Job. The frozen evidence includes request bodies seen by the local scripted peer, public CLI outputs and the GX cost report. [Registration](study-evidence/preregistration.json) · [Official loopback](study-evidence/loopback/result.json) · [Numerical rehearsal](study-evidence/numerics.json)

The previous V6 observed studies used 259,718 and 353,579 provider-confirmed tokens for three turns each. At those rates a three-to-five-turn research run would consume approximately 0.26–0.59 million tokens; actual usage is measured per turn and the run's admission limit is one million. The 25 blinded decisions and the comparison arms have separate project accounting. This estimate supplies no provider dollar price.
