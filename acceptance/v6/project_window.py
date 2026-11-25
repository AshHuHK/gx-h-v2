"""Build the Agent Kit projection from GX snapshots and the matching native rollout."""
from pathlib import Path

import native_witness
import verify as v

HERE = Path(__file__).resolve().parent
TO_STATUS = {"CREATED":"queued", "SUBMITTED":"queued", "RUNNING":"running",
             "SUCCEEDED":"completed", "FAILED":"failed", "CANCELLED":"cancelled"}


class Archive:
    def __init__(self, directory):
        self.root = directory
        (self.root / "objects").mkdir()

    def put(self, value):
        data = value if isinstance(value, bytes) else v.stable(value)
        digest = v.sha(data)
        name = "objects/" + digest
        path = self.root / name
        if not path.exists():
            path.write_bytes(data)
        return {"file": name, "sha256": digest}


class Capture:
    def __init__(self, path):
        self.path = path
        self.data = v.parse((path / "capture.json").read_bytes())
        v.need(self.data["schema"] == "gx.v6.observer/1", "unknown capture")
        self.rows = self.data["rows"]
        self.run = self.data["run_id"]
        self.decisions = v.index(self.rows["v6_decisions"], "decision_ref")
        self.artifacts = v.index(self.rows["artifacts"], "artifact_ref")
        self.nodes = v.index(self.rows["v6_nodes"], "node_id")
        self.branches = {}
        for d in self.decisions.values():
            receipt, original = v.parse(d["receipt_json"]), v.parse(d["request_json"])["decision"]
            for op in receipt["resolved_ops"]:
                spec = op["spec"]
                if spec["op"] == "open_branch":
                    self.branches[spec["branch"]] = original["ops"][op["source_index"]]["branch"].removeprefix("local:")

    def artifact(self, ref):
        row = self.artifacts[ref]
        raw = (self.path / "artifacts" / row["sha256"]).read_bytes()
        v.need(v.sha(raw) == row["sha256"] and len(raw) == row["bytes"], "corrupt captured material")
        return raw

    def graph(self):
        graphs = self.rows["v6_graph_versions"]
        if not graphs:
            return None
        head = max(graphs, key=lambda g: g["version"])
        v.need(v.digest(v.parse(head["graph_json"])) == head["sha256"], "graph head digest mismatch")
        return {"version":head["version"], "sha256":head["sha256"]}

    def claim(self, hypothesis):
        if hypothesis is None:
            return None
        heads = [h for h in self.rows["hypotheses"] if h["hypothesis_id"] == hypothesis]
        if not heads:
            return None
        h, = heads
        r, = [r for r in self.rows["hypothesis_revisions"] if
              (r["hypothesis_id"],r["revision"]) == (hypothesis,h["head_revision"])]
        record = v.parse(r["document_json"])
        v.need(v.digest(record) == r["sha256"] and "claim_revision_id" in record, "unverified scientific claim")
        return record


def head_id(head):
    return "no-graph" if head is None else str(head["version"]) + ":" + head["sha256"]


def job_view(raw, capture, arch, logical, chosen_results):
    intent = v.parse(raw["submit_intent_json"])
    node_id = intent["request"].get("binding", {}).get("node_id")
    node = capture.nodes.get(node_id)
    v.need(node is not None, "unbound Job in witness window")
    op, jid = node["operation_key"], raw["job_id"]
    # JobService adds its resolved lease/step to its provenance after hashing the request.
    request = dict(intent["request"])
    if v.digest(request) != intent["request_sha256"]:
        request.pop("lease_ref", None)
        if request.get("step_index") is None:
            request.pop("step_index", None)
    v.need(v.digest(request) == intent["request_sha256"], "JobService request hash mismatch")
    req = arch.put(request)
    branch = node["branch_id"]
    projected = {"job_id":jid, "operation_key":op,
                 "branch":capture.branches.get(branch,branch) if logical else branch,
                 "decision_id":node["decision_ref"], "request":req, "request_sha256":req["sha256"],
                 "status":TO_STATUS[raw["status"]]}
    if v.PDB_SHA in intent["input_sha256"].values():
        projected["input_sha256"] = v.PDB_SHA
    if projected["status"] in v.FINISHED:
        outcome = v.parse(raw["outcome_json"])
        result = chosen_results.get(jid, node["result_ref"])
        if result:
            out, = [t for t in outcome["outputs"] if t["artifact_ref"] == result]
            artifact = capture.artifacts[result]
            v.need(artifact["job_id"] == jid and artifact["sha256"] == out["sha256"], "output belongs to another Job")
            projected["result"] = arch.put(capture.artifact(result))
        else:
            v.need(projected["status"] != "completed", "completed Job has no registered output")
            projected["result"] = arch.put(outcome)
    return projected


