"""Read-only evidence export for the preregistered yeast and hydrology studies."""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

HERE = Path(__file__).resolve().parent


def pick(row, keys):
    return {key: row[key] for key in keys.split() if key in row.keys()}


def values(item, key):
    if isinstance(item, dict):
        for name, value in item.items():
            if name == key:
                yield value
            yield from values(value, key)
    elif isinstance(item, list):
        for child in item:
            yield from values(child, key)


def strings(item):
    if isinstance(item, str):
        yield item
    elif isinstance(item, dict):
        for value in item.values():
            yield from strings(value)
    elif isinstance(item, list):
        for value in item:
            yield from strings(value)


def rollouts(work):
    """Export only host turn identity, timing and model metadata."""
    result = {}
    for file in (work / "codex/sessions").rglob("*.jsonl"):
        thread = provider = version = None
        with file.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break  # the official host can still be writing its final line
                    raise
                p = record.get("payload", {})
                if record["type"] == "session_meta":
                    thread, provider, version = p.get("id"), p.get("model_provider"), p.get("cli_version")
                elif thread and record["type"] == "turn_context":
                    result.setdefault((thread, p.get("turn_id")), {
                        "provider": provider, "version": version, **pick(p, "model effort"),
                        "started": record["timestamp"]})
                elif thread and record["type"] == "event_msg" and p.get("type") in (
                        "turn_aborted", "task_complete", "turn_complete"):
                    turn = result.get((thread, p.get("turn_id")))
                    if turn:
                        turn.update(ended=record["timestamp"], terminal=p["type"])
    return result


def bytes_for(db, state, ref, expected):
    row = db.execute("SELECT namespace,rel_path,sha256 FROM artifacts WHERE artifact_ref=? AND published=1",
                     (ref,)).fetchone()
    if not row or row["sha256"] != expected:
        return None
    base = (state / "artifacts").resolve()
    path = (base / row["namespace"] / row["rel_path"]).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return None
    raw = path.read_bytes()
    return raw if hashlib.sha256(raw).hexdigest() == expected else None


def jobs(db, state, run):
    rows = db.execute("""SELECT j.*,x.admission_json,x.admission_sha256,x.completion_json,x.completion_sha256
        FROM jobs j LEFT JOIN p0_execution_manifests x USING(job_id) WHERE j.run_id=? ORDER BY j.rowid""", (run,))
    records = []
    for r in rows:
        a, c = json.loads(r["admission_json"] or "{}"), json.loads(r["completion_json"] or "{}")
        item = {**pick(r, "job_id status node_id result_ref started_at finished_at admission_sha256 completion_sha256"),
                "method": a.get("capability"), "inputs": a.get("inputs", []),
                "module_binding": pick(a.get("module_capability") or {}, "event_id scope revision portable_sha256 plugin"),
                "parameters": a.get("parameters"), "outputs": [],
                "nodes": [dict(n) for n in db.execute("""SELECT n.node_id,n.result_ref,n.status,n.decision_ref,
                    a.reused_from FROM v6_nodes n LEFT JOIN v6_job_attempts a USING(node_id)
                    WHERE n.run_id=? AND n.job_id=? ORDER BY n.rowid""",(run,r["job_id"]))]}
        for f in c.get("outputs", []):
            entry = pick(f, "output_name artifact_ref sha256 kind")
            content = bytes_for(db, state, f["artifact_ref"], f["sha256"])
            entry["verified_bytes"] = content is not None
            if content is not None and f["output_name"] == "parent-records.json":
                obj = json.loads(content)
                entry["readout"] = {**pick(obj,"schema query counts exact_reaction limitations"),
                    "assay_coverage": {k:v for k,v in obj.get("assay_coverage", {}).items() if k != "examples"},
                    "entries": [pick(p, "accession name sequence_version assay_row_counts") for p in obj.get("parents", [])]}
            elif content is not None and f["output_name"] in ("paired.json", "marginal.json"):
                entry["readout"] = json.loads(content)
            item["outputs"].append(entry)
        records.append(item)
    return records


