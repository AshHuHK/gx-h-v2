"""Local Responses peer, used only for the labeled rehearsal of the two Lead arms."""
import json

from common import ENTRIES, METHOD, pair_results
from e2e_responses import output_json
from official import brief_from
from native_probe_peer import declared_calls, wire_catalog


def branch(name, label, count):
    return {"op":"open_branch", "branch":name, "hypothesis":f"Pair geometry from {label} agrees across measurable masks.",
            "alternative":"The masks may distinguish chain-pair geometry or have inadequate coverage.",
            "method_family":"deposited coordinate fits", "discriminating_observation":"Matched position coverage and pair-fit deltas.",
            "budget_limits":{"job_submissions":count,"cpu_seconds":30 * count},
            "stop_condition":"Compare measurable masks or record why coordinates are insufficient."}


def node(scope, name, source, mask):
    return {"op":"add_node","node":name,"branch":scope,"capability":METHOD,"inputs":[source],
            "parameters":{"mask":mask},"depends_on":[],"question":f"What are the coverage and pair fits for {mask}?"}


def call_history(body):
    calls = {i["call_id"]:i for i in body["input"] if i.get("type") == "function_call"}
    answers = {}
    for item in body["input"]:
        if item.get("type") != "function_call_output" or item["call_id"] not in calls:
            continue
        call = calls[item["call_id"]]
        result = output_json(item["output"])
        if isinstance(result,dict) and result.get("error"):
            raise ValueError(f"{call['name']} failed: {result['error']}")
        args = json.loads(call["arguments"])
        answers[(call["name"],args.get("ref"))] = result
    return answers


def action(arm, body, ident):
    brief = brief_from(body)
    answers = call_history(body)
    material = {m["role"].removeprefix("structure_"):m["artifact_ref"] for m in brief["materials"]}
    if set(material) != set(ENTRIES):
        raise ValueError("Unexpected material in the native decision brief")
    done = [n for n in brief["nodes"] if n["result_ref"]]
    observed = {row["artifact_ref"]:row["observation"] for row in brief["observations"]
                if row.get("artifact_ref") and row.get("observation",{}).get("method") == METHOD}
    def send(name, args):
        namespace, = [ns for ns,leaf in declared_calls(wire_catalog(body)[0]) if leaf == name]
        return ({"type":"function_call",**({"namespace":namespace} if namespace else {}),"name":name,
                 "call_id":"call-" + ident,"arguments":json.dumps(args)}, {"tool":name,"arm":arm})
    def decide(ops, refs, reason, watched):
        return send("gx_decide_revision", {"decision":{"mode":"amend" if ops else "resolve","rationale":reason,
                "evidence_refs":refs, "ops":ops, "reconsider_when":watched}})
    def watches(names):
        return [{"event":"job_settled","subject_ref":name} for name in names]
    if not brief["head"]:
        scopes = {e:"local:" + (e.lower() if arm == "multi" else "joint") for e in ENTRIES}
        ops = ([branch(scopes[e],e,4) for e in ENTRIES] if arm == "multi" else
               [branch("local:joint","both depositions",8)])
        # A blind protocol probe may use a fixed policy. Claims of model autonomy require live runs.
        nodes = [node(scopes[e],"local:ca" + e.lower(),material[e],"common_ca") for e in ENTRIES]
        return decide(ops + nodes, [], "Obtain a reference selection for both coordinate deposits.", watches([n["node"] for n in nodes]))
    for n in done:
        ref = n["result_ref"]
        if ref not in observed:
            if ("gx_read_artifact", ref) not in answers:
                return send("gx_read_artifact", {"ref":ref})
            observed[ref] = answers["gx_read_artifact",ref]
    current = {(n["commission"]["inputs"][0], n["commission"]["parameters"]["mask"]):n for n in brief["nodes"]}
    additions = []
    for ref, result in observed.items():
        n, = [n for n in done if n["result_ref"] == ref]
        source = n["commission"]["inputs"][0]
        entry = result["entry"]
        next_mask = "common_backbone" if result["mask"] == "common_ca" else "ordered_backbone"
        if (result["mask"] in ("common_ca","common_backbone") and result["coverage_fraction"] >= .60
                and (source,next_mask) not in current):
            additions.append((ref,node(n["branch_id"],"local:" + next_mask + entry.lower(),source,next_mask)))
    if additions:
        return decide([n for _,n in additions], [ref for ref,_ in additions],
                      "The returned coordinates have sufficient coverage for a complementary atom selection.",
                      watches([n["node"] for _,n in additions] + [n["node_id"] for n in brief["nodes"] if not n["result_ref"]]))
    waiting = [n["node_id"] for n in brief["nodes"] if not n["result_ref"] and n["status"] not in ("FAILED","CANCELLED","BLOCKED")]
    if waiting:
        return decide([],list(observed), "Collect the outcomes of the already commissioned fits.", watches(waiting))
    if not observed:
        raise ValueError("No scientific observation reached the decision peer")
    refs = list(observed)
    staged = answers.get(("gx_stage_claim",None))
    if not staged:
        statements = pair_results(list(observed.values()))
        return send("gx_stage_claim", {"level":"mixed",
                   "statement": json.dumps({"statements":statements}, separators=(",",":")),
                   "scope":"Only the measured chain pairs in the two deposited crystal models.",
                   "criteria":"Coverage at least 60 positions and 60%; geometric margins 0.5° and 0.15 angstrom.",
                   "limitations":"Shared coordinates and deposition study; no population inference.",
                   "rationale":"Reported labels follow the measured mask-specific coverage and pair differences.",
                   "evidence_refs":refs})
    return decide([{"op":"close_run","claim_refs":[staged["claim_ref"]],
                    "reason":"Compared measured chain pairs under the registered scope.",
                    "unresolved":["Population and functional effects remain outside this comparison."]}],
                  refs,"The measured comparisons delimit support in the two deposits.",
                  [{"event":"goal_changed","subject_ref":"run"}])
