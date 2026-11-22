"""Alternate persisted Run bindings in one fresh process using only the staged GX installation."""
import json
from pathlib import Path
import sqlite3
import sys


if __name__ == "__main__":
    stage, legacy, composed = map(Path, sys.argv[1:])
    sys.path.insert(0, str(stage / "gx-h"))
    from gx_core.errors import ScopeDenied
    from gx_core.orchestration.service import hooks_for_run
    from gx_core.p0.roots import require_binding

    restored = []
    for state in (legacy, composed, legacy, composed):
        db = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT run_id,workflow_sha256 FROM runs").fetchone()
            hooks = hooks_for_run(db, row["run_id"])
            names = [tool["name"] for tool in hooks.build_worker_tools(db, row["run_id"]).specs]
            assert ("gx_grid_measurement" in names) == (state == composed), (state.name, names)
            result = {**dict(row), "tools": names}
            if state == composed:
                thread = db.execute("SELECT thread_id FROM p0_root_calls WHERE "
                                    "json_extract(result_json,'$.metrics.max_score') IS NOT NULL LIMIT 1").fetchone()[0]
                try:
                    require_binding(db, row["run_id"], thread)
                except ScopeDenied as denied:
                    result["released_attempt"] = {"thread_id": thread, "refusal": str(denied)}
                else:
                    raise AssertionError("the measurement worker retained its completed attempt")
            restored.append(result)
        finally:
            db.close()
    assert restored[:2] == restored[2:]
    print(json.dumps(restored))
