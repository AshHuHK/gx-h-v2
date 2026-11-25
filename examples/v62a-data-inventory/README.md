# Local data inventory from a Goal

This example uses the published 2RE9 and 2O0O coordinate files. The Goal grant selects two released methods and allows the Lead to delegate `data_inventory@1`. Each method may run on its declared inputs. The legacy quality capability is pinned to the 2RE9 bytes. The deposited-chain method can measure one registered deposition under one atom selection, including 2O0O. A Job's returned measurement is cited by its actual immutable artifact. [Grant](grant.json)

From the release root, start a new state outside the checkout. This creates the initial Lead brief and has zero paid inference calls.

```bash
study=$(mktemp -d)
bin/gx-p0 start --state "$study" --run-key local-materials \
  --goal "Which local coordinate data support a comparison, and where is coverage limited?" \
  --research-effort medium --goal-config examples/v62a-data-inventory/grant.json
bin/gx-p0 status --state "$study"
```

`research_effort` in the Goal configuration or `--research-effort` selects the scientific delegation policy. An omitted effort uses the released quick path, including the ordinary method descriptions. Medium exposes installed roles for data, model, workflow and evaluation inventory. Deep permits further research roles once installed. A researcher selects the number and focus of actual delegations. `research_modules` can narrow the Run to explicit `module_id@version` declarations. An empty list offers no modules. The Run saves its exact package/declaration digests, versions and dependency closure.

`module_policy` may customize the per-Run limits. Medium defaults to 4 concurrent modules and 3 rounds. Deep defaults to 6 and 5. The initial report projection is 4096 UTF-8 bytes, the Lead brief is 65536 bytes, and the method catalog switches to categories above 15 methods or 8192 UTF-8 bytes of summaries. The corresponding keys are `max_parallel`, `max_rounds`, `report_bytes`, `brief_bytes`, `catalog_limit` and `catalog_bytes`. The Run's normal compute and model budgets still apply.

## Register another module identity

Install a digest-pinned package using the existing `bin/gx-p0 plugin install` command. Give every module a manifest extension of the form `{"id":"data_inventory","contract":"research_module","version":"1"}`. In the package's ordinary `register(ctx)` function, call `ctx.research_modules.register` once per identity, with the exact same module ID and version as its manifest entry. Multiple identities may be provided by one package. Use a new package version when the declaration changes. The loader verifies declared and registered module identities before admission. [Shipped declaration](../../gx-h/plugins/installed/org.gx.data-inventory/1.0.0/plugin.py)

Each declaration lists its supported research efforts and focuses, purpose, output, cost class, module instructions, tools, versioned scientific capabilities and accepted input roles. A method name in a module declaration still requires a corresponding grant on the parent Run and in that individual delegation. L1 discovery exposes the module's purpose, product, cost and supported focuses. A `gx_query_catalog` call with `kind` equal to `method` or `module` can filter by `category` or `query` and follow `next_offset`. An exact `reference` returns the specification within the authenticated role's grant. The package's independent module identity lets a method-survey role use this same interface to produce a short list.
