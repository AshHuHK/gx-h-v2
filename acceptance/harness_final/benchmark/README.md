# Final harness comparison

The short field reuses the earlier archive question and the late publisher reply. The long field asks whether two claims about May 2023 river discharge can be published. It uses the original USGS daily records from the Potomac near Washington and the Northwest Branch Anacostia River near Hyattsville, plus a separately delivered USGS station inventory. Daily values and site metadata have different producers and retrieval endpoints. The independent Decimal calculation multiplies archived daily means by the duration of each day. Neither the numerical reference nor the rubric is available through a participant tool. [USGS daily values](https://waterservices.usgs.gov/nwis/dv/?format=json&sites=01646500,01651000&parameterCd=00060&startDT=2023-05-01&endDT=2023-05-31&statCd=00003&siteStatus=all) · [USGS station inventory](https://waterservices.usgs.gov/nwis/site/?format=rdb&sites=01646500,01651000&siteOutput=expanded&siteStatus=all)

| Field | Frozen comparison | Observable outcome |
|---|---|---|
| Short archive audit | First revised candidate compared with the earlier question at `5904a4f`. Prior research used zero external Jobs. | Successful first close divided by first close opportunities, refusals with their returned fields, official turns, elapsed time and seven scientific marks. |
| Long water bulletin | Stock Codex with native subagents and resume, GX `4b6aa93`, frozen GX candidate. All have the same two versioned CPU instruments and input bytes. | Anonymous scientific grades, timely delivery of independent source material, actual Job IDs, submissions, launches and post-interruption provider identities. |
| Setup controller loss | A local test controller submits a real CPU Job and is killed while the measurement process is alive. | The original process produces the marginal-flow file, and a repeated request finds its existing receipt. This row has zero official scientific turns. |

The historical Run can be checked in [the short replay](short/old-counts.json). The original trial made 11 close submissions. Fourteen decisions were refused, including three evidence-visibility or permission refusals. The first close was refused. The mixed classification explicitly records a missing reason that can be copied alongside a missing unresolved-science field. The preregistration keeps this field separate from the three-arm hydrology study. [Short registration](short/preregistered.json) · [Long registration](long/preregistered.json) · [Controlled physical pilot](long/SETUP.json)

## Running the original trials

Use the release's Python environment and a private workspace on the deployment host. The short runner requires a clean detached checkout of the frozen candidate. The long runner stages Git objects at their exact revisions and installs the two released instrument packages through the normal GX plugin CLI. The ordinary GX entry point for both product arms is `gx-p0 research`. The native arm exposes read, list, submit and observe through the official app server. Both routes run the same versioned hydrometry worker with one CPU and a 30-second limit. Each research arm has 15 minutes, up to four physical Jobs and its own session and Job state. The first trial's original identity remains durable on interruption.

```sh
bench=acceptance/harness_final/benchmark
python=.venv/bin/python
# work, candidate and checkout designate the private series, frozen full SHA and detached checkout.
"$python" -B "$bench/short/run.py" "$work/short" "$checkout" "$candidate"
"$python" -B "$bench/long/run.py" "$work/long" stock "$candidate"
"$python" -B "$bench/long/run.py" "$work/long" baseline "$candidate"
"$python" -B "$bench/long/run.py" "$work/long" candidate "$candidate"
"$python" -B "$bench/long/assessment.py" "$work/long"
"$python" -B "$bench/long/archive_outputs.py" "$work/long"
```

Pass the same private path to inspect an existing attempt. The long runner's `began.json`, phase receipts and `outcome.json` distinguish an untouched slot, an interrupted original and a completed trial. A started phase without an outcome requires inspecting that process or its original Run before dispatch. The active account and raw OAuth sessions remain in the deployment's private store. All outbound traffic uses the deployment's loopback Clash proxies. The model is `gpt-6.2-astra` at `max` on the existing official subscription.

## Interpreting the receipts

A completed physical Job qualifies for continuity only when the tool and runner were alive at the controller cut and the same PID and process start times appear in the later outcome. A reused Job ID alone cannot demonstrate continuity. Count a relaunch against the same request separately from a later request for a different instrument or an explicitly independent check. A model can legitimately decide the scientific question without spawning any external measurement. Such an arm still receives its scientific score and has no observed in-flight takeover.

The assessment retains official cumulative token subtotals per thread, physical CPU time, provider startup and execution times. Model-turn intervals can include synchronous tool waits. These overlapping durations cannot be added. A controller/other gap needs complete turn boundaries. Each group contributes one independent question attempt. The resulting counts describe these cases and cannot establish a general speedup or reliability rate.