def events(db, run):
    kinds = ("v6.node.settled", "v621.material.assigned", "v62.module.wake", "v62.module.method_selected",
             "v62.module.reported", "v6.route.revised", "v6.run.closed", "v6.research.successor_opened")
    rows = db.execute("SELECT event_id,kind,ts,actor,subject_id,payload_json FROM events WHERE run_id=? ORDER BY event_id",
                      (run,))
    result = []
    for row in rows:
        k = row["kind"]
        if k not in kinds and not k.startswith(("research.topic.", "research.join.", "research.recipe.")):
            continue
        p = json.loads(row["payload_json"])
        if k == "v62.module.method_selected":
            p = {**pick(p, "action method revision"), "provider": p.get("descriptor", {}).get("plugin")}
        if k == "v6.route.revised":
            g = db.execute("SELECT graph_json,decision_ref FROM v6_graph_versions WHERE run_id=? AND version=?",
                           (run,p.get("graph_version"))).fetchone()
            if g and p.get("decision_ref") == g["decision_ref"]:
                route = json.loads(g["graph_json"]).get("intents",{}).get(row["subject_id"],{})
                p["frozen_route"] = pick(route,"intent phase binding_state bindings node_ids module_instance_id")
        result.append({**pick(row, "event_id kind ts actor subject_id"), "data": p})
    return result


def delivered_content(packet):
    evidence = []
    for field in ("module_reports","module_report_index"):
        for r in packet.get(field, []):
            if not isinstance(r,dict) or not r.get("report_ref"):
                continue
            fields = [k for k in ("researcher_message","findings","endpoint_rows","source_quotations",
                                  "qualitative_notes","counterevidence") if r.get(k)]
            if fields:
                evidence.append({"ref":r["report_ref"], "fields":fields,"truncated":r.get("truncated")})
    for group in values(packet,"endpoint_rows"):
        if not isinstance(group,list):
            continue
        for row in group:
            if isinstance(row,dict) and row.get("source_ref") and ("value" in row or "v" in row):
                evidence.append({"ref":row["source_ref"],"fields":["verified_endpoint"]})
    return evidence


def calls(db, run, host, known, documents):
    trace = []
    rows = db.execute("""SELECT n.rowid AS seq,n.*,c.result_json,t.scope,r.task_id FROM p0_v6_native_calls n
        JOIN p0_root_calls c USING(thread_id,turn_id,call_id) JOIN p0_roots r USING(thread_id)
        JOIN v6_decision_tasks t USING(task_id) WHERE r.run_id=? ORDER BY n.rowid""", (run,))
    for r in rows:
        req, out = json.loads(r["request_json"]), json.loads(r["result_json"])
        if not isinstance(out, dict):
            continue
        name = req["tool"]
        args = req["arguments"]
        official = host.get((r["thread_id"],r["turn_id"]), {}).get("provider") == "openai"
        item = {**pick(r, "seq thread_id turn_id call_id task_id scope"), "official": official, "tool": name}
        if name == "gx_decide_revision":
            draft = args.get("decision",{})
            item["proposal"] = {"rationale":draft.get("rationale"),"evidence_refs":draft.get("evidence_refs",[]),
                                "ops":[pick(op,"op capability parameters inputs") for op in draft.get("ops",[])],
                                "outcome":out.get("outcome")}
        if "decision_refusal" in out:
            item["refusal"] = pick(out["decision_refusal"], "code message side_effect_status")
        elif name == "gx_read_source":
            item.update(arguments=pick(args,"source_ref query segment_id"), **pick(out, "receipt_id text_sha256 segment_id source_id source_ref"))
            if isinstance(out.get("text"), str):
                item["returned_text_sha256"] = hashlib.sha256(out["text"].encode()).hexdigest()
        elif name == "gx_read_artifact":
            item["ref"] = args.get("ref")
            # A metadata card can return the identifier while delivering no evidence.
            content_hash = hashlib.sha256(json.dumps(out, ensure_ascii=False, sort_keys=True,
                                                     separators=(",", ":")).encode()).hexdigest()
            item["content_digest"] = content_hash
            item["has_content"] = (item["ref"] in documents and documents[item["ref"]] == content_hash
                                   or bool(out.get("endpoint_rows")) or bool(out.get("content")))
            item["whole_verified_document"] = documents.get(item["ref"]) == content_hash
        elif name == "gx_query_catalog":
            item["arguments"] = pick(args, "kind reference category query")
            item["candidates"] = [x.get("reference") for x in out.get("items", []) if "reference" in x]
            item.update(pick(out,"source_ref reference status total"))
        elif name in ("gx_open_module","gx_select_capability","gx_submit_module_report"):
            item["arguments"] = pick(args,"module_id module_version focus researcher method_choices capabilities method action revision")
            item.update(pick(out,"scope event_id module_instance_id report_ref wake_event_id revision task_id"))
            if name == "gx_open_module":
                item["eligible_methods"] = list(out.get("effective_grant",{}).get("method_options",{}))
            item["caller_scope"] = r["scope"]
        elif name in ("gx_save_recipe","gx_instantiate_recipe","gx_watch_join","gx_send_research_hint"):
            item["arguments"] = pick(args,"key topic action parent_hint_ids recipe reference version materials bindings")
            item.update(pick(out,"key topic outcome_event registration_event event_id reference recipe digest state"))
            item["delivered_hints"] = [e for group in values(out, "entries") if isinstance(group,list)
                                       for e in group if isinstance(e, dict) and e.get("event_id")]
        elif name not in ("gx_task_context","gx_decide_revision","gx_link_core_material"):
            continue
        refs = sorted(known.intersection(strings(out))) if name == "gx_task_context" else []
        if refs:
            item["context_refs"] = refs
        if name == "gx_task_context":
            item["delivered_content"] = delivered_content(out)
            item["delivered_hints"] = [e for group in values(out, "entries") if isinstance(group,list)
                                       for e in group if isinstance(e, dict) and e.get("event_id")]
        trace.append(item)
    return trace


