"""Topic exchange between native researchers. The GX event log owns messages and read cursors."""
import json

from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from ..persistence import store

MESSAGE = "research.topic.message"
SUBSCRIBE = "research.topic.subscribed"
CONSUMED = "research.topic.consumed"
NOTICE = "research.topic.notice"
PAGE_SIZE = 16


def isolated(member):
    return json.loads(member["grant_json"]).get("communication") == "isolated"


def _append(conn, actor, member, kind, subject, payload):
    return store.append_event(conn, run_id=member["run_id"], kind=kind, actor=actor,
                              subject_kind="research_topic", subject_id=subject,
                              payload={"goal_version":member["goal_version"], **payload},
                              evidence_level="EXECUTION_OBSERVED")


def _subscriptions(conn, member):
    """One stable subscription per (Run, Goal version, module, topic)."""
    rows = conn.execute("""SELECT kind,payload_json FROM events WHERE run_id=? AND subject_kind='research_topic'
        AND subject_id=? AND kind IN (?,?) AND json_extract(payload_json,'$.goal_version')=? ORDER BY event_id""",
        (member["run_id"],member["scope"],SUBSCRIBE,CONSUMED,member["goal_version"]))
    result = {}
    for row in rows:
        value = json.loads(row["payload_json"])
        name = value["topic"]
        if row["kind"] == SUBSCRIBE:
            result[name] = {"high_water":value["high_water"], "cursor":0}
        else:
            result[name]["cursor"] = value["cursor"]
    return result


def subscribed_topics(conn, member):
    if isolated(member):
        return []
    return sorted(_subscriptions(conn, member))


def _messages(conn, rid, version, topic=None):
    sql = """SELECT event_id,subject_id,payload_json FROM events WHERE run_id=? AND kind=?
             AND json_extract(payload_json,'$.goal_version')=?"""
    args = [rid,MESSAGE,version]
    if topic is not None:
        sql += " AND subject_kind='research_topic' AND subject_id=?"
        args.append(topic)
    return [{**json.loads(row["payload_json"]), "event_id":row["event_id"], "topic":row["subject_id"]}
            for row in conn.execute(sql + " ORDER BY event_id", args)]


def _view(message):
    return {key:value for key,value in message.items() if key not in ("visibility_refs","goal_version")}


def _retired(messages):
    retired = {m["replaces"] for m in messages if m.get("replaces")}
    dependencies = {}
    for message in messages:
        parents = message["parent_hint_ids"]
        # Replacing the author's old wording inherits its scientific grounds, not its
        # superseded revision. Withdrawing any of those grounds retires derived work.
        dependencies[message["hint_id"]] = (dependencies[message["replaces"]]
            if message.get("replaces") else parents)
        if retired.intersection(dependencies[message["hint_id"]]):
            retired.add(message["hint_id"])
    return retired


def _project(messages, visible, cursor, reader=None, replay=False):
    """Present effective interpretations and distinguish repeated forwards from new work."""
    retired = _retired(messages)
    by_id = {m["hint_id"]:m for m in messages}
    forwards = {}
    forwarded_origin = {}
    result = []
    for message in messages:
        if message["action"] == "forward":
            if message["hint_id"] not in retired and set(message["visibility_refs"]) <= visible:
                parent = message["parent_hint_ids"][0]
                original = forwarded_origin.get(parent,parent)
                forwarded_origin[message["hint_id"]] = original
                forwards.setdefault(original, []).append(message)
            continue
        if message["event_id"] <= cursor or message["sender_scope"] == reader or message["hint_id"] in retired:
            continue
        if not set(message["visibility_refs"]) <= visible:
            # A narrower correction must still invalidate an earlier public reading.
            previous = by_id.get(message.get("replaces"))
            while previous and not set(previous["visibility_refs"]) <= visible:
                previous = by_id.get(previous.get("replaces"))
            if (not replay and previous and set(previous["visibility_refs"]) <= visible
                    and previous["event_id"] <= cursor):
                result.append({"event_id":message["event_id"], "topic":previous["topic"], "action":"withdrawn",
                               "replaces":previous["hint_id"]})
            continue
        if message["action"] != "retract" or not replay:
            result.append(_view(message))
    for entry in result:
        if entry.get("hint_id") in forwards:
            chain = forwards[entry["hint_id"]]
            entry["forwarded_by"] = [m["sender_scope"] for m in chain]
            entry["forward_hint_ids"] = [m["hint_id"] for m in chain]
    return result


def _permitted_parent(messages, hint_id, visible):
    found = next((m for m in messages if m["hint_id"] == hint_id), None)
    if found is None or not set(found["visibility_refs"]) <= visible:
        raise ScopeDenied("The parent hint is outside this topic or the researcher's visible materials")
    return found


