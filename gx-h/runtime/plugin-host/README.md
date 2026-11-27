# Optional score evaluator

The GX science_grid workflow accepts a frozen evaluator object in its run inputs. Create the object at project setup with `gx_core.plugins.evaluator.plugin_ref("1")`. The evaluator reads the Job's complete verified observation artifact, counts values at or above the proposal's inclusive threshold and returns the maximum and sample mean. Version `2` also reports the minimum. Observations are measurements from the supplied finite grid. The independent assessment and next-round decision still belong to GX tasks.

Use Node 22.17+ and install the dependency lock once in this directory. In this workspace, all networked installs use the local Clash proxy.

```sh
npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org \
  --proxy=http://127.0.0.1:7890 --https-proxy=http://127.0.0.1:7890 \
  --noproxy=localhost,127.0.0.1,::1
node --experimental-strip-types --test host.test.ts
```

GX starts a local Node process for each uncompleted evaluation. Input is one JSON object on stdin with run, attempt, implementation version and sample. Output is one numeric summary on stdout. A scope owns its evaluation listener. Disposal drains admitted calls and removes only that listener. The process has no Job or GX database interface. The host uses DSH's createScope and scopeTarget and Cordis's Context, plugin fibers, event dispatch and cleanup.

In the GX store, a route's Job ID identifies its evaluation attempt. GX publishes plugin-lock.json before invoking the provider. The immutable lock includes the local implementation and composition digests, dependency versions, original input ref and hash and numeric cutoff. A registered result.json can be returned by reference on recovery. An unfinished invocation can be retried with the same lock because this evaluator is pure. A missing implementation or changed lock blocks unfinished work. Keep the previous source and package lock for resuming it. Select a different version on new work by changing the evaluator object at project creation. Omit the evaluator field on a new run to use the Python workflow without Node.

Runtime dependencies are @deepseek-ai/cordis 4.1.0, @deepseek-ai/cosmokit 1.9.0, @deepseek-ai/dsh-scope 0.2.2-rc.5 and @standard-schema/spec 1.1.0. npm locks tarball integrity. The reviewed source archive is DeepSeek Harness 2e39e70496aa02986ca6a820c5d497e816b69d7e. Its scope and cosmokit src trees match the installed packages. Its vendored Cordis source has later fiber, registration and context changes relative to published Cordis 4.1.0. The scoped evaluation tests exercise the published build. The score evaluator itself is GX code. DSH SDK sessions, presets, settings and HMR require services outside this composition.
