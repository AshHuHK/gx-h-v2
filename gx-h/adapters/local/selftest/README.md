# A local Job adapter

`selftest@1` uppercases a text artifact with Python's standard library. The Job runner passes the materialized input file to the entry script, captures the process output, publishes `report.txt` and calls the adapter to produce a JSON summary. The example uses one CPU thread.

From the repository root, run this example to register the adapter and exercise its entry point in a temporary store.

```bash
python3 - <<'PY'
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from gx_core.capabilities import environments, ladder, registry
from gx_core.context import Ctx, Principal
from gx_core.persistence import store

with TemporaryDirectory() as state:
    conn = store.open_store(Path(state) / "gx.db")
    ctx = Ctx(conn, Principal("example", scope="operator"))
    spec = {"env_id": "control-plane-python", "kind": "SYSTEM_BINARY",
            "python_executable": sys.executable}
    print(environments.register(conn, spec, actor=ctx.actor)["environment_status"])

    path = Path("adapters/local/selftest/manifests/selftest.json")
    bundle = json.loads(path.read_text())
    for key in ("parser", "entry_script"):
        bundle[key] = registry.publish_script(ctx, bundle[key])
    candidate = registry.register_candidate(ctx, bundle, origin="AUTHORED")
    result = ladder.advance(ctx, candidate["capability_version"])
    for probe in result["probes"]:
        print(probe["rung"], probe["result"])
    print(result["stopped"])
    conn.close()
PY
```

Expect `HEALTHY`, followed by `PASS` for `L0_FILES`, `L1_METADATA` and `L2_ENTRYPOINT`. At this point GX has verified the files, observed the environment and run `--help`. The result reports the outstanding independent review. On a reviewed candidate in a persistent store, calling `ladder.advance` resumes the ladder and submits the smoke and malformed-input checks through the Job service. The adapter supplies both fixtures in [selftest.py](selftest.py).
