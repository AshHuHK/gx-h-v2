# Waking a waiting Root after capacity clears

Commit `2476f2bdd74b53e3833ce35d08287120e4fdf0c8` retains the denying owner's identifiers in the local Driver for that Task and next attempt. Each coordinator pass can ask whether those particular occupants still hold their slots. Ready tasks with the same observed holders share the read on that pass. The original claim transaction decides which Task receives the available slot. Providers waiting for a timed quota reset and workflow-dependent readiness retain their fallback backoff.

The deterministic Driver tests exercised the existing workspace and team admission owners and native turn receipt handling. A team waiter stayed asleep after the previous lease was yielded while its paid turn was still active. A different team's release woke only that team's waiter. Finishing the original turn woke the first waiter on the next 35 ms pass. A second Run's lease release and a second Run's provider turn completion each woke the corresponding type of waiter before the first 500 ms backoff deadline. The provider waiter obtained its lease while the previous Task still owned its original lease. All 70 tests passed in 31.398 seconds. [Regression results](regression.log)

| Observed stable holder | Probes | p50 µs | p95 µs | DB writes |
| --- | ---: | ---: | ---: | ---: |
| Team slot | 512 | 16.91 | 17.82 | 0 |
| Provider turn slot | 512 | 6.88 | 7.32 | 0 |

The probe used a real SQLite store with 128 prior released Root claims and one active scripted native turn. The team query located the named Root by primary key. The provider query located the paid delivery by rowid. SQLite used indexes for every durable table in both plans, including the supporting wait, lease and turn receipt lookups. Each watch used one SELECT. Only the supplied occupant list was enumerated. The elapsed times cover these reads on the test host. Matched full-driver and real-model measurements are separate. [Query plans, raw samples, exact source hashes and environment](stable.json)

Run the following command from `gx-h-v-3` using the release interpreter.

```bash
./.venv/bin/python -B acceptance/v3/p1/dispatch-release-watch/probe.py \
  --source . --out /tmp/gx-stable-slot-watch.json --history 128 --samples 512
```
