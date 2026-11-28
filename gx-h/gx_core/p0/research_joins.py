"""Lead-authored evidence joins. The event log owns registrations and one resolution per revision."""
import json
import math

from ..artifacts import ArtifactService
from ..domain import clock
from ..errors import SchemaInvalid, ScopeDenied, GxError
from ..persistence import store
from . import harness_policy, manifest, research_topics, v62_outputs, v62_reports

REGISTERED = "research.join.registered"
READY = "research.join.ready"


def _records(conn, run_id, through=None):
    sql = """SELECT event_id,kind,payload_json FROM events
             WHERE subject_kind='research_join' AND subject_id=?"""
    args = [run_id]
    if through is not None:
        sql += " AND event_id<=?"
        args.append(through)
    records = {}
    for row in conn.execute(sql + " ORDER BY event_id", args):
        data = store.loads(row, "payload_json")
        index = data["key"], data["revision"]
        if row["kind"] == REGISTERED:
            records[index] = {**data, "registration_event": row["event_id"], "outcome_event": None,
                              "state": "WAITING", "outcome": None}
        elif row["kind"] == READY:
            records[index].update(state=data["outcome"]["state"], outcome=data["outcome"], outcome_event=row["event_id"])
    return records


def lookup(conn, run_id, key, revision=None):
    records = _records(conn, run_id)
    if revision is not None:
        return records.get((key, revision))
    return next((row for (name, _), row in reversed(records.items()) if name == key), None)


