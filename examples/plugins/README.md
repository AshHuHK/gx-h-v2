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