def visible_refs(conn, member):
    from . import v62_modules
    return set(v62_modules.visible_refs(conn,member))


def _available(messages, visible):
    return sorted({m["topic"] for m in _project(messages,visible,0,replay=True)})


def _originals(conn, run_id, refs):
    if not refs:
        return []
    marks = ",".join("?" for ref in refs)
    # Use only recorded source metadata; reading a topic never parses a paper or reruns a Job.
    rows = conn.execute(f"""SELECT a.artifact_ref,a.sha256,a.kind,a.job_id,
        json_extract(s.provenance_json,'$.parsing.publication_identifiers') AS publications
        FROM artifacts a LEFT JOIN sources s ON s.blob_artifact_ref=a.artifact_ref
          AND s.namespace=? AND s.blob_sha256=a.sha256
        WHERE a.artifact_ref IN ({marks}) ORDER BY a.artifact_ref""", (run_id,*refs))
    found = {}
    for row in rows:
        key = row["artifact_ref"]
        item = found.setdefault(key,{k:row[k] for k in ("artifact_ref","sha256","kind","job_id") if row[k]})
        if row["publications"]:
            item["publication_identifiers"] = sorted(set(item.get("publication_identifiers",[]))
                                                     | set(json.loads(row["publications"])))
    return list(found.values())


def lead_preview(conn, run_id, goal_version, visible_refs, *, before_event=None, topic=None):
    """Current author interpretations, their independent origins and cited materials."""
    active = conn.execute("SELECT 1 FROM v6_goals WHERE run_id=? AND goal_version=? AND status='OPEN'",
                          (run_id,goal_version)).fetchone()
    if not active:
        return []
    messages = _messages(conn,run_id,goal_version,topic)
    findings = _project(messages,set(visible_refs),0,replay=True)
    if before_event is not None:
        findings = [m for m in findings if m["event_id"] < before_event]
    return findings[-PAGE_SIZE:]


def _page(conn, member, topic, sub, messages, visible, *, consume):
    rows = [m for m in messages if m["topic"] == topic]
    replay = sub["cursor"] < sub["high_water"]
    if replay:
        rows = [m for m in rows if m["event_id"] <= sub["high_water"]]
    entries = _project(rows,visible,sub["cursor"],member["scope"],replay=replay)
    more = len(entries) > PAGE_SIZE
    entries = entries[:PAGE_SIZE]
    through = sub["high_water"] if replay else max((m["event_id"] for m in rows),default=sub["cursor"])
    cursor = entries[-1]["event_id"] if more else max(through,sub["cursor"])
    if consume and cursor != sub["cursor"]:
        _append(conn,"GX",member,CONSUMED,member["scope"],{"topic":topic,"cursor":cursor})
    later = replay and bool(_project([m for m in messages if m["topic"] == topic],visible,
                                      sub["high_water"],member["scope"]))
    return {"topic":topic,"high_water":sub["high_water"],"cursor":cursor,"entries":entries,
            "has_more":more or later}


def context(conn, member):
    if isolated(member):
        return {"research_topics":[],"available_research_topics":[]}
    messages = _messages(conn,member["run_id"],member["goal_version"])
    visible = visible_refs(conn,member)
    return {"available_research_topics":_available(messages,visible),
            "research_topics":[_page(conn,member,k,v,messages,visible,consume=True)
                               for k,v in _subscriptions(conn,member).items()]}


def check_receipt(conn, member, tool, answer):
    """Replayed native callbacks still obey the live material and blind-review scope."""
    from . import research_handoff
    topic_receipt = "entries" in answer and "topic" in answer
    if not topic_receipt and "research_topics" not in answer:
        return answer
    if isolated(member):
        if topic_receipt or any(answer.get(key) for key in ("research_topics","available_research_topics","peer_hints","open_peers")):
            raise ScopeDenied("Peer communication is outside the current independent assignment")
        return answer
    visible = visible_refs(conn,member)
    messages = _messages(conn,member["run_id"],member["goal_version"])
    required = {m["hint_id"]: set(m["visibility_refs"]) for m in messages}
    pages = [answer] if topic_receipt else answer["research_topics"]
    for page in pages:
        for entry in page["entries"]:
            hint = entry.get("hint_id",entry.get("replaces"))
            hints = [hint,*entry.get("forward_hint_ids",[])]
            if any(i not in required or not required[i] <= visible for i in hints):
                raise ScopeDenied("The original receipt contains material outside the current grant")
    if tool == "gx_task_context":
        inbox = research_handoff.inbox(conn,member["run_id"],member["scope"])
        if any(hint not in inbox
               for hint in answer.get("peer_hints",[])):
            raise ScopeDenied("The original receipt contains a private peer hint")
        names = {m["topic"] for m in messages if set(m["visibility_refs"]) <= visible}
        if not set(answer.get("available_research_topics",[])) <= names:
            raise ScopeDenied("The original receipt names a topic outside the current grant")
    return answer