def turns(db, run, host, refs):
    rows = db.execute("""SELECT d.rowid AS delivery_seq,d.thread_id,d.turn_id,d.text_sha256,d.event_id,r.brief,r.task_id,t.scope,
        c.event_cursor FROM p0_root_deliveries d JOIN p0_roots r ON r.thread_id=d.thread_id
        JOIN v6_decision_tasks t ON t.task_id=r.task_id JOIN research_contexts c USING(context_ref)
        WHERE d.run_id=? AND d.method='turn/start' AND d.state='acknowledged' ORDER BY d.rowid""",(run,))
    results = []
    for row in rows:
        brief = row["brief"]
        matched = hashlib.sha256(brief.encode()).hexdigest() == row["text_sha256"]
        packet = json.loads(brief.split("\n",1)[1]) if matched else {}
        results.append({**pick(row,"thread_id turn_id scope task_id event_cursor text_sha256 event_id"),
                        "official": host.get((row["thread_id"], row["turn_id"])), "full_brief_acknowledged": matched,
                        "delivery_seq":row["delivery_seq"], "delivered_refs": sorted(refs.intersection(strings(packet))), "delivered_content":delivered_content(packet),
                        "joins": packet.get("research_joins", packet.get("joins", [])),
                        "topics": packet.get("research_topics", []), "direct_peer_hints":packet.get("peer_hints", [])})
    return results


def decisions(db, run):
    rows = db.execute("""SELECT d.*,n.rowid AS seq FROM v6_decisions d LEFT JOIN p0_v6_native_calls n
        USING(thread_id,turn_id,call_id) WHERE d.run_id=? ORDER BY d.rowid""", (run,))
    result = []
    for r in rows:
        req, rec = json.loads(r["request_json"])["decision"], json.loads(r["receipt_json"])
        result.append({**pick(r,"seq decision_ref thread_id turn_id call_id task_id context_ref"),
                       **pick(rec,"event_id outcome scope result_head task_status"),
                       **pick(req,"rationale evidence_refs"), "operations": [pick(i["spec"], "op node branch intent reason")
                           for i in rec.get("resolved_ops",[]) if "spec" in i],
                       "dispatched": rec.get("dispatches",[])})
    return result


