"""Archive only the two named public instrument outputs and check them against the independent reference."""

import argparse
import json
from decimal import Decimal
from pathlib import Path
from support import HERE, METHODS, P, read, registered, sha
from routing import receipt


def archive(work, field):
    registered()
    summary = read(field / "results.json")
    reference = read(P / "reference.json")
    outputs = []
    for arm in summary["arms"]:
        folder = work / arm["arm"]
        for job in arm["physical"]:
            method = job["method"]
            name = next((name for allowed, name in METHODS.values() if allowed == method), None)
            if not name:
                continue
            provider_files = job["provider_receipt"].get("files") or []
            file = next((item for item in provider_files if item["path"] == name), None)
            if not file:
                continue
            spec = read(folder / "state/jobs" / job["job_id"] / "spec.json")
            staging = Path(spec["out_dir"]) / name
            artifact_folder = (
                folder / "state/artifacts" / (arm["arm_id"] or "unknown") / "jobs" / job["job_id"]
            )
            candidates = [staging, *artifact_folder.rglob(name)]
            original = next((p for p in candidates if p.is_file() and sha(p) == file["sha256"]), None)
            if not original:
                raise ValueError(
                    f"Original file with the provider checksum is missing for {job['job_id']}"
                )
            destination = field / "outputs" / arm["arm"] / job["job_id"] / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and sha(destination) != file["sha256"]:
                raise ValueError(f"Existing public output differs at {destination}")
            destination.write_bytes(original.read_bytes())
            values = read(destination)
            if (
                values["input_sha256"] != sha(P / "record.json")
                or values["channels"] != reference["sites"]
            ):
                raise ValueError(f"Unexpected study input or gauge order in {destination}")
            errors = {
                site: str(abs(Decimal(str(values[key])) / Decimal(reference["volume_L"][site]) - 1))
                for site, key in zip(values["channels"], ("left_L", "right_L"), strict=True)
            }
            outputs.append(
                {
                    "arm": arm["arm"],
                    "job_id": job["job_id"],
                    "method": method,
                    "path": str(destination.relative_to(field)),
                    "sha256": file["sha256"],
                    "volume_relative_error": errors,
                    "within_registered_tolerance": all(
                        Decimal(value) <= Decimal("0.01") for value in errors.values()
                    ),
                }
            )
    verification = {
        "comparison_scope": "Recorded physical outputs compared with the independent original USGS daily-value calculation",
        "original_flow_sha256": sha(P / "daily.json"),
        "projected_flow_sha256": sha(P / "record.json"),
        "reference_sha256": sha(P / "reference.json"),
        "outputs": outputs,
        "late_source_route": {arm["arm"]:receipt(work/arm["arm"],arm["arm"]) for arm in summary["arms"]},
    }
    dest = field / "output-verification.json"
    text = json.dumps(verification, ensure_ascii=False, indent=2) + "\n"
    if dest.exists() and dest.read_text() != text:
        raise ValueError("Existing verification differs")
    dest.write_text(text)
    return verification


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--field", type=Path, default=HERE / "field")
    args = parser.parse_args()
    result = archive(args.work, args.field)
    print(
        sum(item["within_registered_tolerance"] for item in result["outputs"]),
        "/",
        len(result["outputs"]),
    )
