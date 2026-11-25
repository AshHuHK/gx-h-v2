"""Review a scripted inventory experiment using GX rows, native rollouts and an independent mmCIF parser."""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from Bio import __version__ as bio_version
from Bio.PDB.MMCIF2Dict import MMCIF2Dict


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review(work, out, kit):
    bundle = work / "bundle"
    out.mkdir(parents=True, exist_ok=False)
    anchor = out / "detached-anchor.json"
    anchor.write_text(json.dumps({"bundle_sha256": sha(bundle / "bundle.json")}, indent=2) + "\n")
    native_window = json.loads((work / "observer/window.json").read_bytes())
    rollout = Path(native_window["rollout"])
    source = [json.loads(line) for line in rollout.read_bytes().splitlines()]
    raw, = [r["payload"] for r in source if r.get("type") == "response_item" and
            r["payload"].get("name") == "gx_decide_revision" and r["payload"].get("type") == "function_call"]
    sent = json.loads(raw["arguments"])["decision"]
    assert [op["op"] for op in sent["ops"]] == ["close_run"]
    with closing(sqlite3.connect((work / "state/gx.db").as_uri()+"?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        def asset(ref):
            row, = c.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (ref,))
            path = work / "state/artifacts" / row["namespace"] / row["rel_path"]
            assert row["sha256"] == sha(path)
            return path
        run, = c.execute("SELECT run_id,status,workflow_id FROM runs")
        assert run["status"] == "STOPPED" and run["workflow_id"] is None
        decision, = c.execute("SELECT * FROM v6_decisions WHERE decision_ref=?", (native_window["decision_ref"],))
        native, = c.execute("SELECT * FROM p0_v6_native_calls WHERE thread_id=? AND call_id=?",
                             (decision["thread_id"], raw["call_id"]))
        assert native["turn_id"] == decision["turn_id"] and json.loads(native["request_json"])["arguments"]["decision"] == sent
        assert json.loads(decision["request_json"])["decision"] == sent
        terminal, = c.execute("SELECT * FROM events WHERE kind='v6.run.closed'")
        assert json.loads(terminal["payload_json"])["decision_id"] == decision["decision_ref"]
        job, = c.execute("SELECT * FROM jobs WHERE run_id=?", (run["run_id"],))
        assert job["status"] == "SUCCEEDED"
        manifest, = c.execute("SELECT * FROM p0_execution_manifests WHERE job_id=?", (job["job_id"],))
        admission = json.loads(manifest["admission_json"])
        completion = json.loads(manifest["completion_json"])
        structure, = admission["inputs"]
        measurement, = completion["outputs"]
        assert completion["job_id"] == job["job_id"] and completion["exit_code"] == 0
        cif, report = asset(structure["artifact_ref"]), asset(measurement["artifact_ref"])
        assert structure["sha256"] == sha(cif)
        assert measurement["sha256"] == measurement["runner_sha256"] == sha(report)
        receipt = json.loads(report.read_bytes())
        assert receipt["input_sha256"] == sha(cif) and measurement["artifact_ref"] in sent["evidence_refs"]
        turns = [dict(row) for row in c.execute("SELECT turn_id,thread_id,settled_tokens,confidence FROM p0_meter_native_turns ORDER BY sequence")]
        assert len(turns) == 2 and all(r["confidence"] == "PROVIDER_CONFIRMED" and r["settled_tokens"] == 35 for r in turns)
        numeric = MMCIF2Dict(str(cif))
        fields = ("group_PDB", "label_asym_id", "label_seq_id", "label_atom_id", "label_entity_id", "pdbx_PDB_model_num", "occupancy")
        atoms = defaultdict(set)
        for group, chain, seq, name, entity, model, occupancy in zip(*(numeric["_atom_site."+key] for key in fields), strict=True):
            if group == "ATOM" and entity == "1" and model == "1" and name in {"N", "CA", "C", "O"} and float(occupancy) > 0:
                atoms[chain].add((int(seq), name))
        independent = {key: {"observed_residues": len({seq for seq, _ in items}),
                             "observed_backbone_atoms": len(items)} for key, items in atoms.items()}
        assert set(independent) == set(receipt["per_chain"]) == {"A", "B", "C"}
        assert all(receipt["per_chain"][chain][field] == value for chain, counts in independent.items()
                   for field, value in counts.items())
    peer = [json.loads(line) for line in (work/"runner/requests.jsonl").read_bytes().splitlines()]
    assert len(peer) == 2 and all(r["peer"] == "127.0.0.1" and r["scripted_provider_tokens"] == 35 for r in peer)
    verifier_results = []
    for check in (Path(__file__).resolve().parents[1] / "verify.py", kit / "acceptance/verify.py"):
        result = subprocess.run([sys.executable, "-I", "-B", check, bundle, "--anchor", anchor], check=True,
                                text=True, capture_output=True)
        assert result.stdout.startswith("EXPORT_CONSISTENT mode=observed runs=1")
        verifier_results.append({"verifier_sha256": sha(check), "result": result.stdout.strip(), "exit": result.returncode})
    data = {"review": "Independent raw-record and coordinate inventory review of local scripted experiment",
            "run_id": run["run_id"], "turns": turns, "native_rollout_sha256": sha(rollout),
            "caller": {"thread_id": decision["thread_id"], "turn_id": decision["turn_id"], "call_id": raw["call_id"]},
            "decision_ref": decision["decision_ref"], "observed_job": job["job_id"], "source_sha256": sha(cif),
            "result_sha256": sha(report), "cpu_seconds": json.loads(job["measured_json"])["cpu_seconds"],
            "external_paid_requests": 0, "local_fixture_tokens": 70, "effect": "CLOSED", "event_id": terminal["event_id"],
            "independent_parser": "Biopython "+bio_version+" MMCIF2Dict", "atom_occupancy": ">0", "per_chain": independent,
            "comparison": {"method": "integer counts on the same coordinates", "maximum_observed_difference": 0, "tolerance": 0},
            "scope": "One deposited model and three chains. Model decisions scripted. No claim of live scientific autonomy.",
            "checks": verifier_results}
    (out/"review.json").write_text(json.dumps(data, indent=2) + "\n")
    return data


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--kit", required=True, type=Path)
    args=p.parse_args()
    print(json.dumps(review(args.work.resolve(), args.out.resolve(), args.kit.resolve()), indent=2))