def compare_decisions(db, run, goal, verdicts, delivery, actions, reports, output_refs, derived):
    source_by_report = {r["report_ref"]:set(r["cited_scientific_refs"]) for r in reports
                        if r["verified_bytes"] and r["official_worker_submission"]}
    result = []
    for v in verdicts:
        if v["scope"] != "run" or v["task_status"] != "ACCEPTED":
            continue
        official = [t for t in delivery if t["thread_id"] == v["thread_id"] and t["turn_id"] == v["turn_id"]
                    and (t.get("official") or {}).get("provider") == "openai"]
        if not official:
            continue
        frozen = [t for t in delivery if t["task_id"] == v["task_id"] and t["full_brief_acknowledged"]
                  and t["thread_id"] == v["thread_id"] and t["delivery_seq"] <= official[-1]["delivery_seq"]]
        delivered = {r["ref"] for t in frozen for r in t["delivered_content"]}
        reads = [c for c in actions if c["thread_id"] == v["thread_id"] and c["official"]
                 and c["seq"] < v["seq"] and c["scope"] == "run"]
        delivered.update(c["ref"] for c in reads if c["tool"] == "gx_read_artifact" and c["has_content"])
        delivered.update(r["ref"] for c in reads for r in c.get("delivered_content", []))
        cited = set(v["evidence_refs"])
        linked = sorted(delivered & cited)
        direct = sorted(output_refs.intersection(linked))
        derived_used = {ref: derived[ref] for ref in linked if ref in derived}
        reports_used = sorted(r for r in linked if r in source_by_report)
        effects = []
        if v["decision_ref"] == goal["stop_decision_ref"] and goal["status"] == "CLOSED":
            effects.append({"kind":"closed_goal","run":run})
        for n in db.execute("SELECT node_id,job_id,status FROM v6_nodes WHERE run_id=? AND decision_ref=?",
                            (run,v["decision_ref"])):
            effects.append({"kind":"node",**dict(n)})
        if v["operations"] and v.get("result_head"):
            row = db.execute("SELECT version,sha256 FROM v6_graph_versions WHERE run_id=? AND version=? AND sha256=? AND decision_ref=?",
                             (run, v["result_head"]["version"],v["result_head"]["sha256"],v["decision_ref"])).fetchone()
            if row:
                effects.append({"kind":"committed_graph",**dict(row),
                                "ops":[o["op"] for o in v["operations"]]})
        result.append({**v,"read_or_delivered_and_cited":linked,
                       "actual_outputs_cited": direct,"derived_evidence":derived_used,"reports_cited":reports_used,
                       "report_backed_outputs":sorted({ref for r in reports_used for ref in source_by_report[r]} & output_refs),
                       "effects": effects, "semantic_fit":"requires scientific reading of rationale and evidence"})
    return result


