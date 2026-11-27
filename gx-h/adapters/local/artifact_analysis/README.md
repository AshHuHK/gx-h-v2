# Artifact Analysis

A worker with `gx_job_submit` can request `artifact-analysis@1` on one to eight named, cited JSON artifacts. Run `gx_capability_inspect` to see the capability version, release digest, resource defaults and governance state. Submit the version, the artifact reference for each input alias, an idempotency key, an expression and an exact JSON output schema. The worker can park with `gx_wait_job` as soon as it receives the proposed Job ID. The coordinator admits the work under the worker’s original task, lease and budget and wakes the same attempt with its result.

For example, two input aliases named `treated` and `baseline` can contain arrays of measurements. This script returns their difference in means.

```python
{'difference': mean(data['treated']) - mean(data['baseline'])}
```

This schema declares the returned numeric field.

```json
{"type":"object","properties":{"difference":{"type":"number"}},"required":["difference"],"additionalProperties":false}
```

The expression supports literal objects and arrays, keyed/indexed access to JSON values, numeric `+`, `-`, `*`, `/`, and named operations. `count` takes an array. `sum`, `mean`, `median`, `stdev`, `min` and `max` take numeric arrays. `stdev` is the sample standard deviation. `abs` and `sqrt` take one number. `column(rows, key)` extracts the same field from every row. `sample(rows, n)` requires an explicit unsigned 32-bit seed, samples without replacement, and starts from that seed at each invocation. `params` holds the optional parameter object. Output properties may be numbers, integers, booleans, strings or bounded arrays of those scalars. Each declared field is required.

The existing Job runner stages and hashes the program and every selected input as distinct Artifact refs. A trusted launcher reads at most 1 MiB of selected JSON and 16 KiB of program data. The computing child receives the selected bytes in memory, clears its environment, closes all descriptors except a result pipe, and installs a Linux libseccomp default-deny syscall allowlist before parsing them. The child has one CPU thread, a 128 MiB hard address-space limit, a zero file-size limit, and at most ten seconds wall time under the Job runner. It can return up to 32 KiB of schema-validated JSON. Each run verifies that opening a harmless local device and creating an unconnected socket return EPERM. Its result, execution manifest and resource log are published through the Job and Artifact services. Linux, procfs, Python and libseccomp.so.2 are required. Isolation setup failures end the Job before evaluation.
