# ACC-1 official App Server evidence

The executable was the installed official Codex CLI 0.169.0. The JSON records include its SHA-256. Each case used a temporary Codex home, an unauthenticated Responses peer on 127.0.0.1, stdio between GX and Codex, and inherited Clash proxy settings. The peer supplied predetermined arithmetic replies and token usage. The wake cases also ran the local fixture-grid CPU backend.

## Protocol observations

| Probe | Official observation |
| --- | --- |
| Two responses in one turn | Live cumulative totals were 35 and 70. After process death, the cold resume response preceded a notification for that turn with total 70. |
| Provider omitted the second response's usage | Live cumulative totals were 35 and 35. After process death, the same completed turn replayed 35. The per-response raw notification for the second response carried null usage. Its rollout contained a second `token_count` and only one `token_usage_record`. |
| Parent finished before child | The original root's cumulative total reached 70 and its turn completed while the child's model reply remained blocked. Releasing the reply produced 183 tokens on the child thread and a child completion. Native child `rootTurnId` identified the original parent turn. A cold resume of the parent yielded 70. An explicit diagnostic resume of the child yielded 183 and issued zero model requests. |

The raw cases retain sanitized frame sequences, typed turn history, rollout record kinds and scalar token counts. The public GX cases use these native observations to check business behavior. The pre-change files were run against a clean export of commit `47165af6befa919a5fc444a7c823ea9160cfb22f`, with the same acceptance scripts copied in.

## Measured GX outcomes

All eight cases passed on the isolated source snapshot identified by `source-snapshot.json`. Each suffix-free JSON is from that same run. A warning about cleaning a stale Codex temporary directory appeared on stderr. Each run initialized Codex, executed the specified requests and exited successfully.

| Case | Baseline result | Snapshot result |
| --- | --- | --- |
| GX sees one intermediate sample and reconnects after the second response | Recovered total 70 and settled a 35-token follow-up. | Recovered total 70 and settled the follow-up, with cumulative expenditure of 105 tokens and two GX calls. |
| GX sees one intermediate sample and the provider omits the last response's usage | Charged 35 and admitted the follow-up. | Held the original reservation, reported its reason, and issued zero follow-up turns or reservations. |
| Child finishes after parent | Admitted and reserved for the next root turn while the child was still running. | Held the parent's reservation and refused the follow-up. Once the verified child ended, GX charged 253 tokens and one call. The follow-up then reached the peer once and brought the total to 288 tokens and two calls. |
| Original waiting lease, wake ACK lost | One effective input and one charge. | One effective input and one charge. |
| Expired waiting lease, wake ACK lost | The original waiter lost the lease. The public run exhausted the replacement attempts before any wake of the original recipient. | The public run recovered the original thread and attempt. One wake reached native history. Recovery added zero model requests and settled the 35-token wake exactly once. |

## Wake fault boundary

A focused worker was parked through the GX Roots and JobService APIs with one real numerical Job owned by its lease. The first public `bin/gx-p0 run` observed the terminal Job and sent the wake. A transparent stdio relay saw the official `turn/start` acknowledgment, withheld the wake frames and immediately sent SIGKILL to the GX CLI. The relay kept the native process alive until the already accepted turn finished, then terminated it. Cold recovery ran the ordinary CLI in a new process with the original SQLite and Codex stores. A separate public native history read counted the original client message ID. Each JSON compares the pre-wake, post-kill and recovered receipts, native charges, Job, lease and attempt. `--expire-wait` moved only the fixture waiter's lease expiration into the past before observing terminal Job status. The unexpired lost-ACK control already passed on the baseline. The baseline expired waiter lost its lease before it reached the wake.

The fixture project ends the three-second CLI observation at the timing limit. The experiment measures delivery and accounting during that window. It does not produce a scientific conclusion. Native and GX loss before the accepted turn is durably written is a different fault boundary.

## Reproduce

Run these commands in the package root with the installed pinned binary and Clash. Use a fresh output directory to preserve previous evidence.

```bash
evidence=$(mktemp -d)
for scenario in two_samples missing_last_usage delayed_child; do
    /opt/conda/bin/python -B acceptance/acc1_protocol.py --case "$scenario" --out "$evidence/$scenario.json"
done
for scenario in two_samples missing_last_usage; do
    /opt/conda/bin/python -B acceptance/acc1_cold_meter.py --case "$scenario" --out "$evidence/gx-$scenario.json"
done
/opt/conda/bin/python -B acceptance/acc1_child_late.py --out "$evidence/child-late.json"
/opt/conda/bin/python -B acceptance/acc1_wake.py --out "$evidence/wake.json"
/opt/conda/bin/python -B acceptance/acc1_wake.py --expire-wait --out "$evidence/expired-wake.json"
```