def proofset(case, all_data):
    e = all_data
    dbjobs = [j for j in e["jobs"] if (j["method"] or {}).get("id") == "yeast-archive-lookup" and j["status"] == "SUCCEEDED"]
    own_decisions = [d for d in e["lead_decisions"] if d["effects"] and
                     (d["actual_outputs_cited"] or d["report_backed_outputs"] or d.get("derived_evidence"))]
    if case == "yeast-followup":
        reg = json.loads((HERE/"yeast-followup/preregistered.json").read_text())
        refset = {f["artifact_ref"] for f in e["inputs"] if f["sha256"] == reg["article_sha256"]}
        cell = reg["row"]
        target_reads = [r for r in e["original_reads"] if r["official"] and r["source_digest_verified"]
                        and r["scope"].startswith("module:") and r["source_ref"] in refset
                        and r["label"] == cell["locator"] and r["text_sha256"] == cell["cell_sha256"]]
        choices = [c for c in e["native_calls"] if c["official"] and c["tool"] == "gx_select_capability"
                   and c.get("caller_scope","").startswith("module:") and c.get("event_id")
                   and c["arguments"].get("action")=="enable" and
                   c["arguments"].get("method","").startswith("yeast-archive-lookup@")]
        first = {}
        for c in choices:
            first.setdefault(c["caller_scope"], c)
        ordered = [{"selection_event":c["event_id"],"selection_call":c["call_id"],"prior_read":r["receipt_id"],
                    "original_call":r["call_id"],"scope":scope} for scope,c in first.items() for r in target_reads
                   if r["scope"] == scope and r["seq"] < c["seq"]]
        eligible = [j for j in dbjobs if j.get("parameters",{}).get("substrate","").casefold() == cell["expected_substrate"].casefold()
                    and j.get("parameters",{}).get("taxid") == cell["strain_taxid"] and
                    any(c["event_id"]==j["module_binding"].get("event_id") for c in choices) and
                    any(f["verified_bytes"] and f.get("readout") for f in j["outputs"])]
        turns = e["native_calls"]
        openness = [c for c in turns if c["tool"]=="gx_open_module" and c["official"] and c.get("scope") in first]
        first_assigned = next((c["scope"] for c in turns if c["tool"]=="gx_open_module" and c["official"]
                               and c.get("module_instance_id")),None)
        first_ordered = [o for o in ordered if o["scope"] == first_assigned]
        leader_reads = [r for r in e["original_reads"] if r["scope"]=="run" and r["official"]
                        and r["source_ref"] in refset and r["source_digest_verified"]]
        eligible_lookup_proposals = [{"scope":c["scope"],"seq":c["seq"],"call_id":c["call_id"],
              "outcome":c["proposal"]["outcome"], "refusal":c.get("refusal"),
              "parameters":op.get("parameters"),"rationale":c["proposal"].get("rationale")}
              for c in turns if c["tool"]=="gx_decide_revision" and c["official"] and c["scope"] in first
              for op in c["proposal"]["ops"] if op.get("capability","").startswith("yeast-archive-lookup@")]
        outputs = {f["artifact_ref"] for j in eligible for f in j["outputs"] if f["verified_bytes"]}
        adoption = [d["decision_ref"] for d in own_decisions if d.get("reports_cited") and
                    outputs.intersection(d.get("report_backed_outputs",[]))]
        return [(bool(target_reads),[r["receipt_id"] for r in target_reads]),(bool(first_ordered),first_ordered),
                (bool(eligible),[j["job_id"] for j in eligible]),(bool(adoption),adoption),(False,[])], {
                    "first_assigned_scope":first_assigned, "later_or_first_observed_chains":ordered,
                    "selections":[pick(c,"seq event_id caller_scope call_id") for c in choices],
                    "delegations":[{"scope":o["scope"],"seq":o["seq"],"arguments":o["arguments"],
                                    "lead_prior_original_reads":[pick(r,"receipt_id label segment_id seq") for r in leader_reads if r["seq"] < o["seq"]],
                                    "lead_prior_target_reads":[r["receipt_id"] for r in leader_reads if r["label"]==cell["locator"] and r["seq"] < o["seq"]]}
                                   for o in openness],
                    "lookup_proposals":eligible_lookup_proposals,
                    "source_before_query_parameters":[{"read_call":r["call_id"],"query_call":q["call_id"],"query_outcome":q["outcome"]}
                                for r in target_reads for q in eligible_lookup_proposals
                                if q["scope"]==r["scope"] and q["seq"] > r["seq"] and
                                (q["parameters"] or {}).get("substrate","").casefold() == cell["expected_substrate"].casefold()],
                    "report_science_review":"Compare the cited Table 2 cell and the four printed native-protein values with the child's sourced report and the Lead's wording."}
    if case == "yeast":
        module_reads = [r for r in e["original_reads"] if r["scope"].startswith("module:") and r["official"]
                        and r["kind"] in ("jats","pdf","epub") and r["source_digest_verified"]]
        choices = [c for c in e["native_calls"] if c["tool"] == "gx_select_capability" and c["official"]
                   and c.get("caller_scope","").startswith("module:") and c["arguments"].get("action") == "enable"
                   and c.get("event_id") and c["arguments"].get("method","").startswith("yeast-archive-lookup@")]
        admissions = [c for c in e["native_calls"] if c["tool"] == "gx_open_module" and c["official"]
                      and "method_choices" not in c["arguments"]]
        choices = [c for c in choices if any(a.get("scope")==c["caller_scope"] and c["arguments"]["method"] in
                    a.get("eligible_methods",[]) for a in admissions) and "capability_options" not in e["explicit_method_choices"]]
        linked_jobs = [j for j in dbjobs if any(c["event_id"] == j["module_binding"].get("event_id") for c in choices)
                       and any(f.get("readout") and f["verified_bytes"] for f in j["outputs"])]
        before_choice = [{"read":r["receipt_id"],"selection_event":c["event_id"],"scope":r["scope"]}
                         for c in choices for r in module_reads if r["scope"] == c["caller_scope"] and r["seq"] < c["seq"]]
        archive_refs = {o["artifact_ref"] for j in linked_jobs for o in j["outputs"]}
        chain = [d["decision_ref"] for d in own_decisions
                 if archive_refs.intersection(d["actual_outputs_cited"] + d["report_backed_outputs"] + list(d["derived_evidence"].values()))]
        return [(bool(module_reads), [r["receipt_id"] for r in module_reads]),
                (bool(choices), [c["event_id"] for c in choices]), (bool(linked_jobs), [j["job_id"] for j in linked_jobs]),
                (bool(chain),chain),(bool(before_choice),before_choice)], {
                    "module_original_precedes_its_method_selection":before_choice,
                    "selection_chronology":[{"selection_event":c["event_id"],"scope":c["caller_scope"],
                          "call_id":c["call_id"],"native_seq":c["seq"],
                          "first_original_call":next((r["call_id"] for r in module_reads if r["scope"]==c["caller_scope"]),None),
                          "original_reads_before":sum(r["scope"]==c["caller_scope"] and r["seq"]<c["seq"] for r in module_reads),
                          "original_reads_after":sum(r["scope"]==c["caller_scope"] and r["seq"]>c["seq"] for r in module_reads)}
                         for c in choices],
                    "semantic_review":"Actual archive annotation and assay interpretation must be compared to the raw readout. An earlier paragraph is necessary but not sufficient evidence that it prompted the method choice."}
    if case == "a":
        overlaps = []
        module_turns = [t for t in e["turns"] if t["scope"].startswith("module:") and t["official"]]
        for i,left in enumerate(module_turns):
            for right in module_turns[i+1:]:
                a,b = left["official"],right["official"]
                if left["scope"] != right["scope"] and a.get("ended") and b.get("ended") and (
                        max(a["started"], b["started"]) < min(a["ended"], b["ended"])):
                    overlaps.append([left["turn_id"],right["turn_id"]])
        receives = [c for c in e["native_calls"] if c.get("delivered_hints") and c.get("official")]
        hints = []
        for receipt in receives:
            after = [c for c in e["native_calls"] if c["official"] and c["scope"] == receipt["scope"]
                     and c["seq"] > receipt["seq"] and c["tool"] in ("gx_query_catalog","gx_decide_revision")]
            if after:
                hints.append({"delivery_call":receipt["call_id"], "next_call":after[0]["call_id"],
                              "next_arguments":after[0].get("arguments"), "scope":receipt["scope"]})
        for t in module_turns:
            delivered = [msg for page in t["topics"] for msg in page.get("entries",[]) if msg.get("sender_scope") != t["scope"]]
            later = [c for c in e["native_calls"] if c["turn_id"] == t["turn_id"] and c["official"]
                     and c["tool"] in ("gx_query_catalog","gx_decide_revision")]
            if delivered and later:
                hints.append({"delivery_turn":t["turn_id"],"delivered_hints":[m.get("hint_id") for m in delivered],
                              "next_call":later[0]["call_id"],"next_arguments":later[0].get("arguments"),"scope":t["scope"]})
        route_ops = defaultdict(set)
        for evt in e["events"]:
            if evt["kind"] == "v6.route.revised":
                route_ops[evt["subject_id"]].add(evt["data"].get("frozen_route",{}).get("binding_state"))
        bound = [key for key, ops in route_ops.items() if {"INTENT_UNBOUND","BOUND_EXECUTABLE"} <= ops]
        joins = [x for x in e["events"] if x["kind"] == "research.join.ready"
                 and x["data"].get("outcome",{}).get("state") == "SATISFIED"]
        joined = []
        for evt in joins:
            for turn in e["turns"]:
                if turn["scope"] != "run" or not turn["official"] or not any(
                        j.get("outcome_event") == evt["event_id"] for j in turn["joins"]):
                    continue
                for d in e["lead_decisions"]:
                    if d["task_id"] == turn["task_id"] and d["event_id"] > evt["event_id"] and d["effects"]:
                        joined.append({"event_id":evt["event_id"],"turn_id":turn["turn_id"],"decision":d["decision_ref"]})
        macros = [r["reference"] for r in e["recipes"] if r["is_macro"] and r["has_modules_join_decision"]]
        return [(bool(overlaps),overlaps),(False,hints),(bool(bound),bound),(bool(joined),joined),
                (bool(macros),macros)], {"hint_action_review":"A later query alone cannot establish causation. Compare the receiver's earlier search, delivered words and ensuing action."}
    prior = e["predecessor"]
    inherited = [c for c in e["native_calls"] if c["official"] and c["tool"] == "gx_instantiate_recipe"
                 and c.get("digest") in e["available_predecessor_macro_digests"]]
    registered = json.loads((HERE/"hydro/b/goal.json").read_text())["input_files"]
    b_hashes = {f["sha256"] for f in registered}
    new_refs = {f["artifact_ref"] for f in e["inputs"] if f["sha256"] in b_hashes}
    own_jobs = [j for j in e["jobs"] if j["status"]=="SUCCEEDED" and any(
        x["artifact_ref"] in new_refs for x in j["inputs"]) and any(f["verified_bytes"] for f in j["outputs"])]
    outputs = {x["artifact_ref"] for j in own_jobs for x in j["outputs"]}
    used = [d["decision_ref"] for d in own_decisions if outputs.intersection(
        d["actual_outputs_cited"]+d["report_backed_outputs"]+list(d["derived_evidence"].values()))]
    received = [t["turn_id"] for t in e["turns"] if t["official"] and t["scope"]=="run" and outputs.intersection(r["ref"] for r in t["delivered_content"])]
    received.extend(c["call_id"] for c in e["native_calls"] if c["official"] and c["scope"]=="run" and c.get("has_content") and c.get("ref") in outputs)
    return [(bool(prior and own_jobs),[j["job_id"] for j in own_jobs]),(bool(inherited),[c["call_id"] for c in inherited]),
            (bool(received),received),(bool(used),used)], {"source_run":prior.get("parent_run_id"),
            "expanded_recipe_requires_target_execution":"The instantiate receipt describes a plan. Check B's module admissions and results separately."}