def summary(capture, arch, logical, initial, rank, hypothesis, chosen_results):
    run, = capture.rows["runs"]
    goal, = capture.rows["v6_goals"]
    budget, = [b for b in capture.rows["budgets"] if b["budget_ref"] == run["budget_ref"]]
    cap = v.parse(budget["limits_json"])["job_submissions"]
    jobs, ledger = [], []
    reservations = v.index(capture.rows["budget_reservations"], "reservation_ref")
    for raw in capture.rows["jobs"]:
        job = job_view(raw, capture, arch, logical, chosen_results)
        jobs.append(job)
        r = reservations[raw["reservation_ref"]]
        event, = [e for e in capture.rows["events"] if e["kind"] == "budget.reserved"
                  and v.parse(e["payload_json"])["reservation_ref"] == r["reservation_ref"]]
        payload = v.parse(event["payload_json"])
        v.need(r["subject_kind"] == "job" and r["subject_id"] == job["job_id"] and
               payload["for"] == ["job",job["job_id"]] and
               v.parse(r["amounts_json"])["job_submissions"] == payload["amounts"]["job_submissions"] == 1,
               "Job submission not backed by its reservation")
        ledger.append({"ledger_id":r["reservation_ref"], "job_id":job["job_id"],
                       "operation_key":job["operation_key"], "units":1})
    claims = capture.claim(hypothesis)
    scopes = {"run:" + capture.run:goal["status"].lower()}
    scopes.update({"branch:" + b["branch_id"]:b["status"].lower() for b in capture.rows["v6_branches"]})
    return {"run_id":capture.run, "capture_order":rank, "head":head_id(capture.graph()),
            "logical_head":initial["graph_version"], "base_digest":v.digest(initial),
            "science":claims["science"] if claims else {"level":"open","chain_pairs":[]},
            "scope_states":scopes, "jobs":jobs, "submission_ledger":ledger, "spent_job_submissions":len(ledger),
            "job_submission_limit":cap, "resource_ledger":arch.put(capture.data["costs"]),
            "boot_id":f'{capture.data["kernel_boot"]}/observer-{capture.data["observer_pid"]}',
            **({"science_receipt":arch.put(claims)} if claims else {})}


def pairs(request):
    """Find explicit chain selections in the admitted request, retaining their source pointers."""
    result = {}
    def walk(value, path):
        if isinstance(value,str) and value in {"A/B","A/C","B/C"}:
            result[path] = value
        elif isinstance(value,list):
            if len(value) == 2 and all(isinstance(s,str) and s in {"A","B","C"} for s in value) and value[0]!=value[1]:
                result[path] = "/".join(sorted(value))
            else:
                for i,item in enumerate(value):
                    walk(item, path+"/"+str(i))
        elif isinstance(value,dict):
            for k,item in value.items():
                walk(item, path+"/"+k.replace("~","~0").replace("/","~1"))
    walk(request,"")
    return {"pair_pointers":list(result), "chain_pairs":sorted(set(result.values()))}


def material_documents(capture):
    row, = capture.rows["v6_goals"]
    for ref in v.parse(row["materials_json"]):
        data = capture.artifact(ref)
        try:
            yield ref, v.parse(data)
        except (ValueError, UnicodeError):
            pass


