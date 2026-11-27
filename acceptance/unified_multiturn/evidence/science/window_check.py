"""Independently verify the selected 3TBL interface window from both published A6 workspaces."""
import argparse
from collections import Counter
from contextlib import closing
from fractions import Fraction
from hashlib import sha256
from itertools import combinations
import json
from io import StringIO
from math import sqrt
from pathlib import Path
import sqlite3

import numpy as np
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from recount import artifact, contacts, selection

RUN = "gxr_01m8d5edt69rah4ndn"
JOB = "gxj_01m8d5kv34knbdsjaf"
METHOD = "pcna-interface@1"
PAIR_KEYS = ("12", "23", "31")
INTERVAL = set(range(181, 201))


def database(state):
    conn = sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return conn


def digest(data):
    return sha256(data).hexdigest()


def table_hash(table):
    return digest(json.dumps(table["pairs"], sort_keys=True, separators=(",", ":")).encode())


def read_artifact(db, state, rid, ref):
    raw, sha = artifact(db, state, rid, ref)
    return json.loads(raw), sha


def method_pin(db, rid):
    row = db.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
    return json.loads(row[0])[METHOD]


def frozen_criterion(db, ref):
    decision = db.execute("""SELECT d.request_json,d.receipt_json FROM v6_decisions d
        JOIN v6_decision_tasks t USING(run_id,task_id) WHERE d.run_id=? AND d.decision_ref=? AND t.scope='run'""",
                          (RUN, ref)).fetchone()
    rationale = json.loads(decision["request_json"])["decision"]["rationale"]
    assert "极差除以均值" in rationale and "10%" in rationale
    criterion = json.loads(decision["receipt_json"])["event_id"]
    admitted = db.execute("SELECT event_id FROM events WHERE run_id=? AND subject_id=? AND kind='job.created'",
                          (RUN, JOB)).fetchone()[0]
    assert criterion < admitted
    return criterion, admitted


def compare_coordinates(chains, declared, vectors, table, named):
    positions = {chain: set(map(int, values[0])) for chain, values in vectors.items()}
    whole_pairs, checks = {}, {}
    for index, key in enumerate(PAIR_KEYS):
        a, b = chains[index], chains[(index + 1) % 3]
        hits = contacts(vectors[a], vectors[b])
        whole_pairs[key] = hits
        measured = table["pairs"][key]
        assert measured["chains"] == [a, b]
        assert set(map(tuple, measured["contacting_label_seq_id_pairs"])) == hits
        coverage = [len(positions[a]), len(positions[b])]
        assert measured["whole"]["observed_positions"] == coverage
        assert measured["whole"]["n_contact_residue_pairs"] == len(hits)
        window_hits = sorted((p, q) for p, q in hits if p in INTERVAL or q in INTERVAL)
        covered = [len(positions[a] & INTERVAL), len(positions[b] & INTERVAL)]
        outputs = [measured["windows"][9]]
        if key == named["pair"]:
            outputs.append(named)
        for output in outputs:
            assert (output["block_index"], output["label_interval"]) == (9, [181, 200])
            assert output["observed_positions"] == covered and output["n_contact_residue_pairs"] == len(window_hits)
        checks[key] = {"chains": [a, b], "whole_count": len(hits), "whole_coverage": coverage,
                       "window_count": len(window_hits), "window_coverage": covered,
                       "window_label_seq_id_pairs": window_hits}

    counts = [len(group) for group in whole_pairs.values()]
    mean = Fraction(sum(counts), len(counts))
    relative_range = (max(counts) - min(counts)) / mean
    multiplicities = Counter(pair for group in whole_pairs.values() for pair in group)
    shared = {f"{a}/{b}": len(whole_pairs[a] & whole_pairs[b]) for a, b in combinations(whole_pairs, 2)}
    profiles = {
        "mapping": "Cycle-aligned ordered label_seq_id pairs AB, BC, CA; chain letters dropped only for this same-sequence comparison.",
        "intersection_all": sum(n == 3 for n in multiplicities.values()),
        "union": len(multiplicities),
        "intersection_counts_by_number_of_interfaces": {str(n): count for n, count in Counter(multiplicities.values()).items()},
        "pairwise_shared": shared,
        "unique_to_interface": {key: sorted(p for p in group if multiplicities[p] == 1) for key, group in whole_pairs.items()},
    }
    science = {
        "window": "Count each oriented cross-chain label_seq_id pair once when either end lies in inclusive [181,200].",
        "whole_criteria": "Lead committed range/arithmetic mean and 10% criterion before Job creation.",
        "range_over_mean": str(relative_range), "percent": float(relative_range * 100), "arithmetic_mean": str(mean),
        "threshold": "1/10", "meets_threshold": relative_range <= Fraction(1, 10),
        "whole_contact_profiles": profiles,
        "coverage": {"declared_per_chain": len(declared),
                     "window_missing_labels": {c: sorted(INTERVAL - v) for c, v in positions.items()}},
    }
    return checks, science