def pending(conn, member, *, after=0):
    """A scoped wake can advance a researcher while its own independent Jobs are in flight."""
    if isolated(member):
        return False
    subs = _subscriptions(conn,member)
    for name,sub in subs.items():
        cursor = max(after, sub["cursor"])
        changed = conn.execute("""SELECT 1 FROM events WHERE subject_kind='research_topic'
            AND subject_id=? AND event_id>? AND run_id=? AND kind=?
            AND json_extract(payload_json,'$.goal_version')=? LIMIT 1""",
            (name, cursor, member["run_id"], MESSAGE, member["goal_version"])).fetchone()
        if not changed:
            continue
        # Corrections and withdrawals need the earlier lineage only after a new event.
        messages = _messages(conn,member["run_id"],member["goal_version"],name)
        if _project(messages,visible_refs(conn,member),cursor,member["scope"]):
            return True
    return False


def _wake(conn, sender, message):
    from . import v62_modules, v6_goal
    peers = conn.execute("""SELECT * FROM v62_modules WHERE run_id=? AND goal_version=?
        AND status='OPEN' AND scope!=?""",(sender["run_id"],sender["goal_version"],sender["scope"])).fetchall()
    messages = _messages(conn,sender["run_id"],sender["goal_version"],message["topic"])
    for peer in peers:
        sub = _subscriptions(conn,peer).get(message["topic"])
        if isolated(peer) or not sub:
            continue
        if not any(m["event_id"] == message["event_id"] for m in
                   _project(messages,visible_refs(conn,peer),sub["cursor"],peer["scope"])):
            continue
        task = v62_modules._task(conn,peer)
        if not task or task["status"] not in v6_goal.ACTIVE_TASKS or not task["lease_id"]:
            continue   # The module tick uses the same unread cursor to schedule a fresh brief.
        key = f"{NOTICE}:{peer['module_instance_id']}:{message['hint_id']}"
        store.enqueue_outbox(conn,topic=NOTICE,idempotency_key=key,payload={
            "run_id":peer["run_id"], "goal_version":peer["goal_version"], "scope":peer["scope"],
            "task_id":task["task_id"], "lease_id":task["lease_id"], "event_id":message["event_id"]})


def notice_due(conn, payload):
    from . import v62_modules, v6_goal
    peer = conn.execute("""SELECT m.* FROM v62_modules m JOIN v6_goals g USING(run_id)
        WHERE m.run_id=? AND m.scope=? AND m.status='OPEN' AND g.status='OPEN'
        AND m.goal_version=g.goal_version AND m.goal_version=?""",
        (payload["run_id"], payload["scope"],payload["goal_version"])).fetchone()
    if not peer or isolated(peer):
        return False
    task = v62_modules._task(conn,peer)
    if not task or (task["task_id"],task["lease_id"]) != (payload["task_id"],payload["lease_id"]):
        return False
    if task["status"] not in v6_goal.ACTIVE_TASKS:
        return False
    # A newer update or a native context read can supersede an unsent notice. No text
    # or material identity crosses the host boundary in a wake-up, even if a grant changes.
    for name,sub in _subscriptions(conn,peer).items():
        messages = _messages(conn,peer["run_id"],peer["goal_version"],name)
        visible = _project(messages,visible_refs(conn,peer),sub["cursor"],peer["scope"])
        if visible and visible[-1]["event_id"] == payload["event_id"]:
            return True
    return False


