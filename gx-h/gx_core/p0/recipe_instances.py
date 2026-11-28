"""Bind a portable macro's logical producers to the current study at its first accepted use."""
import json

from ..domain import clock, ids
from ..errors import SchemaInvalid, ScopeDenied
from . import harness_policy, research_joins, v6_macro_recipes


def _branches(conn, rid, branch):
    if not branch.startswith("local:"):
        return {branch}
    return {row[0] for row in conn.execute("""SELECT b.branch_id FROM v6_branches b
        JOIN v6_decisions d USING(run_id,decision_ref)
        JOIN json_each(d.receipt_json,'$.aliases') alias ON alias.value=b.branch_id
        WHERE b.run_id=? AND alias.key=?""", (rid, branch))}


def _same_branch(conn, rid, previous, branch, target):
    if previous.get("branch_id"):
        return previous["branch_id"] in target
    return branch == previous["branch"] or bool(target and _branches(conn, rid, previous["branch"]) == target)


def identity(conn, rid, reference, digest, version, prefix, inputs, branch):
    parts = [reference, digest, version, prefix, inputs]
    if not harness_policy.current(conn, rid):
        return ids.sha256_of_obj(parts)[:24]
    target = _branches(conn, rid, branch)
    if len(target) > 1:
        raise SchemaInvalid("This branch alias has multiple bindings. Select a durable branch ID.")
    # A draft can name a local branch before the first graph commit assigns its ID.
    # Recover the original instance if this is its durable spelling.
    for row in conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND
            subject_kind='research_join' AND kind=? AND json_extract(payload_json,'$.goal_version')=? AND
            json_extract(payload_json,'$.instance.recipe')=? AND json_extract(payload_json,'$.instance.prefix')=?""",
            (rid, research_joins.REGISTERED, version, reference, prefix)):
        accepted = json.loads(row[0])
        instance = accepted["instance"]
        if instance["digest"] != digest or instance["inputs"] != inputs:
            continue
        if _same_branch(conn, rid, instance, branch, target):
            return accepted["key"].removeprefix("recipe-")
    return ids.sha256_of_obj(parts + [next(iter(target), branch)])[:24]


def pending_adoptions(conn, rid, version, tool, args):
    """Select only exact, issued proposals, outside the decision writer."""
    if tool == "gx_open_module":
        key = args.get("operation_key", "")
        if not key.startswith("recipe-"):
            return []
        selector = key.split("-", 2)[1]
        clause, parameter = "json_extract(c.result_json,'$.instance_key')=?", "recipe-" + selector
    else:
        nodes = [op for op in args["decision"].get("ops", []) if op.get("recipe_decision")]
        if not nodes:
            return []
        refs = sorted({op["recipe_decision"]["recipe"] for op in nodes})
        clause, parameter = "json_extract(c.result_json,'$.recipe') IN (SELECT value FROM json_each(?))", ids.canonical_json(refs)
    proposals = conn.execute(f"""SELECT n.request_json,c.result_json
        FROM p0_roots r JOIN v6_decision_tasks t USING(task_id,run_id)
        JOIN p0_v6_native_calls n USING(thread_id)
        JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
        WHERE r.run_id=? AND t.goal_version=? AND t.scope='run'
          AND json_extract(n.request_json,'$.tool')='gx_instantiate_recipe' AND {clause}
        ORDER BY c.rowid DESC""", (rid,version,parameter))
    selected = {}
    matches = {}
    issued_commission = False
    proposed_nodes = set()
    for row in proposals:
        proposal = json.loads(row["result_json"])
        key = proposal.get("instance_key")
        if not key:
            continue
        if tool == "gx_open_module":
            matched = []
            for item in proposal["module_calls"]:
                issued = item.get("arguments")
                if issued is None or issued["operation_key"] != args["operation_key"]:
                    continue
                issued_commission = True
                # Money and presentation round belong to this study. Keep the
                # requested research, input bindings and audience of the logical slot.
                local = {"sub_budget", "round"}
                if {k:v for k,v in args.items() if k not in local} == {k:v for k,v in issued.items() if k not in local}:
                    matched.append(args["operation_key"])
        else:
            matched = []
            by_node = {candidate["node"]: candidate for candidate in proposal["ops"]}
            for op in nodes:
                name = op["node"]
                candidate = by_node.get(name)
                if (not candidate or op["branch"] != candidate["branch"]
                        or op["recipe_decision"] != candidate["recipe_decision"]):
                    continue
                proposed_nodes.add(name)
                # The present Lead chooses its question; input ports and executable
                # settings identify the same step that will satisfy the Join.
                if dict(candidate, question=op.get("question")) == dict(op, question=op.get("question")):
                    matched.append(name)
        for name in matched:
            matches.setdefault(name, set()).add(key)
            selected.setdefault(key, json.loads(row["request_json"])["arguments"])
    if issued_commission and not matches:
        raise SchemaInvalid("The recipe's commission has different scientific or audience bindings. Use a new operation key for independent work.")
    if proposed_nodes - matches.keys():
        raise SchemaInvalid("This node differs from the proposed recipe stage. Rebind the recipe or commission an independent node without its recipe marker.")
    adopted_keys = set()
    for keys in matches.values():
        if len(keys) > 1:
            keys = {key for key in keys if research_joins.lookup(conn, rid, key)}
            if len(keys) != 1:
                raise SchemaInvalid("Several previewed instances match this work. Use a distinct prefix or adopt one explicitly.")
        adopted_keys.update(keys)
    return [prepare(conn, rid, version, selected[key]) for key in sorted(adopted_keys)]


def prepare(conn, rid, version, args):
    row = conn.execute("SELECT definition_json FROM v6_saved_recipes WHERE reference=? AND digest=?",
                       (args["recipe"],args["digest"])).fetchone()
    if not row:
        raise SchemaInvalid("The recipe release does not match this instance")
    definition = json.loads(row[0])
    macro = definition.get("macro")
    if not macro:
        raise SchemaInvalid("Explicit logical Join adoption requires a macro recipe")
    key = "recipe-" + identity(conn, rid, args["recipe"], args["digest"], version, args["prefix"], args["inputs"], args["branch"])
    modules = {m["slot"]:m for m in macro["modules"]}
    steps = {s["slot"]:s for s in definition["steps"]}
    dependencies = {}
    for slot, producer in {**steps,**modules}.items():
        ports = [{"slot":p.get("module",p.get("step")), "output_name":p["output"]}
                 for p in producer["inputs"] if "input" not in p]
        named = {p["slot"] for p in ports}
        dependencies[slot] = ports + [{"slot":dep} for dep in producer.get("depends_on",[]) if dep not in named]
    ports = []
    for port in macro["join"]["ports"]:
        alternatives = []
        for alt in port["alternatives"]:
            slot = alt.get("module",alt.get("step"))
            if modules.get(slot,{}).get("communication") == "isolated":
                raise ScopeDenied("An isolated researcher cannot be a Join source")
            alternatives.append({"recipe_slot":slot,"output_name":alt["output_name"]})
        ports.append(dict(slot=port["slot"],required=port.get("required",False),alternatives=alternatives))
    spec = {k:v for k,v in macro["join"].items() if k != "ports"}
    spec.update(key=key,ports=ports,question=macro["decision_point"]["question"])
    instance = {key:args[key] for key in ("recipe","digest","prefix","branch","inputs")}
    instance.update(dependencies=dependencies,modules={
        slot:"module_"+ids.sha256_of_obj([rid,key+"-"+slot])[:32] for slot in modules})
    return dict(scope="run",key=key,goal_version=version,revision=1,spec=spec,instance=instance)


def adopt(conn, rid, plan, aliases=None):
    """The native callback commits this registration with the first accepted commission."""
    latest = research_joins.lookup(conn,rid,plan["key"])
    if latest:
        original = research_joins.lookup(conn,rid,plan["key"],1)
        if original and original.get("instance"):
            previous = original["instance"]
            instance = plan["instance"]
            branch = instance["branch"]
            if (all(previous[field] == instance[field] for field in ("recipe", "digest", "inputs", "prefix"))
                    and _same_branch(conn, rid, previous, branch, _branches(conn, rid, branch))):
                return latest
        raise SchemaInvalid("The recipe's Join key is already used by another watch; choose another instance prefix")
    instance = plan["instance"]
    branch = instance["branch"]
    bound = (aliases or {}).get(branch) or next(iter(_branches(conn, rid, branch)), None)
    if bound:
        instance["branch_id"] = bound
    delay = plan["spec"].get("timeout_seconds")
    deadline = clock.now().timestamp()+delay if delay else None
    event = research_joins._emit(conn,rid,research_joins.REGISTERED,dict(plan,deadline=deadline))
    return dict(plan,registration_event=event,state="WAITING",outcome=None,outcome_event=None,deadline=deadline)


class Producers:
    """Observe physical bindings when present. An unborn producer inherits its prerequisites' reachability."""
    def __init__(self, conn, rid, row):
        self.conn, self.rid = conn,rid
        instance = self.instance = row["instance"]
        pinned = conn.execute("SELECT definition_json FROM v6_saved_recipes WHERE reference=? AND digest=?",
                              (instance["recipe"],instance["digest"])).fetchone()
        if not pinned:
            raise SchemaInvalid("The adopted recipe definition is unavailable")
        definition = json.loads(pinned[0])
        steps = {s["slot"]:s for s in definition["steps"]}
        self.nodes = v6_macro_recipes._bound_nodes(conn,rid,instance["recipe"],instance["digest"],instance["prefix"],
                                                  instance.get("branch_id", instance["branch"]),steps,instance["inputs"])
        self.cache = {}

    def location(self, slot):
        if slot in self.nodes:
            return {"node_id": self.nodes[slot]}
        mid = self.instance["modules"].get(slot)
        if mid and research_joins._module(self.conn,self.rid,mid):
            return {"module_instance_id":mid}
        return None

    def observe(self, slot, output):
        key = slot,output
        if key not in self.cache:
            self.cache[key] = self._observe(slot,output)
        return self.cache[key]

    def _observe(self, slot, output):
        where = self.location(slot)
        if where and output is None and "module_instance_id" in where:
            output = "report"
        if where and output is None:
            node = research_joins._node(self.conn,self.rid,where["node_id"])
            status = node["status"]
            if status in ("SUCCEEDED","REUSED"):
                return dict(where,state="AVAILABLE",status=status,evidence=[])
            if status in ("BLOCKED","FAILED","CANCELLED"):
                return dict(where,state="MISSING",reason=status)
            return dict(where,state="PENDING",status=status,job_id=node["job_id"])
        if where:
            result = research_joins._source_result(self.conn,self.rid,dict(where,output_name=output))
            return {**where,**result}
        waiting = []
        for dependency in self.instance["dependencies"][slot]:
            observation = self.observe(dependency["slot"],dependency.get("output_name"))
            # A named prerequisite cannot deliver a corrupt artifact to the next module.
            if observation.get("evidence"):
                from ..artifacts import ArtifactService
                observation = dict(observation)
                research_joins._check_deliveries(ArtifactService(self.conn),observation,{})
            if observation["state"] != "AVAILABLE":
                waiting.append({**dependency,**{k:v for k,v in observation.items() if k!="evidence"}})
        missing = any(item["state"] != "PENDING" for item in waiting)
        return dict(recipe_slot=slot,planned_module=slot in self.instance["modules"],state="MISSING" if missing else "PENDING",waiting_on=waiting,
                    reason="prerequisite_unreachable" if missing else "awaiting_commission")

    def physical_spec(self, spec):
        ports = []
        for port in spec["ports"]:
            alternatives = []
            for alt in port["alternatives"]:
                location = self.location(alt["recipe_slot"])
                if not location:
                    return None
                alternatives.append(dict(location,output_name=alt["output_name"]))
            ports.append(dict(port,alternatives=alternatives))
        return dict(spec,ports=ports)
