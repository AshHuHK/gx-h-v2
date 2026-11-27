---
name: gx-native-review
description: Independently verify a GX task product against its frozen requirements and submit evidence with a reasoned review verdict.
---

Use a different non-author native worker, matching the task's actual required
model/effort and installed role. Follow `gx-native-run` for native dispatch and
worker bootstrap. On a new review attempt the registered subject needs a settled
`ASSIGNED` record. With `native_v2`, acquire the task, inspect the frozen inputs, and finish with the review manifest. With `native_core`, claim with the current `expected_attempt`/`as_of_event_id`,
bind the returned lease with `CLIENT_REPORTED`, then read the complete frozen
attempt brief. A returning reviewer on the same RUNNING task and valid, bound
lease skips claim and bind, continues its saved review and submits with that lease.
A new subject name does not make self-review independent. When fresh context is required,
explicitly request `agent_type:"gx_verifier", fork_turns:"none"` on pinned V2 and
verify the observed `fresh_context`; an unknown value or requested setting is not
proof. Supplied complete, version-identified canonical text is guidance, not identity
or child Core Skill activation. With this guidance and the task brief available,
separate Core activation proof is not an acceptance condition. Apply warning handling
by business impact as in `gx-native-run`; required review checks still apply.

Read the fixed acceptance criteria, exact product version/hash, source evidence,
prior rejected draft and rejection receipt, and the revision or diff, including all
referenced brief sections. Request missing references before deciding. Judge whether
the requirement was satisfied and the evidence supports the claim. Run the required
permitted tests or reproduction; keep software success, proxy metrics and scientific
support distinct. Do not change criteria, author the replacement product or accept
solely from the author's summary.

Publish the review evidence, complete required checks and submit the existing review
bundle specified by the brief with concrete reasons. GX verifies identity, exact
version and mandatory gates, then records the verdict and advances or returns
repairs. Preserve rejected products and receipts; repairs use the same task's next
legal attempt. Native progress/completion messages notify the coordinator; required
durable version approvals and business handoff remain in GX. For scenario tasks,
answer from delivered material and disclose prior exposure to cases/answers.

On UNKNOWN, inspect the original request and recorded effects. Use the existing
read/reconcile operations until the business owner resolves the pending effect. Stop for
missing identity, policy mismatch, safeguard refusal or exhausted budget.