def identity_example(raw, vectors, chains):
    cif = MMCIF2Dict(StringIO(raw.decode()))
    cols = ("group_PDB", "pdbx_PDB_model_num", "label_entity_id", "label_asym_id", "label_seq_id", "label_comp_id",
            "type_symbol", "occupancy")
    names = {chain: {} for chain in chains}
    for group, model, entity, chain, seq, name, element, occupancy in zip(*(cif["_atom_site." + k] for k in cols), strict=True):
        if ((group, model, entity) == ("ATOM", "1", "1") and chain in chains and seq in ("109", "183")
                and element not in ("H", "D") and float(occupancy) > 0):
            assert names[chain].setdefault(seq, name) == name
    assert all(names[c] == {"109": "GLU", "183": "SER"} for c in chains)
    observations = {}
    for i, key in enumerate(PAIR_KEYS):
        a, b = chains[i], chains[(i + 1) % len(chains)]
        pos_a, xyz_a = vectors[a]
        pos_b, xyz_b = vectors[b]
        delta = xyz_a[pos_a == 109, None, :] - xyz_b[pos_b == 183][None, :, :]
        minimum = int(np.sum(delta * delta, axis=-1).min())
        observations[key] = {"chains": [a, b], "minimum_distance_A": sqrt(minimum) / 1000,
                             "squared_distance_milli_A2": minimum, "contact": minimum <= 4000 ** 2}
    return observations


def close_receipt(directory, cited, checked, science, example):
    source = (directory / "result.json").read_bytes()
    result = json.loads(source)
    final = result["final_decision"]
    assert result["run_id"] == RUN and set(cited) <= set(final["evidence_refs"])
    rendered = final["rendered"]["rationale"]
    counts = [checked[k]["whole_count"] for k in PAIR_KEYS]
    percent = f"{science['percent']:.2f}%"
    for passage in ("、".join(map(str, counts)) + " 对", "均值为 " + science["arithmetic_mean"] + " 对",
                    "相对极差为 " + percent, "10%", "Glu109", "Ser183", "A–B 与 B–C 达到 4.0 Å",
                    "C–A 超出该距离", f"读数为 {checked['23']['window_count']} pairs"):
        assert passage in rendered, passage
    assert [example[key]["contact"] for key in PAIR_KEYS] == [True, True, False]
    predictions = [p for p in result["predictions"] if p["prediction"]["method"]["parameters"] == {"block_index": 9, "pair": "23"}]
    registration, = predictions
    assert (registration["prediction"]["operator"], registration["prediction"]["threshold"]) == ("ge", 1)
    assert registration["registration"]["chronology_scope"] == "same_store" and checked["23"]["window_count"] >= 1
    reason = final["rendered"]["reason"]
    assert "A 链缺 187–192 和 257–261，B 链缺 187–191 和 257–261，C 链缺 186–191 和 259–261" in reason
    expected_gaps = {"A": [*range(187, 193), *range(257, 262)], "B": [*range(187, 192), *range(257, 262)],
                     "C": [*range(186, 192), *range(259, 262)]}
    assert science["coverage"]["whole_missing_labels"] == expected_gaps
    return {"utc": result["closure"]["closed_utc"], "decision_ref": final["decision_ref"],
            "full_result_sha256": digest(source), "report_sha256": digest((directory / "REPORT.md").read_bytes()),
            "rationale": final["rationale"], "rendered_rationale": rendered, "cited_refs": final["evidence_refs"],
            "numeric_review": {"displayed_percent": percent, "reported_named_count": checked["23"]["window_count"],
                               "missing_labels_match": True, "GLU109_SER183_distances": example,
                               "registered_predicate": ">= 1 pair", "registered_scope": "same_store",
                               "prior_workspace_full_table_already_contained_this_window": True}}


