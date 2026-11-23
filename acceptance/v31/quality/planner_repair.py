"""Replay the two observed planner errors via the signed local Codex and public gx-p0 CLI."""
import argparse
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import run as study


class AdversePlanner(study.StudyPeer):
    def __call__(self, body, ident):
        brief = study.responses.brief_from(body)
        if brief["input"]["output_kind"] != "science_decision" or brief["input"]["round"] != 0:
            return super().__call__(body, ident)
        transport = brief["native_product_transport"]
        # The positive-control peer regards any submission reply as a completed product.
        # Here a genuine C reply explicitly requests a correction on this same Turn.
        repairs = {}
        for item in body["input"]:
            if item.get("type") == "function_call_output" and transport["digest"] in str(item.get("output")):
                reply = study.responses.output_json(item["output"])
                if reply.get("category") == "product_structure":
                    assert reply["code"] == "SCHEMA_INVALID" and reply["pointer"] == "/next_grids/high"
                    assert reply["failure_count"] == 1 and not reply["terminal"]
                    repairs[item["call_id"]] = reply
        history = [i for i in body["input"] if i.get("call_id") not in repairs]
        event, detail = super().__call__({**body, "input": history}, ident)
        if event.get("name") != "gx_submit_product":
            return event, detail
        direct, = [t for group in body["tools"] if group.get("name") == "gx" for t in group["tools"]
                   if t["name"] == "gx_submit_product"]
        props = direct["parameters"]["properties"]["product"]["properties"]
        assigned_grid = transport["schema"]["properties"]["next_grids"]["additionalProperties"]
        assert props["next_grids"]["additionalProperties"] == assigned_grid
        assert props["pivots"]["items"]["properties"]["grid"] == assigned_grid
        assert all(word in str(props["source_decisions"]) for word in ("observations_ref", "assessment_ref", "artifact_ref"))
        args = json.loads(event["arguments"])
        product = args["product"]
        if brief["attempt"] == 1:
            draft = "secondary_citation_with_valid_grid"
            detail["advertised_grid"] = props["next_grids"]
            detail["advertised_source_evidence"] = props["source_decisions"]["items"]["properties"]["evidence_refs"]
            product["source_decisions"][0]["evidence_refs"].append(product["assessment_ref"])
        elif brief["attempt"] == 2 and not repairs:
            draft = "bare_candidate_list"
            for path, grid in product["next_grids"].items():
                product["next_grids"][path] = grid["candidates"]
        else:
            assert brief["attempt"] == 2 and len(repairs) == 1
            draft = "corrected_in_same_turn"
        event["arguments"] = json.dumps(args)
        return event, {**detail, "test_draft": draft, "transport_digest": transport["digest"],
                       "correction_for": list(repairs), "attempt": brief["attempt"]}


def main(args):
    with patch.object(study, "StudyPeer", AdversePlanner):
        study.main(SimpleNamespace(**vars(args), mode="scripted", v2_control=False))
    db = sqlite3.connect((args.private.resolve() / "state/gx.db").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    with db:
        rows = [dict(r) for r in db.execute(
            "SELECT r.thread_id,r.attempt,t.turn_id,t.reason,t.call_id,r.task_id FROM p0_root_terminals t "
            "JOIN p0_roots r USING(thread_id) JOIN tasks a USING(task_id) "
            "WHERE r.role='planner' AND json_extract(a.input_json,'$.round')=0 ORDER BY r.attempt")]
        assert [r["reason"] for r in rows] == ["GX_TERMINAL_REJECTED", "GX_ACCEPTED"]
        rejected, accepted = rows
        corrections = [dict(r) for r in db.execute("SELECT * FROM p0_product_repairs")]
        correction, = corrections
        assert (correction["thread_id"], correction["turn_id"]) == (accepted["thread_id"], accepted["turn_id"])
        assert rejected["task_id"] == accepted["task_id"] and [r["attempt"] for r in rows] == [1, 2]
        submissions = [dict(r) for r in db.execute("SELECT result,attempt,validation_json FROM submissions "
                                                    "WHERE task_id=? ORDER BY attempt", (accepted["task_id"],))]
        assert [(s["result"], s["attempt"]) for s in submissions] == [("REJECTED", 1), ("ACCEPTED", 2)]
        assert "actual reviewed research artifacts" in submissions[0]["validation_json"]
        assert db.execute("SELECT count(*) FROM artifacts WHERE task_id=? AND kind='science_decision'",
                          (accepted["task_id"],)).fetchone()[0] == 2
    db.close()
    provider = json.loads((args.evidence / "scripted-requests.json").read_bytes())
    drafts = [r for r in provider if r.get("test_draft")]
    assert [r["test_draft"] for r in drafts] == [
        "secondary_citation_with_valid_grid", "bare_candidate_list", "corrected_in_same_turn"]
    assert drafts[-1]["correction_for"] == [correction["call_id"]]
    assert len({r["transport_digest"] for r in drafts} | {correction["contract_digest"]}) == 1
    # Join the official authored arguments and the no-publication/durable-terminal receipts.
    published = json.loads((args.evidence / "evidence.json").read_bytes())["official_products"]
    calls = {p["call_id"]: p for p in published}
    bare = calls[correction["call_id"]]
    assert not bare["receipt"] and isinstance(bare["product"]["next_grids"]["high"], list)
    original, corrected = calls[rejected["call_id"]]["product"], calls[accepted["call_id"]]["product"]
    assert original["assessment_ref"] in original["source_decisions"][0]["evidence_refs"]
    assert corrected["next_grids"]["high"]["candidates"] == bare["product"]["next_grids"]["high"]
    assert corrected["assessment_ref"] not in corrected["source_decisions"][0]["evidence_refs"]
    study.runner.save(args.evidence / "planner-repair.json", {"terminal_receipts": rows, "repair": correction,
                      "official_drafts": drafts, "published_decision_artifacts": 2,
                      "scientific_refusal": json.loads(submissions[0]["validation_json"])})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="WORKTREE")
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=study.RELEASE)
    main(parser.parse_args())
