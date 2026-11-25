"""Loopback-only model peer used to exercise the provenance scorer."""
import hashlib
import json
from pathlib import Path
import sys
from contextlib import contextmanager
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "acceptance/v6/engineering"), str(ROOT / "acceptance"), str(ROOT / "gx-h")]
from official import brief_from, responses, wire_catalog
from e2e_grid import fixture_env


def decision(brief):
    observations = brief["observations"]
    structures = [r["artifact_ref"] for r in brief["materials"]]
    watches = [{"event":"job_settled","subject_ref":"run"}]
    if not brief["head"]:
        return {"mode":"amend","rationale":"Test whether the selected subset covers the deposited chains.",
                "evidence_refs":[],"reconsider_when":watches,
                "ops":[{"op":"open_branch","branch":"local:geometry","hypothesis":"The ordered mask has broad coverage.",
                        "alternative":"Many deposited positions may be outside this mask.","method_family":"geometry",
                        "discriminating_observation":"Count supported positions.","stop_condition":"Inspect remaining evidence.",
                        "budget_limits":{"job_submissions":5,"cpu_seconds":150}},
                       {"op":"add_node","node":"local:one","branch":"local:geometry","capability":"deposited-chain-geometry@1",
                        "inputs":[structures[0]],"parameters":{"mask":"ordered_backbone"},"depends_on":[],
                        "question":"How many deposited positions meet this mask?"}]}
    cited = [o["artifact_ref"] for o in observations]
    if brief["head"]["version"] == 1:
        value = observations[0]["observation"]
        choice = "common_ca" if value["coverage_fraction"] < .6 else "common_backbone"
        return {"mode":"amend","rationale":"The returned coverage determines the next mask; the second deposition lacks a measurement.",
                "evidence_refs":cited,"reconsider_when":watches,
                "ops":[{"op":"add_node","node":"local:two","branch":brief["branches"][0]["branch_id"],
                        "capability":"deposited-chain-geometry@1","inputs":[structures[1]],
                        "parameters":{"mask":choice},"depends_on":[],"question":"Measure the second deposited model."}]}
    return {"mode":"amend","rationale":"The scripted protocol exercise obtained the follow-up observation.",
            "evidence_refs":cited,"reconsider_when":[{"event":"goal_changed","subject_ref":"run"}],
            "ops":[{"op":"close_run","reason":"Scripted engineering endpoint reached.","claim_refs":[],
                    "unresolved":["This short protocol did not complete the scientific comparison."]}]}


@contextmanager
def peer(private, out, binary):
    lock = json.loads((ROOT / "release-assets/lock.json").read_bytes())
    if hashlib.sha256(binary.read_bytes()).hexdigest() != lock["linux_x64"]["binary_sha256"]:
        raise ValueError("Official host does not match release")
    inputs = []
    def respond(body, ident):
        _, catalog = wire_catalog(body)
        assert "gx_decide_revision" in catalog
        assert len(inputs) < 5, "Unexpected extra scripted request"
        brief = brief_from(body)
        proposed = decision(brief)
        call_id = "call-" + ident
        inputs.append({"call_id":call_id,"request_sha256":hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest(),
                       "visible":brief,"catalog":sorted(catalog),"decision":proposed})
        (out / "scripted-requests.json").write_text(json.dumps(inputs,ensure_ascii=False,indent=2)+"\n")
        return ({"type":"function_call","namespace":"gx","name":"gx_decide_revision","call_id":call_id,
                 "arguments":json.dumps({"decision":proposed})},{"tool":"gx_decide_revision"})
    (private / "home").mkdir()
    with responses.local_responses(out / "local-peer.jsonl") as server, patch.object(responses, "fixture_action", respond):
        yield fixture_env(private/"home",server,str(binary))
