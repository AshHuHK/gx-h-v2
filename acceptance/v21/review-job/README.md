# C04 and C05 — detached coordinate review

The public `--tl1a-geometry` example ran `tl1a_geometry@3` through the official Codex 0.169.0 App Server with a local scripted Responses peer. All six CPU Jobs ran on the local detached scheduler. The first round compared common backbone and common Cα measurements. Its reviewer found a stable comparison. The decision commissioned the ordered-backbone protocol. The second reviewer found the comparison sensitive under this control's prospective 40-residue and 25% coverage criteria. The decision stopped. The independent reproduction magnitudes apply to the deposited crystal coordinates.

`public-cli.json` identifies the two independent reviewer tasks and native threads, their request and collection turns, the Jobs and scientific report. The official requests went to the loopback peer. Each batch requested one Job, parked through `gx_wait_job`, and collected the results on a later native turn belonging to the same attempt. All six submissions counted against the formal six-Job limit. A second command reopened the completed state and verified the same Jobs and report.

`public-products.json` contains both original execution manifests and 25 referenced artifact records. JSON records retain the exact UTF-8 text and recorded SHA-256. The coordinate record points to the released 507,971-byte 2RE9 mmCIF by digest. Each review input collection retains the exact measurement bytes and distinguishes their source Job output from any rescoring. Each `checks.json` records the original source hash, independently recovered atom mapping, SVD estimates, full per-pair discrepancies and tolerance predicates. The corresponding `geometry_checks` receipts bind each result to the reviewer and the request and collection calls.

`baseline.json` records the original synchronous `@2` timings on the same structure. `cold.json` measures the `@3` Root callback directly, including a call tracer that fails the test on numerical fitting, process launch, or nested RPC. Each review was interrupted after durable submission and parking but before Job admission. The next connection launched the original Job ID. Replaying the initial callback after admission, requesting another route in the same batch and collecting a route twice all used the original operation. This control used the default 60-residue and 60% coverage threshold and retained the resulting insufficient coverage for the ordered subset. `four-job-cap.json` records a study with one inventory, two measurements, one independent review and a stop after one round.

From the package root, reproduce the public acceptance with `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python acceptance/v21_review_job.py`. Reproduce the cold starts, capped study, discrepancy controls, historical contracts, and pre-send nested-RPC guard with the following command.

```sh
OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=gx-h \
GX_REVIEW_JOB_EVIDENCE=acceptance/v21/review-job/cold.json \
.venv/bin/python -m unittest tests.test_tl1a_review_job \
  tests.test_app_server_client.AppServerClientTests.test_callback_nested_request_is_refused_before_second_send \
  tests.test_tl1a_review_versions tests.test_tl1a_geometry
```