def packet_view(mode, spec, native, before, decision, arch):
    goal_row, = before.rows["v6_goals"]
    brief = v.parse(native["brief"].split("\n", 1)[1])
    v.need(brief["goal"] == goal_row["goal"] and brief["head"] == before.graph(), "goal/base changed before turn")
    grants = brief["evidence_refs"]
    observations, results = [], {}
    if mode == "controls":
        v.need(spec["packet_id"] in {"p17","p42","p63"}, "unknown control packet")
        goal, initial, packet = [v.parse((HERE / "inputs" / (stem + ".json")).read_bytes())
                                 for stem in ("goal","initial",spec["packet_id"])]
        materials = [d for _,d in material_documents(before)]
        v.need(goal in materials and initial in materials and brief["goal"] == goal["question"],
               "matched prestate/Goal were not registered as actual run materials")
        v.need(before.graph()["version"] == 1, "control did not start from its single accepted seed graph")
        alias = {}
        for ref, key in spec["evidence_map"].items():
            obs, = [o for o in packet["observations"] if o["id"] == key]
            value = v.parse(before.artifact(ref))
            v.need(value == obs["measurement"], "injected material differs from its registered packet")
            # A scoped observation can be cited by the immutable artifact or by its event-source ID.
            refs = [ref] + [o["id"] for o in brief["observations"] if o["artifact_ref"] == ref]
            cited = [r for r in refs if r in native["intent"]["evidence_refs"]]
            native_ref = (cited or refs)[0]
            alias[native_ref] = key
            observations.append((native_ref, value))
    else:
        goal = {"id":before.run, "question":goal_row["goal"], "version":goal_row["goal_version"]}
        initial = {"goal_id":goal["id"], "goal_digest":v.digest(goal), "graph_version":head_id(before.graph()),
                   "context_ref":decision["context_ref"]}
        chosen = spec.get("observations")
        if chosen is None:
            chosen = [{"evidence_ref":ref} for ref in native["intent"]["evidence_refs"]
                      if ref in before.artifacts and before.artifacts[ref]["job_id"]]
        v.need(chosen, "no cited source measurement in the pre-decision capture")
        coords = {ref for ref in v.parse(goal_row["materials_json"]) if before.artifacts[ref]["sha256"] == v.PDB_SHA}
        v.need(len(coords) == 1, "2RE9 coordinates were not uniquely granted for this run")
        packet = {"packet_id":decision["context_ref"], "base_digest":v.digest(initial),
                  "kind":"observed_structure", "coordinates":arch.put(before.artifact(coords.pop())), "observations":[]}
        alias = {}
        for selection in chosen:
            ref, pointer = selection["evidence_ref"], selection.get("pointer", "")
            a = before.artifacts[ref]
            raw = v.parse(before.artifact(ref))
            value = v.pointer(raw, pointer)
            # GX adds source IDs/manifest to some parsed products. A window locator may select a
            # nested measured object; its value still has to be present in the official turn.
            v.need(isinstance(value,(dict,list)) and value and a["job_id"], "not a measured structure object")
            obs = {"id":ref, "source_job_id":a["job_id"], "result_sha256":a["sha256"],
                   "pointer":pointer, "value":value}
            packet["observations"].append(obs)
            results[a["job_id"]] = ref
            alias[ref] = ref
            observations.append((ref,value))
    for ref, value in observations:
        v.need(ref in grants and native_witness.seen(native, ref, value),
               "source-scoped numeric evidence absent from official turn: " + ref)
    presented = {"goal_digest":v.digest(goal), "base_digest":v.digest(initial), "packet_digest":v.digest(packet),
                 "evidence_map":alias, "visible_observations":[{"evidence_ref":r,"value":x} for r,x in observations]}
    return goal, initial, packet, presented, results


def prove_resume(window, before, settled, reopened):
    seq = [s.data for s in (before,settled,reopened)]
    v.need(len({(s["kernel_boot"], *s["db_file_id"]) for s in seq}) == 1 and
           seq[0]["monotonic_ns"] < seq[1]["monotonic_ns"] < seq[2]["monotonic_ns"],
           "out-of-order captures or different SQLite database")
    args, output = window["argv"], window["stdout"]
    valid_exit = window["exit_code"] == 0 or (window["exit_code"] == 75 and output.get("driver") == "time_limit")
    v.need(Path(args[0]).name == "gx-p0" and args[1] == "resume" and
           args[args.index("--run-id")+1] == settled.run and output.get("run_id") == settled.run and valid_exit and
           type(window["pid"]) is int and window["pid"] > 0 and seq[1]["captured_ns"] <= window["started_ns"]
           <= window["finished_ns"] <= seq[2]["captured_ns"], "no matching public cold-resume invocation")


