"""Bind a portable macro's logical producers to the current study at its first accepted use."""
import json

from ..domain import clock, ids
from ..errors import SchemaInvalid, ScopeDenied
from . import research_joins, v6_macro_recipes


def identity(reference, digest, version, prefix, inputs):
    return ids.sha256_of_obj([reference, digest, version, prefix, inputs])[:24]


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
    for row in proposals:
        proposal = json.loads(row["result_json"])
        key = proposal.get("instance_key")
        if not key:
            continue
        if tool == "gx_open_module":
            matched = [args["operation_key"]] if any(item.get("arguments") == args for item in proposal["module_calls"]) else []
        else:
            matched = [op["node"] for op in nodes if op in proposal["ops"]]
        for name in matched:
            matches.setdefault(name, set()).add(key)
            selected.setdefault(key, json.loads(row["request_json"])["arguments"])
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
    key = "recipe-" + identity(args["recipe"],args["digest"],version,args["prefix"],args["inputs"])
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


def adopt(conn, rid, plan):
    """The native callback commits this registration with the first accepted commission."""
    latest = research_joins.lookup(conn,rid,plan["key"])
    if latest:
        original = research_joins.lookup(conn,rid,plan["key"],1)
        if original and original.get("instance"):
            previous = original["instance"]
            if (previous["recipe"], previous["digest"], previous["inputs"], previous["prefix"]) == (
                plan["instance"]["recipe"], plan["instance"]["digest"], plan["instance"]["inputs"], plan["instance"]["prefix"]):
                return latest
        raise SchemaInvalid("The recipe's Join key is already used by another watch; choose another instance prefix")
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
                                                  instance["branch"],steps,instance["inputs"])
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
