# V6 observed 2RE9 run

Use the product root, a CPython 3.11 environment with the release wheels on PATH, the installed org.gx.protein plugin and the pinned official Codex 0.169.0 host. The live runner uses an existing official file-backed ChatGPT OAuth login. The official HTTPS and WSS host checks its configured Clash HTTP route through the same path used by gx-p0. The complete proxy environment, including its separately configured SOCKS5 route, is retained. The catalog check uses the official host and issues zero inference turns.

```bash
private_dir=$(mktemp -d)
review_parent=$(mktemp -d)
python3 acceptance/v6/live.py preflight \
  --private-dir "$private_dir" --evidence-dir "$review_parent/preflight"

private_run=$(mktemp -d)
python3 acceptance/v6/live.py observed \
  --private-dir "$private_run" --evidence-dir "$review_parent/observed" \
  --seconds 360
```

Each execution uses a new private directory with mode 0700. The runner passes the text of `examples/v6-2re9/goal.md`, the supplied Kit’s verbatim research question, the pinned 2RE9 coordinate file, the registered method IDs and the actual run limits to `gx-p0 start --goal`. The observed run admits at most five native calls, five submitted Jobs, 300 CPU seconds and one million tokens of new reservation. Each turn initially holds 150,000 tokens. Use the provider receipts for observed usage. USD is unknown until supplied by the provider.

The product invokes the Lead in a fresh native thread with the run's scoped GX tools. The private Codex home has a reference to the operator's existing auth file, fresh project and session stores, and no copied engineering instructions or previous sessions. Keep the complete private directory under the operator's control. The independently reviewable material includes the effective thread settings, the actual brief and submitted tool call, material hashes and Job outputs. The read-only monitor captures state at Job and decision boundaries. The cold reopen runs the public resume entry in another process with GX_CODEX_OFFLINE=1.

An observed exit of 0 means that a native decision window could be exported for review. Exit 2 records a preflight blocker, 3 records an unsuccessful Goal-only start, 4 signals a driver or resume failure and 5 means no qualifying observed window could be projected. An operator can inspect an incomplete run with `bin/gx-p0 status --state "$private_run/state"`. Continue the same run using its existing dedicated `HOME`, `CODEX_HOME`, `GX_P0_PROFILE` and Run ID to retain the original thread store, Jobs and costs.

An independent reviewer should compare each candidate with the original session, Job manifests and both process receipts, save the approved bundle digest separately and run the supplied verifier.

```bash
python3 acceptance/v6/verify.py "$review_parent/observed/candidate-000" \
  --anchor /path/held-by-reviewer/observed-anchor.json
```
