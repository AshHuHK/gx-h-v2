"""Run the pinned independent coordinate method on each measured route in one CPU Job."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy


METHOD_SHA256 = "7d9ca4e78049900243f3a042b8534aebf95745cac8eab31cff3803ac3b9f4705"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("method", "structure", "criteria", "observations", "out"):
        parser.add_argument("--" + field, required=True, type=Path)
    args = parser.parse_args()
    if hashlib.sha256(args.method.read_bytes()).hexdigest() != METHOD_SHA256:
        raise ValueError("the independent coordinate method differs from its frozen version")
    check = runpy.run_path(str(args.method))["independent_check"]
    coordinates = args.structure.read_bytes()
    criteria = json.loads(args.criteria.read_bytes())
    batch = json.loads(args.observations.read_bytes())
    results = {}
    for protocol, member in batch["routes"].items():
        data = member["measurement_json"].encode("utf-8")
        if hashlib.sha256(data).hexdigest() != member["measurement_sha256"]:
            raise ValueError(f"the observation bytes for {protocol} differ from the assigned artifact")
        observation = json.loads(data)
        if observation.get("protocol") != protocol:
            raise ValueError(f"the observation does not identify its assigned protocol {protocol}")
        try:
            result = check(coordinates, observation, criteria)
        except (ImportError, ValueError, OSError, ArithmeticError) as exc:
            result = {"status": "unavailable", "protocol": protocol, "error": f"{type(exc).__name__}: {exc}"}
        results[protocol] = {**result, **{k: v for k, v in member.items() if k != "measurement_json"}}
    if not results:
        raise ValueError("the reviewer needs at least one observed route")
    output = {"schema": "gx.tl1a.independent_batch/1", "evidence_ref": batch["evidence_ref"],
              "method_sha256": METHOD_SHA256, "results": results}
    (args.out / "checks.json").write_text(json.dumps(output, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