def send(conn, member, args, native):
    from . import pi_materials
    if isolated(member):
        raise ScopeDenied("An isolated research assignment cannot exchange topic messages")
    topic, action = args.get("topic"), args.get("action")
    if not isinstance(topic,str) or not topic.strip() or len(topic) > 128 or topic.strip() != topic:
        raise SchemaInvalid("Use a nonempty research topic of up to 128 characters")
    choices = {"subscribe","publish","forward","correct","retract","history"}
    if not isinstance(action,str) or action not in choices:
        raise SchemaInvalid("Choose a research topic action: subscribe, publish, forward, correct, retract or history")
    visible = visible_refs(conn,member)
    messages = _messages(conn,member["run_id"],member["goal_version"],topic)
    if action == "history":
        parents = args.get("parent_hint_ids")
        if (set(args) != {"action","topic","parent_hint_ids"} or not isinstance(parents,list)
                or len(parents)!=1 or not isinstance(parents[0],str)):
            raise SchemaInvalid("Inspect one hint by its parent_hint_ids entry")
        target = _permitted_parent(messages,parents[0],visible)
        # Follow revision links, keeping separate interpretations of the same original distinct.
        chain = {target["hint_id"]}
        origin = target
        by_id = {m["hint_id"]:m for m in messages}
        while origin.get("replaces"):
            origin = by_id[origin["replaces"]]
            chain.add(origin["hint_id"])
        for message in messages:
            if message.get("replaces") in chain:
                chain.add(message["hint_id"])
        retired = _retired(messages)
        return {"topic":topic,"entries":[{**_view(m),"current":m["hint_id"] not in retired
                and m["action"] != "retract"} for m in messages if m["hint_id"] in chain
                and set(m["visibility_refs"]) <= visible]}
    if action == "subscribe":
        if set(args) != {"action","topic"}:
            raise SchemaInvalid("Topic subscriptions require action and topic")
        sub = _subscriptions(conn,member).get(topic)
        initial = sub is None
        if initial:
            high = conn.execute("SELECT coalesce(max(event_id),0) FROM events WHERE run_id=?",
                                (member["run_id"],)).fetchone()[0]
            sub = {"high_water":high,"cursor":0}
            _append(conn,native["threadId"],member,SUBSCRIBE,member["scope"],{"topic":topic,"high_water":high})
        return _page(conn,member,topic,sub,messages,visible,consume=True)

    fields = {"action","topic","parent_hint_ids"}
    if action in ("publish","correct"):
        fields.update(("question","interpretation","source_refs"))
    if set(args)-fields:
        raise SchemaInvalid("Use question, interpretation and source_refs for original or corrected work; a forward or retraction names only its parent")
    for field in ("question","interpretation"):
        if field in args and (not isinstance(args[field],str) or not args[field].strip() or len(args[field])>600):
            raise SchemaInvalid("Provide a short nonempty research question or interpretation (up to 600 characters each)")
    parents, refs = args.get("parent_hint_ids",[]),args.get("source_refs",[])
    for field, values in (("parent_hint_ids",parents),("source_refs",refs)):
        if (not isinstance(values,list) or len(values)>8 or not all(isinstance(x,str) and x for x in values)
                or len(values)!=len(set(values))):
            raise SchemaInvalid(f"Use up to eight different references for {field}")
    if action in ("forward","correct","retract") and len(parents)!=1:
        raise SchemaInvalid("Forward, correct or retract one current hint")
    if action in ("publish","correct") and not any(args.get(k) for k in ("question","interpretation","source_refs")):
        raise SchemaInvalid("Provide an open question, an interpretation or an original evidence reference")
    if not set(refs) <= visible:
        raise ScopeDenied("Cited original is outside this researcher's materials")
    parent_messages = [_permitted_parent(messages,i,visible) for i in parents]
    retired = _retired(messages)
    if any(m["hint_id"] in retired or m["action"]=="retract" for m in parent_messages):
        raise ScopeDenied("This parent has been withdrawn or replaced; inspect the current topic")
    replace = action in ("correct","retract")
    if replace and parent_messages[0]["sender_scope"]!=member["scope"]:
        raise ScopeDenied("Only the author can correct or withdraw this hint")
    dependencies = set(refs) | (visible & pi_materials.private_originals(conn,member["run_id"]))
    for m in parent_messages:
        dependencies.update(m["visibility_refs"])
    hint = ids.new_id("team_message")
    roots = sorted({root for m in parent_messages for root in m["origin_hint_ids"]}) or [hint]
    original_refs = sorted(set(refs).union(*(m.get("original_source_refs",m["source_refs"]) for m in parent_messages)))
    message = {"hint_id":hint,"action":action,"sender_scope":member["scope"],"parent_hint_ids":parents,
               "origin_hint_id":roots[0] if len(roots)==1 else None,"origin_hint_ids":roots,
               "source_refs":refs,"original_source_refs":original_refs,
               "originals":_originals(conn,member["run_id"],original_refs),"question":args.get("question"),
               "interpretation":args.get("interpretation"),"visibility_refs":sorted(dependencies),
               "author":{k:native[k] for k in ("threadId","turnId","callId")}}
    if replace:
        message["replaces"]=parents[0]
    eid = _append(conn,native["threadId"],member,MESSAGE,topic,message)
    if action!="forward":
        _wake(conn,member,{**message,"topic":topic,"event_id":eid})
    return {"topic":topic,"hint_id":hint,"event_id":eid,"origin_hint_ids":roots,"parent_hint_ids":parents}
