# Public Goal decision engineering trial

Four Goal runs exercise the official local Codex App Server, the GX JobService and the real deposited-coordinate method. A loopback service scripts the model replies. Each decision reads the delivered brief and results returned by granted GX tools. The first three scientific records are the released 2RE9 and 2O0O mmCIFs and their public workflow card.

| Slot | First investigation | Follow-up checked in the Job ledger |
| --- | --- | --- |
| quick | Direct 2O0O measurement | One Job, zero module reports or wakes |
| medium | A scoped workflow survey reads the public specification, selects the applicable method and cites the exclusion | The original 2O0O four-atom backbone measurement |
| change | The inventory worker measures the ordered four-atom backbone on 2RE9 | Close the provisional branch and commission shared C-alpha coverage on 2O0O |
| maintain | The inventory worker measures shared C-alpha coverage on 2RE9 | Keep the original branch and commission its proposed shared four-atom backbone on 2O0O |

The two deep slots share the exact grant, materials, goal, budget, model configuration and Lead script. Only the module worker's choice of pilot atom selection differs. The Lead initially records a provisional four-atom backbone route and schedules zero follow-up Jobs. It applies the preset 60 percent coverage cutoff to the counts in the first *delivered* module report. The scorer checks the original branch, the accepted later revision, the actual Job, its immutable source and output, and an independent numerical reproduction from the input mmCIF. Both arms close with a claim limited to the deposited coordinates. The legacy method output declaration does not identify a scientific nature, so the enriched claim is staged conservatively.

From the release root, choose a fresh work directory and use the inherited isolated, reject-only Clash launcher. The launcher needs the deployed Clash executable, the locked official Codex binary, and the release Python environment with Biopython and NumPy.

```bash
out=$(mktemp -d)
clash=../../../clash/clash.new
.venv/bin/python -B acceptance/phase2a/regression/loopback.py \
  --clash "$clash" --receipt "$out/network.json" -- \
  .venv/bin/python -B acceptance/v62b/engineering/run.py --work "$out/public"
.venv/bin/python -B acceptance/v62b/engineering/pack_public.py \
  --work "$out/public" --network "$out/network.json" --output "$out/public-evidence.tar.gz"
```

The run writes `result.json` and a per-slot `summary.json`. Each slot retains the complete transmitted brief, ordered Responses requests, exact CLI commands, official rollouts, results and a frozen GX database. Summary rows report separate Lead and module input, cached and output fixture tokens, delivered brief bytes, module wake events, Job resources, delegation latency and elapsed time. Per-brief tokenizer measurements and genuine provider usage are unavailable. The two additional deliveries of the captured report callback exercise the cold receiver's replay path after the scientific work has completed. A fresh offline CLI process verifies identical reports, graph versions, claim maturity, Jobs, costs and native requests.

The shareable archive contains per-case selected official rollout entries, the stored Goal graph and decisions, numerical Job results, execution manifests, bounded claims, publication and wake receipts, and the loopback network record. Every archived member is listed in `SHA256SUMS`. Numerical input bytes remain available at the pinned paths in the V6.1 study.

These are four publicly scripted engineering slots. Independent true-model cases, their funding, denominator and scoring records are maintained in the sibling study directory.