def verify(series):
    prior = json.loads(Path(__file__).with_name("successor.json").read_text())
    prior_job, = prior["jobs"]
    old_state = series / "live-e2a102a-01/state"
    trial = series / "live-routes-repair-02"
    state = trial / "state"
    with closing(database(state)) as db, closing(database(old_state)) as old:
        job = db.execute("""SELECT n.spec_json,n.decision_ref,n.result_ref,j.status,j.submitted_at,j.finished_at
            FROM v6_nodes n JOIN jobs j USING(run_id,job_id) WHERE n.run_id=? AND n.job_id=?""", (RUN, JOB)).fetchone()
        assert job and job["status"] == "SUCCEEDED"
        spec = json.loads(job["spec_json"])
        assert (spec["capability"], spec["parameters"]) == (METHOD, {"pair": "23", "block_index": 9})
        ref, = spec["inputs"]
        raw, input_sha = artifact(db, state, RUN, ref)
        assert input_sha == prior_job["input"]["sha256"]
        entry, entity, chains, declared, vectors = selection(raw)
        assert (entry, entity, chains) == ("3TBL", "1", list("ABC"))
        refs = dict(db.execute("SELECT rel_path,artifact_ref FROM artifacts WHERE run_id=? AND job_id=? AND published=1",
                               (RUN, JOB)))
        named_ref, table_ref = refs["files/interface.json"], refs["files/interface-table.json"]
        assert named_ref == job["result_ref"]
        named, named_sha = read_artifact(db, state, RUN, named_ref)
        table, table_sha = read_artifact(db, state, RUN, table_ref)
        old_table, old_sha = read_artifact(old, old_state, prior["run_id"], prior_job["table"]["artifact_ref"])
        assert old_sha == prior_job["table"]["sha256"] and old_table["pairs"] == table["pairs"]
        for item in (named, table):
            assert (item["pair"], item["method"], item["input_sha256"], item["contact_cutoff_A"]) == ("23", METHOD, input_sha, 4.0)
            assert (item["chain_left"], item["chain_right"], item["model"], item["label_entity"]) == ("B", "C", "1", "1")
        criterion, admitted = frozen_criterion(db, job["decision_ref"])
        pin = method_pin(db, RUN)
        assert pin == method_pin(old, prior["run_id"])

    checked, science = compare_coordinates(chains, declared, vectors, table, named)
    science["coverage"]["whole_missing_labels"] = {
        chain: sorted(declared - set(map(int, data[0]))) for chain, data in vectors.items()
    }
    example = identity_example(raw, vectors, chains)
    result = {
        "run_id": RUN, "job_id": JOB, "study_build": json.loads((trial / "study.json").read_text())["build"],
        "criterion_event": criterion, "job_created_event": admitted, "scientist_decision_ref": job["decision_ref"],
        "submitted_at": job["submitted_at"], "finished_at": job["finished_at"],
        "method": METHOD, "pinned_capability_sha256": pin, "parameters": spec["parameters"], "entry": entry,
        "input": {"artifact_ref": ref, "sha256": input_sha},
        "named": {"artifact_ref": named_ref, "sha256": named_sha, "pair": "23", "block_index": 9,
                  "label_interval": [181, 200], "value": named["n_contact_residue_pairs"],
                  "delta": named["n_contact_residue_pairs"] - checked["23"]["window_count"]},
        "table": {"artifact_ref": table_ref, "sha256": table_sha},
        "prior": {"run_id": prior["run_id"], "job_id": prior_job["job_id"], "earlier_table_ref": prior_job["table"]["artifact_ref"],
                  "earlier_table_sha256": old_sha, "prior_table_pairs_canonical_sha256": table_hash(old_table),
                  "new_table_pairs_canonical_sha256": table_hash(table), "equal_tables_excluding_request_header": True},
        "independent_recalculation": checked, "scientific_checks": science,
        "tolerances": {"count_coverage_residue_ids": 0, "internal_coordinate_unit": "0.001 Å; exact squared distance at inclusive 4.000 Å.",
                       "printed_percent": "±0.005 percentage points for two decimal places."}, "independent_SASA": False,
    }
    export = state / "research" / RUN
    result["formal_close"] = (close_receipt(export, [ref, named_ref, table_ref], checked, science, example) if (export / "result.json").is_file()
                              else "Awaiting enacted close and exported final report")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-dir", required=True, type=Path, help="Parent of the two GX A6 trial directories")
    parser.add_argument("--out", required=True, type=Path, help="Output JSON receipt path")
    args = parser.parse_args()
    receipt = verify(args.series_dir)
    args.out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(f"{receipt['run_id']} {receipt['job_id']}: {receipt['named']['value']} residue pairs, delta {receipt['named']['delta']}")
