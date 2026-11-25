# Install the hello_research example

From a GX installation, review the files under `examples/plugins/hello_research`.
Compute the digest of that directory and keep its `sha256` value with the reviewed copy.

```sh
bin/gx-p0 plugin digest examples/plugins/hello_research
bin/gx-p0 plugin install examples/plugins/hello_research --sha256 "$REVIEWED_SHA256"
bin/gx-p0 plugin list
bin/gx-p0 plugin verify org.example.gx.hello-research@0.2.0
```

Set `REVIEWED_SHA256` to the digest you reviewed before running the install command.
The operator installs that exact version in the local GX installation. Verification
imports the registered tool and prints its package identity and contributions.
The tool accepts `{"name":"Ada"}` and returns `{"greeting":"Hello, Ada!"}`.
Its greeting is imported from a nested plugin-local module on first use.

## Goal-only methods in V6.1

A Goal grant lists the exact capabilities required for a study. The operator can also select a package version when multiple installed packages offer the same capability. For example:

```json
{
  "capabilities": ["tl1a-coordinate-quality@1"],
  "plugins": ["org.gx.protein@0.2.0"]
}
```

GX finds the verified provider, registers the immutable capability bundle and saves the package identity, dependency pins, manifest artifact and portable method digest when it creates the Run. The first decision sees just the requested methods. Omitting capabilities produces an empty catalog. Reopening an existing study uses the original grants. A package-owned worker is admitted for the selected Run, subject to its current Intent, capability lifecycle, resource limits and JobService checks.

A new package uses `ctx.capabilities.add_manifest("methods/calibration.json")` for its own bundle and worker. For a method whose ordered inputs and parameters match the manifest, GX supplies the standard binding. Authors can register any additional behavior in `register(ctx)`:

```python
ctx.goal_methods.register(
    "sensor-calibration@1",
    project=compact_observation,
)
```

The optional callbacks are `describe(capability)`, `validate(ctx, capability, refs, parameters, *, producers)`, `bind(ctx, capability, refs, parameters)`, `project(value)` and `dependencies(capability, python_executable, *, probe)`. The binder returns an input-role dictionary and Job parameters. Projection supplies the next decision brief; the source artifact retains the full result. List a `goal_method` extension in the manifest when registering any of these callbacks.

A scoped evaluator can be registered through `ctx.goal_tools.register(name=..., capability=..., schema=..., handler=..., description=...)` with a `goal_tool` extension. GX advertises the tool only to a Lead holding the named method. The handler receives a method context and validated arguments; it returns `ctx.evaluation(value, original_refs, folder=..., kind=...)`. GX binds the event and replay receipt to the current Run, Task, lease, thread and call. The context can describe/read its visible artifacts and the inputs behind genuine Job outputs. Installed package assets are available as verified bytes through `ctx.read_file(path)` during registration.

The coordinate provider demonstrates custom input binding, projection, dependency probing and evidence-based re-evaluation. The older coordinate provider uses the exact release compatibility mapping; its original package remains part of the verification. [Protein package](../../gx-h/plugins/installed/org.gx.protein/0.2.0/plugin.py)
