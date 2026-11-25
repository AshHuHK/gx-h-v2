# V6.1 matched-control experiment

The planned cohort has 25 independent Goal workspaces. Each of five randomly ordered blocks includes the three published interventions and two evaluator-held interventions. The model receives only its own measurement bytes. The reviewer retains the numerical rubric, packet hashes, schedule, aliases and expected effects outside the Lead's working directory. Each scored workspace permits one official OpenAI Codex decision at `gpt-6.2-astra/xhigh`, one protected citation Job, and two additional compute submissions. The provider's default sampling policy applies. Leave failed or timed-out slots in the original denominator.

From a frozen product checkout with its release Python, installed protein provider and official Codex runtime, create a **new** private campaign outside the checkout. The final command starts paid inference. Pass the token admission cap actually authorized for this cohort. At each wave the runner deducts booked tokens, held reservations and uncertain admissions before reserving another slot. Actual provider usage may exceed a turn's reservation. Provider-confirmed tokens, estimated bookings, unconfirmed holds and absent USD appear separately. The historical expectation for 25 live calls is about 1.3–1.6 million tokens, additional to the two-structure study and the equal-budget comparisons.

```bash
source .venv/bin/activate
ctl=acceptance/v61/adaptive/controls.py
study_dir=$(mktemp -d)/campaign
python -B "$ctl" prepare --work "$study_dir" --token-cap "$GX61_CONTROLS_TOKEN_CAP" --plugin org.gx.protein@0.2.0
python -B "$ctl" seed --work "$study_dir"
python -B "$ctl" run --work "$study_dir" --parallel 3
python -B "$ctl" report --work "$study_dir"
```

The source, verifier, rubric, packet and configuration hashes are recorded in `preregistered.json` before the first paid call. Keep that file with the original candidate. A cohort comparison requires one common verified digest for the starting science, protected Job conditions, tool profile, provider, model and budget across all 25 native windows. `report.json` reports actual trials and costs, the prespecified denominator and both original and product verifier results. Each pre-existing admission marker consumes its original slot on continuation. A missing terminal receipt remains unresolved. Public triplets run both full V6 checkers. Each of the five conditions also calls their unchanged per-window checking functions with the frozen packet registry; the historical CLI registers only the original three cases. The enrollment process's bundle checksum is a capture receipt. Give an independent reviewer read access to the retained run stores and export objects for independent anchoring and interpretation. [Exact scientific endpoint](controls_cases/rubric.md)

The offline rehearsal exercises the same campaign allocation and actual GX JobService. It seeds all 25 worlds and uses an official host with a local scripted peer for one block of five. The mock policy always keeps the current plan and cites the observations. Its expected scientific score is four failures and one neutral-event pass. The checker must recover each programmed outcome. These scripted decisions and historical replays never count toward paid decisions. The rehearsal uses a local, credential-free Codex provider. Its original-source replay checks the historical reviewer hashes. Copies of both original anchors ship beside the rehearsal receipts. Supply independently retained originals with --custody to check external chain of custody.

```bash
dry_dir=$(mktemp -d)/campaign
bin_path="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
python -B "$ctl" prepare --work "$dry_dir" --token-cap 2000000 --rehearsal --plugin org.gx.protein@0.2.0
python -B acceptance/phase2a/regression/loopback.py --clash "$GX_CLASH" --receipt "$dry_dir/network.json" -- \
  python -B acceptance/v61/adaptive/controls_rehearse.py --work "$dry_dir" --binary "$bin_path" \
  --custody acceptance/v61/adaptive/controls_receipts/historical-anchors --out "$dry_dir/rehearsal.json"
```

The candidate's shared official provider login and model catalog check dispatched zero inference turns. [Transport readiness](study-evidence/oauth-readiness.json)