def stop_events(archive, before, settled, decision, start, ops):
    refs = []
    for op in ops:
        if op["op"] not in ("stop_branch","close_run"):
            continue
        kind = "branch" if op["op"] == "stop_branch" else "run"
        target = op.get("branch", settled.run)
        key = kind + ":" + target
        dg = v.digest(op)
        row, = [e for e in settled.rows["events"] if e["kind"] in {"v6.branch.stopped","v6.run.closed"}
                and (e["subject_kind"],e["subject_id"]) == (kind,target)
                and v.parse(e["payload_json"]).get("decision_id") == decision]
        v.need(v.parse(row["payload_json"])["op_digest"] == dg and row["event_id"] not in
               {e["event_id"] for e in before.rows["events"]}, "wrong operation for stop")
        # Project the *whole decision window*, from the captured open scope through
        # its persisted drain state to the native terminal event. Include both sources.
        parked, = [r for r in (settled.rows["v6_branches"] if kind == "branch" else settled.rows["v6_goals"])
                   if r.get("branch_id", r.get("run_id")) == target]
        v.need(parked["stop_decision_ref"] == decision and parked["stop_op_digest"] == dg and
               parked["status"] == row["new_state"] and row["prev_state"] in {"STOPPING","CLOSING"} and
               start["scope_states"][key] == "open", "incomplete native stop transition")
        requested, = [e for e in settled.rows["events"] if e["kind"] in
                     {"v6.branch.stop_requested","v6.run.close_requested"} and
                     (e["subject_kind"],e["subject_id"]) == (kind,target) and
                     v.parse(e["payload_json"]).get("decision_id") == decision]
        v.need(requested["new_state"] == row["prev_state"] and
               requested["prev_state"].lower() == start["scope_states"][key] and
               v.parse(requested["payload_json"])["op_digest"] == dg and requested["event_id"] < row["event_id"],
               "stop request and terminal transition do not form a durable chain")
        refs.append(archive.put({**row, "prev_state":requested["prev_state"],
                    "raw_start_event":archive.put(requested), "raw_terminal_event":archive.put(row),
                    "durable_scope":archive.put(parked)}))
    return refs


def resolve_window(archive, mode, path):
    spec = v.parse(path.read_bytes())
    def location(key):
        return path.parent / spec[key]
    before, settled, reopened = [Capture(location(k)) for k in ("before","settled","reopened")]
    rid, did = before.run, spec["decision_ref"]
    v.need(settled.run == reopened.run == rid and did not in before.decisions and
           set(settled.decisions)-set(before.decisions) == {did} and set(settled.decisions) == set(reopened.decisions),
           "ambiguous window: history must contain exactly one newly accepted decision")
    decision = settled.decisions[did]
    v.need(reopened.decisions[did] == decision, "decision changed across restart")
    cold = v.parse(location("resume_receipt").read_bytes())
    prove_resume(cold, before, settled, reopened)
    receipt = v.parse(decision["receipt_json"])
    v.need(receipt["base"] == before.graph() and receipt["result_head"] == settled.graph(), "decision base/head mismatch")
    native = native_witness.rollout(location("rollout"), decision, settled.rows)
    args, body = native["call"], native["intent"]
    call, = [r for r in settled.rows["p0_v6_native_calls"]
             if (r["thread_id"],r["turn_id"],r["call_id"]) == tuple(decision[k] for k in ("thread_id","turn_id","call_id"))]
    official = v.parse(call["request_json"])
    v.need(official["arguments"] == v.parse(args["arguments"]) and official["tool"] == "gx_decide_revision",
           "rollout and official callback parameters disagree")
    goal, initial, packet, presented, results = packet_view(mode, spec, native, before, decision, archive)
    claim_ids = {r["hypothesis_id"] for r in receipt.get("claim_revisions", [])}
    if not claim_ids:
        claim_ids = {h["hypothesis_id"] for h in before.rows["hypotheses"]}
    hypothesis = spec.get("hypothesis_id")
    if hypothesis:
        v.need(hypothesis in claim_ids, "unknown scientific projection")
    else:
        v.need(len(claim_ids) <= 1, "select an actual hypothesis_id from this decision")
        hypothesis = next(iter(claim_ids),None)
    start, end, cold_state = [summary(c,archive,mode=="controls",initial,i,hypothesis,results)
                              for c,i in ((before,0),(settled,3),(reopened,4))]
    frozen = v.parse(native["brief"].split("\n",1)[1])
    start["prior_evidence_refs"] = frozen["evidence_refs"]
    products = []
    for ref in set(body["evidence_refs"]) - set(frozen["evidence_refs"]):
        made = native["products"][ref]
        call_id = made["call_id"]
        prior_call, = [c for c in settled.rows["p0_v6_native_calls"] if
                       (c["thread_id"],c["turn_id"],c["call_id"]) == (decision["thread_id"],decision["turn_id"],call_id)]
        returned, = [c for c in settled.rows["p0_root_calls"] if
                     (c["thread_id"],c["turn_id"],c["call_id"]) == (decision["thread_id"],decision["turn_id"],call_id)]
        products.append({"ref":ref, "call":archive.put(v.parse(prior_call["request_json"])),
                         "receipt":archive.put(returned), "output":archive.put(made["frame"]),
                         "line":made["line"], "bytes":archive.put(settled.artifact(ref)),
                         **({"nested":archive.put(made["native_item"])} if "native_item" in made else {})})

    # A cold resume is proven by the actual distinct GX invocation, even when observer captures
    # are called in the same orchestration process.
    cold_state["boot_id"] = f'gx-resume-{cold["pid"]}-{cold["started_ns"]}'
    for item in (cold_state,end):
        item["decision_id"] = did
    end["stop_receipts"] = stop_events(archive,before,settled,did,start,body["ops"])
    prior = {j["job_id"] for j in start["jobs"]}
    dispatches = []
    for job in end["jobs"]:
        if job["job_id"] in prior:
            continue
        dispatched, = [d for d in receipt["dispatches"] if d["operation_key"] == job["operation_key"]]
        op, = [d for d in receipt["resolved_ops"] if d["op_digest"] == dispatched["op_digest"] and
               d["spec"].get("node") == dispatched["node_id"]]
        original = body["ops"][op["source_index"]]
        v.need(v.digest(original) == op["op_digest"], "dispatch differs from native call")
        dispatches.append({"node":original["node"], "node_digest":op["op_digest"], "branch":job["branch"],
                           "request_sha256":job["request_sha256"], "operation_key":job["operation_key"],
                           **pairs(v.document(archive.root,job["request"]))})
    turn = {"run_id":rid,"turn_id":decision["turn_id"],"thread_id":decision["thread_id"],"decision_id":did,
            "capture_order":1,"base_head":start["head"],"packet_digest":v.digest(packet),
            "tool_arguments":dict(archive.put(official),pointer="/arguments/decision"),
            "presented_evidence":archive.put(presented), "native_user_brief":archive.put(native["brief"].encode()),
            "native_call_item":archive.put(args),"native_reads":archive.put(native["reads"]),
            "native_context":archive.put(native["context"]), "native_session":archive.put(native["session"]),
            "same_turn_products":products, "decision_line":native["call_line"]}
    commit = {"run_id":rid,"turn_id":decision["turn_id"],"decision_id":did,"capture_order":2,
              "base_head":start["head"],"result_head":end["head"],"intent_digest":receipt["draft_sha256"],
              "dispatches":dispatches, "gx_receipt":archive.put(receipt)}
    access = v.parse(native["root"]["effective_profile_json"])["research_access"]
    settings = {"model":native["context"]["model"], "effort":native["context"]["effort"],
                "provider":native["session"]["model_provider"], "host_version":native["session"]["cli_version"],
                "available_tools":access["dynamic_tools"], "thread_config":access["thread_config"],
                "instructions":access["developer_instructions_sha256"],
                "capability_pins":v.parse(before.rows["v6_goals"][0]["capabilities_json"]),
                "limits":v.parse(before.rows["v6_goals"][0]["limits_json"])}
    records = {"goal":goal,"initial":initial,"packet":packet,"profile":settings,"before":start,"turn":turn,
               "commit":commit,"settled":end,"reopened":cold_state,"cold_resume_invocation":cold,
               "native_access":access}
    return {"run_id":rid, **{k:archive.put(x) for k,x in records.items()}}


