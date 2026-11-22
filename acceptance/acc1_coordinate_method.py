"""Check the released TL1A CPU methods from an isolated copy and a new GX database."""
import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_copy(root):
    sys.path.insert(0, str(root))
    from adapters.scientific.tl1a_geometry.review import compare_protocols, independent_check
    from gx_core import budgets
    from gx_core.artifacts import ArtifactService
    from gx_core.jobs.geometry_workflow import observe_geometry, observe_quality, submit_geometry, submit_quality
    from gx_core.jobs.service import JobService
    from gx_core.p0.manifest import read_manifest, validate_evidence
    from gx_core.persistence import records, store

    data = root / "adapters/scientific/tl1a_geometry/data"
    raw, criteria_raw = (data / "2RE9.cif").read_bytes(), (data / "criteria.json").read_bytes()
    criteria, provenance = json.loads(criteria_raw), json.loads((data / "provenance.json").read_bytes())
    reference = json.loads((data / "reference.json").read_bytes())
    require(len(raw) == provenance["bytes"] and hashlib.sha256(raw).hexdigest() ==
            provenance["sha256"] == reference["input_sha256"], "Deposited coordinate bytes changed")
    require(hashlib.sha256(criteria_raw).hexdigest() == reference["criteria_sha256"], "Criteria bytes changed")
    db = Path.cwd() / "method-state" / "gx.db"
    db.parent.mkdir()
    conn = store.open_store(db)
    actor = "offline-coordinate-check"
    try:
        with store.transaction(conn) as tx:
            budget = budgets.create_root(tx, run_id=None, actor=actor)
            run = records.create_run(tx, objective="Check the released coordinate methods on 2RE9",
                                     project_ref="tl1a-method-check", actor=actor, budget_ref=budget)
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(tx, machine="run", subject_id=run, to_state=state, actor=actor)
        def publish(filename, body, kind):
            return ArtifactService(conn).publish_bytes(namespace=f"{run}/inputs", rel_path=filename, kind=kind,
                data=body, run_id=run, media_type="chemical/x-mmcif" if kind == "structure" else "application/json").artifact_ref

        cif_ref = publish("2RE9.cif", raw, "structure")
        criteria_ref = publish("criteria.json", criteria_raw, "evaluation_criteria")
        quality_args = {"run_id": run, "structure_ref": cif_ref, "operation_id": "coordinate-inventory"}
        quality_job = submit_quality(conn, **quality_args)
        conn.close()
        conn = store.open_store(db)
        require(JobService(conn).wait(quality_job["job_id"], 30)["status"] == "SUCCEEDED", "Inventory Job failed")
        quality = observe_quality(conn, quality_job["job_id"])
        quality_ref = quality["observations_ref"]
        validate_evidence(conn, run_id=run, job_id=quality["job_id"], artifact_ref=quality_ref)
        quality_sha = ArtifactService(conn).describe(quality_ref)["sha256"]
        jobs = []
        for protocol in criteria["protocols"]:
            args = dict(run_id=run, operation_id="geometry-" + protocol, protocol=protocol,
                        structure_ref=cif_ref, criteria_ref=criteria_ref, quality_ref=quality_ref)
            job = submit_geometry(conn, **args)
            jobs.append((args, job["job_id"]))
        conn.close()
        conn = store.open_store(db)
        results, receipts, checks = [], [], []
        for args, job_id in jobs:
            require(JobService(conn).wait(job_id, 30)["status"] == "SUCCEEDED", "Geometry Job failed")
            job = observe_geometry(conn, job_id)
            output = job["observations"]
            proof = validate_evidence(conn, run_id=run, job_id=job_id, artifact_ref=job["observations_ref"])
            manifest = read_manifest(conn, job_id)
            selection = next(e for e in manifest["admission"]["inputs"] if e["role"] == "selection")
            require(selection["artifact_ref"] == quality_ref and selection["sha256"] == quality_sha ==
                    output["quality_sha256"], "Geometry Job did not consume the measured coordinate inventory")
            checked = independent_check(raw, output, criteria)
            require(checked["status"] == "matches", "Independent SVD reproduction disagrees")
            saved = reference["protocols"][output["protocol"]]
            require(saved["mapping"] == checked["reference"]["mapping"] and
                    saved["status"] == checked["reference"]["status"], "Shipped reference atom selection differs")
            require(saved["implementation"]["script_sha256"] == checked["implementation"]["script_sha256"],
                    "Reference implementation changed without updating its recorded observations")
            for actual, old in zip(checked["reference"]["pairs"], saved["pairs"], strict=True):
                require((actual["fixed_chain"], actual["moving_chain"]) == (old["fixed_chain"], old["moving_chain"])
                    and abs(actual["rotation_angle_deg"] - old["rotation_angle_deg"]) <= criteria["reproduction"]["angle_abs_tol_deg"]
                    and abs(actual["fitted_rmsd_angstrom"] - old["fitted_rmsd_angstrom"]) <= criteria["reproduction"]["rmsd_abs_tol_angstrom"],
                    "Shipped numerical reference differs from a new fit")
            require(submit_geometry(conn, **args)["job_id"] == job_id, "Reopening a completed Job launched another Job")
            receipts.append({"protocol": output["protocol"], "job_id": job_id, "proof": proof,
                             "backend": manifest["completion"]["backend"],
                             "scheduler_ref": manifest["completion"]["backend_job_id"],
                             "inputs": manifest["admission"]["inputs"], "capability": manifest["admission"]["capability"]})
            checks.append(checked)
            results.append(output)
        require(submit_quality(conn, **quality_args)["job_id"] == quality["job_id"], "Inventory replay duplicated the Job")
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0]
        require(count == 1 + len(jobs), "The number of external Jobs changed on replay")
        by_protocol = {row["protocol"]: row for row in results}
        full_coverage = [by_protocol[p] for p in ("common_backbone", "common_ca")]
        pair = compare_protocols(full_coverage, criteria)
        all_selections = compare_protocols(results, criteria)
        require(pair["status"] == "stable" and all_selections["status"] == "insufficient",
                "The deposited-coordinate outcome changed under the frozen criteria")
        altered = copy.deepcopy(results[0])
        altered["pairs"][0]["rotation_angle_deg"] += 1.0
        require(independent_check(raw, altered, criteria)["status"] == "disagrees",
                "Independent reproduction accepted a deliberately perturbed rotation")
        tighter = copy.deepcopy(criteria)
        tighter["comparison"]["max_angle_delta_deg"] = 0.01
        require(compare_protocols(full_coverage, tighter)["status"] == "sensitive",
                "Comparison failed to detect a known threshold crossing")
        return {"mode": "offline_release_method_check", "run_id": run, "source": provenance,
                "source_artifact_ref": cif_ref, "criteria_ref": criteria_ref,
                "quality": {"job_id": quality["job_id"], "artifact_ref": quality_ref, "sha256": quality_sha,
                            "source": quality["observations"]["source"], "per_chain": quality["observations"]["per_chain"],
                            "mapping": quality["observations"]["mapping"]},
                "job_receipts": receipts, "external_job_count_after_cold_replay": count,
                "measurements": results, "independent_checks": checks, "full_coverage_comparison": pair,
                "all_selection_comparison": all_selections, "controls": {
                    "one_degree_perturbation": "disagrees", "alternate_0.01_degree_threshold": "sensitive"}}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--copied-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.copied_root:
        report = verify_copy(args.copied_root)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        return
    package = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="gx-coordinate-method-") as temp:
        scratch = Path(temp)
        harness = scratch / "gx-h"
        for subdir in ("gx_core", "adapters", "config", "contracts", "workflows"):
            shutil.copytree(package / "gx-h" / subdir, harness / subdir,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        checker = scratch / "method_check.py"
        shutil.copy2(__file__, checker)
        local_output = scratch / "observations.json"
        subprocess.run([sys.executable, "-I", "-B", str(checker), "--copied-root", str(harness),
                        "--output", str(local_output)], cwd=scratch, check=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_output, output)
        print(f"Verified coordinate inventory, 3 protocols, 9 independent fits and 4 cold-replayed Jobs. {output}")


if __name__ == "__main__":
    main()
