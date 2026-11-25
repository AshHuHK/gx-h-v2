"""Verify the identities used by saved static Runs after moving release-owned transport code."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "gx-h"))

from gx_core.orchestration.native_worker import DEFAULT, for_workflow
from gx_core.orchestration.service import default_registry, load_errors


def collect(baseline):
    old = baseline / "gx-h"
    pins = json.loads((old / "config" / "shipped.json").read_bytes())["workflows"]
    registry = default_registry()
    if load_errors():
        raise ValueError(load_errors())
    current = {ref: registry.get(*ref.split("@")).digest() for ref in pins}
    if current != pins:
        raise ValueError("A published workflow changed identity")
    matches = {}
    for name in ("science_inputs", "science_refresh_v2"):
        module = importlib.import_module(f"gx_core.p0.{name}")
        delivered = Path(module.__file__).resolve()
        original = old / "gx_core" / "p0" / f"{name}.py"
        if delivered.read_bytes() != original.read_bytes():
            raise ValueError(f"Published callback bytes differ for {name}")
        matches[module.__name__] = {
            "delivered": delivered.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(delivered.read_bytes()).hexdigest(),
        }
    bindings = {ref: for_workflow(*ref.split("@")).__class__.__name__ for ref in pins}
    if [ref for ref in pins if for_workflow(*ref.split("@")) is not DEFAULT] != ["science_grid@3"]:
        raise ValueError("Native product presentation attached to a different graph")
    return {"result": "PASS", "workflow_pins": current, "frozen_modules": matches, "native_workers": bindings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v6-baseline", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.v6_baseline.resolve()), indent=2))