def collect(base, case):
    work = base / (case if case in ("yeast","yeast-followup") else "hydro")
    state = work / "state"
    dbfile = state / "gx.db"
    if case == "yeast-followup":
        criteria = json.loads((HERE/case/"preregistered.json").read_text())["scoring"]
    else:
        criteria = json.loads((HERE/"preregistered.json").read_text())["procedural_scoring"][case]
    if case == "yeast":
        criteria = [*criteria, "module original read precedes its local archive selection"]
    empty = {"case":case,"status":"not_admitted","observed":[],
             "missing":[{"criterion":x,"evidence":[]} for x in criteria]}
    if not dbfile.is_file():
        return empty
    host = rollouts(work)
    with closing(sqlite3.connect(dbfile.resolve().as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        question = (HERE / (case if case in ("yeast","yeast-followup") else "hydro/"+case) / "question.md").read_text().strip()
        candidates = [r for r in db.execute("SELECT * FROM v6_goals") if r["goal"].strip() == question]
        if not candidates:
            if case == "b":
                candidates = list(db.execute("""SELECT g.* FROM v6_goals g JOIN runs r USING(run_id)
                    WHERE json_extract(r.inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')='june-gauges-first'"""))
            if not candidates:
                return empty
        if len(candidates) != 1:
            raise ValueError("Multiple Run identities match the frozen study")
        goal = candidates[0]
        run = goal["run_id"]
        data = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])
        conf = data["inputs"]["p0_spec"]
        result = {"case":case,"run_id":run,"status":goal["status"],"stop_decision_ref":goal["stop_decision_ref"],
                  "effort":conf.get("research_effort","quick"),"policy":data["inputs"].get("harness_policy"),
                  "explicit_method_choices":{k:conf[k] for k in ("capability_options","capability_collections","capabilities") if k in conf},
                  "predecessor":conf.get("predecessor",{}),
                  "inputs":[pick(f, "role artifact_ref sha256") for f in data["files"]]}
        result["events"] = events(db, run)
        result["jobs"] = jobs(db, state, run)
        refs = {f["artifact_ref"] for j in result["jobs"] for f in j["outputs"] if f["verified_bytes"]}
        reports = []
        for row in db.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? ORDER BY rowid", (run,)):
            r = json.loads(row[0])
            raw = bytes_for(db,state,r["report_ref"],r["report_sha256"])
            record = {**pick(r,"module_instance_id scope author report_ref report_sha256 event_id wake_event_id status"),
                      "verified_bytes":raw is not None,"sources":r["source_refs"]}
            if raw is not None:
                document = json.loads(raw)
                record.update(pick(document,"findings qualitative_notes gaps next_steps applicability"))
            evidence_fields = [document.get(k,[]) for k in ("findings","qualitative_notes","verified_metrics",
                                "metrics","source_quotations")] if raw is not None else []
            record["cited_scientific_refs"] = sorted(set(record["sources"]).intersection(strings(evidence_fields)))
            author = r.get("author") or {}
            native = db.execute("""SELECT n.request_json FROM p0_v6_native_calls n JOIN v6_decision_tasks d
                ON d.task_id=? JOIN p0_roots root ON root.task_id=d.task_id AND root.thread_id=n.thread_id
                WHERE n.thread_id=? AND n.turn_id=? AND n.call_id=? AND d.run_id=? AND d.scope=?""",
                (author.get("task_id"),author.get("thread_id"),author.get("turn_id"),author.get("call_id"),run,r["scope"])).fetchone()
            record["official_worker_submission"] = bool(author.get("kind")=="MODULE" and native and
                json.loads(native[0]).get("tool")=="gx_submit_module_report" and
                host.get((author.get("thread_id"),author.get("turn_id")),{}).get("provider")=="openai")
            reports.append(record)
        result["reports"] = reports
        verdicts = result["decisions"] = decisions(db,run)
        known = refs | {r["report_ref"] for r in reports} | {x for v in verdicts for x in v["evidence_refs"]}
        result["turns"] = turns(db,run,host,known)
        documents = {}
        derived = {}
        for ref in known:
            a = db.execute("SELECT kind,sha256,media_type FROM artifacts WHERE artifact_ref=?",(ref,)).fetchone()
            if a and a["media_type"] and "json" in a["media_type"]:
                raw = bytes_for(db,state,ref,a["sha256"])
                if raw is not None:
                    doc = json.loads(raw)
                    if a["kind"] in ("v62_source_extraction","v62_evidence_review") and isinstance(doc,dict):
                        source = doc.get("source_ref")
                        if source in refs:
                            src = db.execute("SELECT sha256 FROM artifacts WHERE artifact_ref=?", (source,)).fetchone()
                            if src and src["sha256"] == doc.get("source_sha256"):
                                derived[ref] = source
                    documents[ref] = hashlib.sha256(json.dumps(doc,ensure_ascii=False,sort_keys=True,
                                                               separators=(",", ":")).encode()).hexdigest()
        actions = result["native_calls"] = calls(db,run,host,known,documents)
        authenticated = {t["turn_id"] for t in result["turns"]
                         if (t.get("official") or {}).get("provider") == "openai"}
        # Host files can record a new turn before GX has acknowledged the native turn.
        for c in actions:
            c["official"] = c["official"] and c["turn_id"] in authenticated
        read_actions = {c["receipt_id"]:c for c in actions if c.get("receipt_id")}
        source_hashes = {}
        source_rows = db.execute("""SELECT r.receipt_id,r.source_id,r.segment_id,r.text_sha256,r.task_id,
            s.blob_artifact_ref AS source_ref,s.blob_sha256,s.kind,seg.label,seg.text,seg.text_sha256 AS segment_sha,
            t.scope FROM source_reads r JOIN sources s USING(source_id) JOIN source_segments seg
            USING(source_id,segment_id) JOIN v6_decision_tasks t USING(task_id) WHERE t.run_id=? ORDER BY r.rowid""", (run,))
        readings = result["original_reads"] = []
        for s in source_rows:
            c = read_actions.get(s["receipt_id"])
            if not c or c["arguments"].get("source_ref") != s["source_ref"] or c["task_id"] != s["task_id"]:
                continue
            ref = s["source_ref"]
            if ref not in source_hashes:
                source_hashes[ref] = bytes_for(db,state,ref,s["blob_sha256"]) is not None
            readings.append({**pick(s,"receipt_id source_id segment_id source_ref kind label text_sha256 scope"),
                             **pick(c,"seq call_id turn_id official"),
                             "source_digest_verified":source_hashes[ref] and c.get("returned_text_sha256") == s["segment_sha"] == s["text_sha256"] ==
                             hashlib.sha256(s["text"].encode()).hexdigest()})
        result["lead_decisions"] = compare_decisions(db,run,goal,verdicts,result["turns"],actions,reports,refs,derived)
        recipes = []
        for r in db.execute("SELECT * FROM v6_saved_recipes WHERE source_run=?", (run,)):
            doc = json.loads(r["definition_json"])
            macro = doc.get("macro") or {}
            recipes.append({**pick(r, "reference digest source_run"),"is_macro":bool(macro),
                            "has_modules_join_decision":bool(macro.get("modules") and macro.get("join") and macro.get("decision_point")),
                            "modules":[pick(m,"slot module_id module_version method_choices") for m in macro.get("modules",[])],
                            "join":macro.get("join"), "decision_point":macro.get("decision_point")})
        result["recipes"] = recipes
        result["available_predecessor_macro_digests"] = [r[0] for r in db.execute(
            "SELECT digest FROM v6_saved_recipes WHERE source_run=? AND json_type(definition_json,'$.macro')='object'",
            (result["predecessor"].get("parent_run_id"),))]
        scored, extra = proofset(case,result)
        assertions = [{"criterion":c,"evidence":proof} for c,(_,proof) in zip(criteria,scored,strict=True)]
        result.update(observed=[a for a,(ok,_) in zip(assertions,scored) if ok],
                      missing=[a for a,(ok,_) in zip(assertions,scored) if not ok],interpretation_inputs=extra)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base",type=Path)
    parser.add_argument("case",choices=("yeast","yeast-followup","a","b"))
    args = parser.parse_args()
    print(json.dumps(collect(args.base.resolve(),args.case), ensure_ascii=False,indent=2))