def view(conn, run_id, through=None):
    records = _records(conn, run_id, through)
    if not records:
        return []
    goal = conn.execute("SELECT goal_version,status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    latest = {row["key"]: row for row in records.values() if row["goal_version"] == goal["goal_version"]}
    return list(latest.values()) if goal["status"] == "OPEN" else []


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SchemaInvalid(f"{field} must be non-empty text")
    return value


def _normalise(spec):
    if not isinstance(spec, dict) or spec.keys() - {"key", "revision", "question", "mode", "ports", "k", "timeout_seconds"}:
        raise SchemaInvalid("Invalid evidence join fields")
    normal = {key: _text(spec.get(key), key) for key in ("key", "question", "mode")}
    mode = normal["mode"]
    ports = spec.get("ports")
    if mode not in ("all", "any", "k-of-n") or not isinstance(ports, list) or not ports:
        raise SchemaInvalid("An evidence join needs all, any or k-of-n and at least one port")
    if mode == "k-of-n":
        if type(spec.get("k")) is not int or not 1 <= spec["k"] <= len(ports):
            raise SchemaInvalid("k must be an integer within the number of ports")
        normal["k"] = spec["k"]
    elif "k" in spec:
        raise SchemaInvalid("Only a k-of-n join takes k")
    if "timeout_seconds" in spec:
        delay = spec["timeout_seconds"]
        if type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0:
            raise SchemaInvalid("timeout_seconds must be a finite positive number")
        normal["timeout_seconds"] = delay
    if "revision" in spec and (type(spec["revision"]) is not int or spec["revision"] < 1):
        raise SchemaInvalid("revision must be a positive integer")
    slots = set()
    normal["ports"] = []
    for port in ports:
        if not isinstance(port, dict) or port.keys() - {"slot", "alternatives", "required"}:
            raise SchemaInvalid("Invalid join port")
        slot = _text(port.get("slot"), "slot")
        if slot in slots or type(port.get("required", False)) is not bool:
            raise SchemaInvalid("Join slots must be unique and required must be a boolean")
        slots.add(slot)
        if not isinstance(port.get("alternatives"), list) or not port["alternatives"]:
            raise SchemaInvalid("A port requires at least one source")
        alts = []
        for alt in port["alternatives"]:
            if not isinstance(alt, dict) or set(alt) not in ({"node_id", "output_name"}, {"module_instance_id", "output_name"}):
                raise SchemaInvalid("Each join source names one node or module and its output")
            alt = {k: _text(v, k) for k, v in alt.items()}
            if alt in alts:
                raise SchemaInvalid("Duplicate alternative within a join port")
            alts.append(alt)
        normal["ports"].append({"slot": slot, "required": port.get("required", False), "alternatives": alts})
    return normal


def _node(conn, rid, node_id):
    return conn.execute("""SELECT n.*,t.goal_version AS commissioned_goal,j.status AS job_state,
                      m.module_instance_id,w.wait_json FROM v6_nodes n
                      JOIN v6_decisions d ON d.decision_ref=n.decision_ref AND d.run_id=n.run_id
                      JOIN v6_decision_tasks t ON t.task_id=d.task_id
                      LEFT JOIN v62_modules m ON m.branch_id=n.branch_id AND m.run_id=n.run_id
                      LEFT JOIN jobs j ON j.job_id=n.job_id LEFT JOIN v6_node_waits w USING(node_id)
                      WHERE n.node_id=? AND n.run_id=?""", (node_id, rid)).fetchone()


def _module(conn, rid, mid):
    return conn.execute("""SELECT m.*,r.report_ref FROM v62_modules m LEFT JOIN v62_module_reports r
                        USING(run_id,module_instance_id) WHERE m.run_id=? AND m.module_instance_id=?""",
                        (rid, mid)).fetchone()


def _check_source(conn, rid, goal_version, alt):
    module = None
    if "module_instance_id" in alt:
        module = _module(conn, rid, alt["module_instance_id"])
        if not module or module["goal_version"] != goal_version:
            raise ScopeDenied("Join source module is outside the current Goal")
    else:
        node = _node(conn, rid, alt["node_id"])
        if not node or node["commissioned_goal"] != goal_version:
            raise ScopeDenied("Join source node is outside the current Goal")
        if node["module_instance_id"]:
            module = _module(conn, rid, node["module_instance_id"])
    if module and research_topics.isolated(module):
        raise ScopeDenied("An isolated researcher cannot be a join source")


def _emit(conn, rid, kind, payload):
    return store.append_event(conn, run_id=rid, actor="GX", subject_kind="research_join", subject_id=rid,
                              kind=kind, evidence_level="EXECUTION_OBSERVED", payload=payload)


def register(conn, run_id, spec):
    if not harness_policy.active(conn, run_id):
        raise ScopeDenied("Evidence joins require the current harness policy")
    normal = _normalise(spec)
    key = normal["key"]
    with store.transaction(conn, transaction_owner="ScientificLead", operation="register_evidence_join", run_id=run_id):
        goal = conn.execute("SELECT goal_version,status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
        if not goal or goal["status"] != "OPEN":
            raise ScopeDenied("An active Goal is required to register an evidence join")
        previous = lookup(conn, run_id, key)
        revision = spec.get("revision", previous["revision"] if previous else 1)
        exact = lookup(conn, run_id, key, revision) if previous else None
        if not (exact and exact["goal_version"] == goal["goal_version"] and normal == exact["spec"]):
            if revision != (previous["revision"] + 1 if previous else 1):
                raise SchemaInvalid("Changing an evidence join requires the next explicit revision")
            for port in normal["ports"]:
                for alt in port["alternatives"]:
                    _check_source(conn, run_id, goal["goal_version"], alt)
            delay = normal.get("timeout_seconds")
            _emit(conn, run_id, REGISTERED, {"scope": "run", "key": key, "revision": revision, "spec": normal,
                  "goal_version": goal["goal_version"], "deadline": clock.now().timestamp() + delay if delay else None})
    # Native callbacks may wrap registration in a receipt transaction. The Driver performs
    # artifact I/O and settlement after that commit. Standalone callers can reconcile now.
    if not conn.in_transaction:
        tick(conn, run_id)
    return lookup(conn, run_id, key, revision)


def _module_result(conn, rid, module, name, node_id=None):
    mid = module["module_instance_id"]
    basic = {"module_instance_id": mid, "module_status": module["status"]}
    if research_topics.isolated(module):
        return {"state": "MISSING", "reason": "isolated_source"}
    if not module["report_ref"]:
        if module["status"] == "OPEN":
            waits = [dict(row) for row in conn.execute("""SELECT n.node_id,n.job_id,n.status,j.status AS job_state,
                w.wait_json FROM v6_nodes n LEFT JOIN jobs j USING(job_id) LEFT JOIN v6_node_waits w USING(node_id)
                WHERE n.run_id=? AND n.branch_id=? AND n.status IN ('PENDING','RUNNING','CANCEL_REQUESTED')""",
                (rid, module["branch_id"]))]
            for row in waits:
                if value := row.pop("wait_json"):
                    row["retry"] = json.loads(value)
            return dict(basic, state="PENDING", in_flight=waits)
        return dict(basic, state="MISSING", reason="module_closed_without_report")
    bundle = ArtifactService(conn).read_json(module["report_ref"])
    if bundle["run_id"] != rid or bundle["module_instance_id"] != mid:
        raise ScopeDenied("Published report identity differs from its join source")
    basic["report_ref"] = module["report_ref"]
    verified = {src["artifact_ref"]: src for src in bundle["sources"]}
    proofs = []
    if name == "report" and not node_id and bundle["author"]["kind"] == "MODULE":
        cited = sorted({ref for finding in bundle.get("findings", []) for ref in finding["source_refs"] if ref in verified}
                       | {metric["artifact_ref"] for metric in bundle.get("verified_metrics", []) if metric.get("artifact_ref") in verified})
        if cited:
            proofs.append(dict(artifact_ref=module["report_ref"], source_refs=cited, output_name=name,
                               source_digests={ref: verified[ref]["sha256"] for ref in cited}))
    else:
        # A researcher can deliver the same named endpoint on several samples.
        # Preserve each producing Job so the Lead can distinguish their experimental contexts.
        for src in verified.values():
            for receipt in src["receipts"]:
                if (receipt.get("output_name") == name and receipt.get("job_id")
                        and receipt.get("output_sha256") == src["sha256"]
                        and (not node_id or node_id == receipt["node_id"])):
                    proofs.append({**{k: src[k] for k in ("artifact_ref", "sha256", "kind")},
                                   **{k: receipt[k] for k in ("node_id", "job_id", "output_name", "nature") if k in receipt}})
    if proofs:
        return dict(basic, state="AVAILABLE", evidence=[
            dict(proof, module_instance_id=mid, report_ref=module["report_ref"]) for proof in proofs])
    return dict(basic, state="MISSING", reason="requested_port_absent_in_published_report")


def _source_result(conn, rid, alt):
    name = alt["output_name"]
    if "module_instance_id" in alt:
        return _module_result(conn, rid, _module(conn, rid, alt["module_instance_id"]), name)
    node = _node(conn, rid, alt["node_id"])
    if node["module_instance_id"]:
        return _module_result(conn, rid, _module(conn, rid, node["module_instance_id"]), name, node["node_id"])
    basic = {k: node[k] for k in ("node_id", "status", "job_id", "job_state")}
    if node["status"] in {"SUCCEEDED", "REUSED"}:
        for out in v62_outputs.published_outputs(conn, rid, node["job_id"]):
            if out["output_name"] == name:
                return dict(basic, state="AVAILABLE", evidence=[dict(out, node_id=node["node_id"], job_id=node["job_id"])])
        return dict(basic, state="MISSING", reason="requested_port_absent_in_published_job")
    if node["status"] in {"BLOCKED", "FAILED", "CANCELLED"}:
        evt = conn.execute("""SELECT reason,payload_json FROM events WHERE subject_kind='node' AND subject_id=?
                           AND kind='v6.node.settled' ORDER BY event_id DESC LIMIT 1""", (node["node_id"],)).fetchone()
        details = store.loads(evt, "payload_json") if evt else {}
        return dict(basic, state="MISSING", reason=evt["reason"] if evt and evt["reason"] else node["status"],
                    **{k: details[k] for k in ("failure", "condition", "dependency") if k in details})
    if node["wait_json"]:
        basic["retry"] = store.loads(node, "wait_json")
    return dict(basic, state="PENDING")


def _assign(ports, *, allow_pending):
    """Count distinct deliveries. Reassign alternatives to make room for constrained ports."""
    candidates = []
    for port in ports:
        choices = [proof["artifact_ref"] for proof in port["evidence"]]
        if allow_pending:
            for source in port["sources"]:
                if source["state"] == "PENDING":
                    producer = source.get("node_id") or source["module_instance_id"]
                    # An open module can report multiple Jobs with the same named output.
                    capacity = port["slot"] if "node_id" not in source and source["output_name"] != "report" else None
                    choices.append((producer, source["output_name"], capacity))
        candidates.append(choices)
    assigned = {}

    def assign(index, examined):
        for candidate in candidates[index]:
            if candidate in examined:
                continue
            examined.add(candidate)
            if candidate not in assigned or assign(assigned[candidate], examined):
                assigned[candidate] = index
                return True
        return False

    for index in sorted(range(len(ports)), key=lambda i: not ports[i]["required"]):
        assign(index, set())
    return {index: candidate for candidate, index in assigned.items()}


def _check_deliveries(assets, result, checks):
    accepted, issues = [], []
    for proof in result["evidence"]:
        pins = proof.get("source_digests") or {proof["artifact_ref"]: proof["sha256"]}
        for ref, sha in pins.items():
            if (ref, sha) not in checks:
                try:
                    manifest.verify_artifact_pin(assets, {"artifact_ref": ref, "sha256": sha})
                    checks[ref, sha] = None
                except (GxError, OSError) as error:
                    checks[ref, sha] = str(error)
        faults = {ref: checks[ref, sha] for ref, sha in pins.items() if checks[ref, sha]}
        if faults:
            issues.append(dict(artifact_ref=proof["artifact_ref"], faults=faults))
        else:
            accepted.append(proof)
    result["evidence"] = accepted
    if issues:
        result["unverified_deliveries"] = issues
    if not accepted:
        result.update(state="UNVERIFIED", reason="Published bytes differ from the registered digest or are unavailable")


def _evaluate(conn, rid, row, now, *, verify=False):
    spec = row["spec"]
    ports = []
    cache = {}
    checks = {}
    for port in spec["ports"]:
        sources = []
        for alt in port["alternatives"]:
            source = tuple(sorted(alt.items()))
            if source not in cache:
                try:
                    result = _source_result(conn, rid, alt)
                    if verify and result.get("evidence"):
                        _check_deliveries(ArtifactService(conn), result, checks)
                except (GxError, OSError) as error:
                    result = {"state": "UNVERIFIED", "reason": str(error)}
                cache[source] = result
            sources.append({**alt, **cache[source]})
        evidence = [proof for src in sources if src["state"] == "AVAILABLE" for proof in src["evidence"]]
        state = "AVAILABLE" if evidence else "PENDING" if any(s["state"] == "PENDING" for s in sources) else "MISSING"
        ports.append({"slot": port["slot"], "required": port["required"], "state": state, "evidence": evidence,
                      "sources": [{k: v for k,v in src.items() if k != "evidence"} for src in sources]})
    need = {"all": len(ports), "any": 1}.get(spec["mode"], spec.get("k"))
    delivered = _assign(ports, allow_pending=False)
    possible = _assign(ports, allow_pending=True)
    have = len(delivered)
    pending = len(possible) - have
    required = {i for i, p in enumerate(ports) if p["required"]}
    for index, port in enumerate(ports):
        if index in delivered:
            port["counted_artifact_ref"] = delivered[index]
        elif port["evidence"]:
            port["state"] = "PENDING" if index in possible else "MISSING"
            port["reason"] = "same_delivery_already_counted"
    if have >= need and required <= delivered.keys():
        state = "SATISFIED"
    elif len(possible) < need or not required <= possible.keys():
        state = "UNREACHABLE"
    elif row["deadline"] is not None and now >= row["deadline"]:
        state = "DEADLINE"
    else:
        return None
    # Waits inspect existing publication receipts. Hash currently usable measurements only
    # when the decision can actually be delivered, before obtaining the commit write lock.
    if have and not verify:
        return _evaluate(conn, rid, row, now, verify=True)
    return {"state": state, "question": spec["question"], "needed": need, "satisfied_count": have,
            "still_possible_count": pending, "ports": ports}


def tick(conn, run_id, *, now=None):
    if conn.in_transaction:
        # The ordinary driver reconciles after callback and admission writers release SQLite.
        return []
    current = clock.now().timestamp() if now is None else now
    resolved = []
    for row in view(conn, run_id):
        if row["state"] != "WAITING":
            continue
        cursor = conn.execute("SELECT MAX(event_id) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        outcome = _evaluate(conn, run_id, row, current)
        if outcome is None:
            continue
        with store.transaction(conn, transaction_owner="ScientificLead", operation="settle_evidence_join", run_id=run_id):
            if conn.execute("SELECT MAX(event_id) FROM events WHERE run_id=?", (run_id,)).fetchone()[0] != cursor:
                continue
            last = lookup(conn, run_id, row["key"])
            goal = conn.execute("SELECT goal_version,status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
            if (last["revision"] != row["revision"] or last["outcome_event"] or goal["status"] != "OPEN"
                    or goal["goal_version"] != row["goal_version"]):
                continue
            # A subscribed report may precede unrelated members of its presentation round.
            # Publish through the original owner in the same transaction as the decision wake.
            mids = {source["module_instance_id"] for port in outcome["ports"] for source in port["sources"]
                    if source.get("report_ref")}
            unreleased = mids - v62_reports._released(conn, run_id) if mids else set()
            for mid in sorted(unreleased):
                module = _module(conn, run_id, mid)
                v62_reports._wake(conn, module, "join-input:" + mid, "join_input", [module])
            payload = {k:row[k] for k in ("key", "revision", "scope", "goal_version", "registration_event")}
            event = _emit(conn, run_id, READY, dict(payload, outcome=outcome))
            resolved.append(event)
    return resolved


def attach(conn, rid, cursor, brief):
    rows = view(conn, rid, through=cursor)
    if not rows:
        return
    joins = []
    visible = dict.fromkeys(brief["evidence_refs"])
    for row in rows:
        item = {k: row[k] for k in ("key", "revision", "registration_event", "outcome_event", "deadline", "state")}
        item["question"] = row["spec"]["question"]
        if outcome := row["outcome"]:
            item["outcome"] = outcome
            for port in outcome["ports"]:
                visible.update(dict.fromkeys(proof["artifact_ref"] for proof in port["evidence"]))
                visible.update(dict.fromkeys(src["report_ref"] for src in port["sources"] if src.get("report_ref")))
        joins.append(item)
    brief.update(research_joins=joins, evidence_refs=list(visible))


def next_deadline_seconds(conn, run_id):
    deadlines = [r["deadline"] for r in view(conn, run_id) if r["state"] == "WAITING" and r["deadline"] is not None]
    return max(0.0, min(deadlines) - clock.now().timestamp()) if deadlines else None
