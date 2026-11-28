"""Determine what a fresh normal Goal was actually offered, without starting a model."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile


def probe(root):
    python = root / ".venv/bin/python"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        cmd = [str(python), "-B", str(root / "bin/gx-p0"), "research", "--state", str(path / "state"),
               "--run-key", "normal-forward-study", "--goal", "Evaluate the native shape of this milk protein.",
               "--timeout", "0.001"]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root,
                              env=dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1"))
        if proc.returncode not in (0, 75):
            raise RuntimeError(proc.stderr or proc.stdout)
        with sqlite3.connect(path / "state/gx.db") as db:
            inputs = json.loads(db.execute("SELECT inputs_json FROM runs").fetchone()[0])["inputs"]
        offers = {option["method"] for option in inputs["method_scope"]["capability_options"]}
        return {"normal_cli": "bin/gx-p0 research", "official_model": False, "exit": proc.returncode,
                "run_id": json.loads(proc.stdout)["run_id"],
                "offered": {ref: ref in offers for ref in ("natural-food-fold@1", "mapped-structure-agreement@1")}}

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("release", type=Path)
    a = p.parse_args()
    result = probe(a.release.resolve())
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if all(result["offered"].values()) else 1)