def make_bundle(out, mode, windows):
    arch = Archive(out)
    rows = [resolve_window(arch,mode,Path(p)) for p in windows]
    suite = v.parse((HERE / "reviewer/suite.json").read_bytes())
    projections, case_results = [], []
    for row in rows:
        try:
            projections.append(v.verify_run(out,row,suite,mode))
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            case_results.append({"run_id":row["run_id"], "status":"FAIL", "reason":str(exc)})
        else:
            case_results.append({"run_id":row["run_id"], "status":"PASS"})
    group_error = None
    if mode == "controls":
        if len(rows) != 3:
            group_error = "expected three captured controls"
        elif len(projections) == 3:
            same = ({r[0] for r in projections} == set(suite["packet_digests"])
                    and all(len({r[i] for r in projections}) == 3 for i in range(5))
                    and len({r[5] for r in projections}) == 1)
            effects = {r[0]:r[6] for r in projections}
            if not same:
                group_error = "independent controls have different initial conditions"
            elif effects["p17"] == effects["p42"]:
                group_error = "opposed inputs have equivalent effects"
    elif len(rows) != 1:
        group_error = "expected one observed witness"
    raw = v.stable({"mode":mode,"version":6,"runs":rows})
    (out / "bundle.json").write_bytes(raw)
    passed = group_error is None and all(row["status"] == "PASS" for row in case_results)
    grading = {"consistent":passed,"cases":case_results,"group_error":group_error}
    (out / "projection-verdict.json").write_bytes(v.stable(grading))
    return {"mode":mode,"run_ids":[r["run_id"] for r in rows],"bundle_sha256":v.sha(raw),**grading}
