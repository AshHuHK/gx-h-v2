"""Checkpoint the Lead's own key evidence choices separately from closing, publishing and PI signing."""
import json

from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from ..persistence import store
from . import harness_policy
from .research_questions import Questions


LINK = "gx_link_core_material"
LINKED = "research.close.material_linked"
PROMPT = "research.close.material_prompt"
SIGNED = "research.close.pi_signed"
TEXT = {"type": "string", "minLength": 1}


def extend_decision(schema):
    fields = schema["properties"]
    if "stop_reason" in fields:
        fields["stop_reason"]["description"] = ("For a close caused by insufficient evidence use INSUFFICIENT_DATA, "
            "or UNREAD_EVIDENCE for delivered measurements left unread. Use SCIENTIFIC_COMPLETION when the "
            "scientific question is resolved, including a refuted premise.")
    variant = next(op for op in fields["ops"]["items"]["oneOf"]
                   if op["properties"]["op"]["const"] == "close_run")
    variant["properties"]["core_question"] = dict(TEXT, description=
        "On an insufficient-data close, identify the core question of the nominated records. Defaults to goal.")
    return schema


def validate_terms(terms, files):
    fields = {"signoff", "signer", "core_materials"}
    if not isinstance(terms, dict) or terms.keys() - fields:
        raise SchemaInvalid("closing_terms accepts core_materials, signoff and an optional signer")
    if "signoff" in terms and not _text(terms["signoff"]):
        raise SchemaInvalid("The formal PI signoff agreement needs its exact terms")
    if "signer" in terms and (not _text(terms["signer"]) or "signoff" not in terms):
        raise SchemaInvalid("A named signer requires a PI signoff agreement")
    items = terms.get("core_materials", [])
    roles = {entry.get("role") for entry in files}
    if not isinstance(items, list) or any(
            not isinstance(item, dict) or item.keys() - {"question", "role", "segment_id"}
            or not _text(item.get("role")) or item["role"] not in roles
            or not all(_text(v) for v in item.values()) for item in items):
        raise SchemaInvalid("Each PI core material requires an admitted role and optional question and segment_id")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _inputs(conn, rid):
    return json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])


def _events(conn, rid, kind):
    return conn.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind=? ORDER BY event_id",
                        (rid, kind))


def prepare_nomination(conn, task, args):
    """Visibility of a nomination at use is checked again on the deciding Lead's frozen brief."""
    rid, scope = task["run_id"], task["scope"]
    ref = args["ref"]
    module = conn.execute("SELECT scope FROM v62_modules WHERE run_id=? AND module_instance_id=? AND goal_version=?",
                          (rid, ref, task["goal_version"])).fetchone()
    if module:
        if scope not in ("run", module[0]) or set(args) & {"candidate_id", "segment_id", "fetch_job"}:
            raise ScopeDenied("Select a commissioned report within the assigned scope")
    elif ref not in json.loads(task["visible_refs_json"]):
        raise ScopeDenied("Key material must be visible in the nominating task")
    if any(not _text(value) for value in args.values()):
        raise SchemaInvalid("Identify the material and its relevance with nonempty text")
    if "candidate_id" in args:
        from ..artifacts import ArtifactService
        from adapters.scientific.publication_coverage import _aliases
        source = conn.execute("SELECT kind FROM artifacts WHERE artifact_ref=? AND run_id=? AND published=1",
                              (ref, rid)).fetchone()
        if not source or source[0] != "scholarly_candidates":
            raise SchemaInvalid("A candidate ID requires a published candidate list")
        doc = ArtifactService(conn).read_json(ref)
        if not isinstance(doc,dict) or doc.get("schema") != "gx.scholarly-candidates/1":
            raise SchemaInvalid("Select a typed scholarly candidate publication")
        try:
            paper = next((p for p in doc["candidates"] if any(v == args["candidate_id"].casefold()
                         for field,v in _aliases(p) if field in ("doi","pmcid","pmid"))), None)
        except (KeyError, AttributeError, TypeError) as error:
            raise SchemaInvalid("Invalid publication identifiers in the candidate artifact") from error
        if not paper:
            raise SchemaInvalid("The supplied publication identifier is absent from this candidate list")
    else:
        paper = None
    question = args.get("question", "goal")
    payload = {**args, "question":question, "scope":scope, "goal_version":task["goal_version"]}
    if harness_policy.current(conn, rid):
        brief = _frozen(conn, task)
        index, _ = _materials_for(conn, task, brief, set(json.loads(task["visible_refs_json"])))
        qid = index.lookup(question, create_for=scope)
        payload.update(question=index.labels[qid] if question == qid else question, question_id=qid)
        if not paper and not module:
            from . import research_report_facts
            if record := research_report_facts.query_record_for_ref(conn, rid, ref):
                payload["query_record"] = record
    if paper:
        payload["upstream_claims_fetchable"] = bool(paper.get("fetchable_pmcids"))
        payload["identifiers"] = [v for field,v in _aliases(paper) if field in ("doi","pmcid","pmid")]
    if "fetch_job" in args and (not paper or args["fetch_job"] not in {
            job for job, status in _fetch_jobs(conn, rid, ref, payload["identifiers"])}):
        raise ScopeDenied("A fetch Job must be bound to this exact candidate and list")
    return payload


def nominate(conn, task, actor, payload):
    rid, ref = task["run_id"], payload["ref"]
    if (payload["goal_version"],payload["scope"]) != (task["goal_version"],task["scope"]):
        raise ScopeDenied("The task changed during nomination")
    key = ids.sha256_of_obj(payload)
    recorded = conn.execute("SELECT event_id FROM events WHERE run_id=? AND kind=? AND subject_id=? LIMIT 1",
                            (rid, LINKED, key)).fetchone()
    answer = {key:payload[key] for key in ("question", "ref")}
    if "question_id" in payload:
        answer.update(question_id=payload["question_id"], goal_version=payload["goal_version"])
    if recorded:
        return dict(answer, event_id=recorded[0])
    eid = store.append_event(conn, run_id=rid, kind=LINKED, actor=actor, subject_kind="core_material",
                             subject_id=key, payload=payload)
    return dict(answer, event_id=eid)


def _fetch_jobs(conn, rid, candidates_ref, identifiers):
    return conn.execute("""SELECT j.job_id,j.status FROM p0_execution_manifests m JOIN jobs j USING(job_id)
        WHERE j.run_id=? AND EXISTS (SELECT 1 FROM json_each(m.admission_json,'$.output_declarations') o
            WHERE json_type(o.value,'$.acquired_material')='object')
          AND EXISTS (SELECT 1 FROM json_each(m.admission_json,'$.inputs') i
            WHERE json_extract(i.value,'$.artifact_ref')=?)
          AND EXISTS (SELECT 1 FROM json_each(m.admission_json,'$.parameters') p
            WHERE p.type='text' AND lower(p.value) IN (SELECT value FROM json_each(?)))""",
                        (rid, candidates_ref, json.dumps(identifiers)))


def _designated(conn, rid, version, visible, files, terms, *, through=None, viewer="run", questions=None):
    if version == 1:
        for item in terms.get("core_materials", []):
            for file in files:
                if file.get("role") == item["role"] and file.get("artifact_ref") in visible:
                    yield {"question":"goal", **item, "ref":file["artifact_ref"], "linked_by":"PI"}
    for row in _events(conn, rid, LINKED):
        if through is not None and row["event_id"] > through:
            break
        item = json.loads(row["payload_json"])
        if item["goal_version"] != version:
            continue
        if viewer != "run" and item["scope"] != viewer:
            if (item["scope"] != "run" or item["ref"] not in visible or questions is None
                    or item.get("question_id") not in questions.labels):
                continue
            # The parent explicitly granted this original. Project the shared
            # relationship, without disclosing the parent's private notes or Job.
            item = {k:v for k,v in item.items() if k not in ("reason", "fetch_job")}
            item["question"] = questions.labels[item["question_id"]]
        if item["scope"] not in ("run", viewer):
            own = conn.execute("""SELECT r.report_ref FROM v62_modules m JOIN v62_module_reports r
                USING(run_id,module_instance_id) WHERE m.run_id=? AND m.scope=?""", (rid,item["scope"])).fetchone()
            if not own or own[0] not in visible:
                continue
        if item["ref"] not in visible:
            if viewer != "run":
                continue
            released = conn.execute("SELECT report_ref FROM v62_module_reports WHERE run_id=? AND module_instance_id=?",
                                    (rid,item["ref"])).fetchone()
            if not released or released[0] not in visible:
                continue
            item["ref"] = released[0]
        yield {**item, "linked_by":item["scope"]}


def _frozen(conn, task):
    row = conn.execute("SELECT snapshot_json FROM research_contexts WHERE context_ref=?", (task["context_ref"],)).fetchone()
    return json.loads(row[0])


def _collect(conn, brief, visible, files, terms, *, through=None):
    index = Questions(brief)
    rid, version = brief["run_id"], brief["goal_version"]
    designated = _designated(conn, rid, version, visible, files, terms, through=through, viewer=brief["scope"], questions=index)
    items = [index.designate(item) for item in designated]
    for join in brief.get("research_joins", ()):
        qid = index.joins[join["key"]]
        for port in (join.get("outcome") or {}).get("ports", ()):
            ref = port.get("counted_artifact_ref")
            if port["required"] and ref in visible:
                items.append({"ref":ref, "question":index.labels[qid], "question_id":qid, "goal_version":version,
                              "linked_by":"join:" + join["key"], "reason":port["slot"]})
    return index, items


def _materials_for(conn, task, brief, visible):
    inputs = _inputs(conn, task["run_id"])
    terms = inputs["inputs"]["p0_spec"].get("closing_terms", {})
    return _collect(conn, brief, visible, inputs["files"], terms)


def owned_materials(conn, task, visible):
    return _materials_for(conn, task, _frozen(conn, task), visible)[1]


def attach(conn, brief, files, terms, cursor):
    visible = set(brief["evidence_refs"])
    if brief.get("harness_policy") != harness_policy.VERSION:
        items = _designated(conn, brief["run_id"], brief["goal_version"], visible, files, terms, through=cursor)
        core = [{k:item[k] for k in ("question","ref","role","reason","segment_id","candidate_id") if k in item}
                for item in items]
        if core:
            brief["core_materials"] = core
        return
    index, items = _collect(conn, brief, visible, files, terms, through=cursor)
    for join in brief.get("research_joins", ()):
        join["question_id"] = index.joins[join["key"]]
    for item in items:
        state = _state(conn, brief, item, visible)
        item["state"] = state[0] if state else "inspected"
    if items:
        brief["core_materials"] = [{k:item[k] for k in (
            "question","question_id","goal_version","ref","role","reason","linked_by","segment_id","candidate_id","state","query_record")
            if k in item} for item in items]


def read_authority_sql(column):
    return f"""(t.scope=? OR EXISTS (SELECT 1 FROM v62_module_reports mr
            JOIN v62_modules m USING(run_id,module_instance_id)
            WHERE mr.run_id=t.run_id AND m.scope=t.scope AND mr.report_ref IN (SELECT value FROM json_each(?))
              AND json_extract(mr.receipt_json,'$.author.kind')='MODULE'
              AND EXISTS (SELECT 1 FROM json_each(mr.receipt_json,'$.source_refs') refs WHERE refs.value={column})))"""


def _reads(conn, rid, scope, ref, segment, visible):
    """Only credit another author's located read when their signed delivery released that source."""
    releases = json.dumps(sorted(visible)) if scope == "run" else "[]"
    digest = conn.execute("SELECT sha256 FROM artifacts WHERE artifact_ref=? AND run_id=?", (ref,rid)).fetchone()
    if not digest:
        return False
    segment_read = conn.execute(f"""SELECT 1 FROM source_reads r JOIN sources s USING(source_id)
        JOIN v6_decision_tasks t ON t.task_id=r.task_id AND t.run_id=s.namespace
        JOIN source_segments seg ON seg.source_id=r.source_id AND seg.segment_id=r.segment_id
            AND seg.text_sha256=r.text_sha256
        WHERE {read_authority_sql('s.blob_artifact_ref')} AND s.namespace=? AND s.blob_sha256=? AND (r.segment_id=? OR (? IS NULL AND
            (s.text_coverage!='FULL_TEXT' OR seg.kind='body'))) LIMIT 1""",
                                (scope, releases, rid, digest[0], segment, segment)).fetchone()
    if segment_read:
        return True
    if segment and conn.execute(f"""SELECT 1 FROM sources original JOIN source_segments wanted
              ON wanted.source_id=original.source_id AND wanted.segment_id=?
            JOIN source_reads r ON r.text_sha256=wanted.text_sha256
            JOIN sources read ON read.source_id=r.source_id AND read.namespace=original.namespace
            JOIN source_segments seen ON seen.source_id=r.source_id AND seen.segment_id=r.segment_id
              AND seen.text_sha256=wanted.text_sha256 AND seen.kind=wanted.kind
            JOIN v6_decision_tasks t ON t.task_id=r.task_id AND t.run_id=read.namespace
            WHERE {read_authority_sql('read.blob_artifact_ref')} AND original.namespace=? AND original.blob_artifact_ref=?
              AND EXISTS (SELECT 1 FROM json_each(original.provenance_json,'$.parsing.publication_identifiers') a
                JOIN json_each(read.provenance_json,'$.parsing.publication_identifiers') b ON a.value=b.value)
            LIMIT 1""", (segment,scope,releases,rid,ref)).fetchone():
        return True
    # Unpaged JSON is delivered as its entire value. For paged artifacts inspect the
    # byte bounds in its stored native receipt, without loading large report bodies.
    rows = conn.execute(f"""WITH reads AS (
        SELECT t.scope,a.bytes,
            ((coalesce(a.media_type,'') IN ('application/json','application/ld+json')
              OR a.media_type LIKE '%+json' OR (coalesce(a.media_type,'')='' AND a.rel_path LIKE '%.json')
              OR a.media_type LIKE 'application/json;%' OR a.media_type LIKE '%+json;%')
               IS TRUE AND json_type(n.request_json,'$.arguments.offset') IS NULL) AS full_json,
            c.result_json AS page
        FROM p0_root_calls c JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
        JOIN p0_roots p USING(thread_id) JOIN v6_decision_tasks t ON t.task_id=p.task_id
        JOIN artifacts a ON a.artifact_ref=json_extract(n.request_json,'$.arguments.ref') AND a.run_id=t.run_id
        WHERE {read_authority_sql('a.artifact_ref')} AND t.run_id=? AND a.sha256=? AND json_extract(n.request_json,'$.tool')='gx_read_artifact'
          AND json_type(n.request_json,'$.arguments.endpoint_offset') IS NULL)
        SELECT scope,bytes,full_json,CASE WHEN NOT full_json THEN json_extract(page,'$.metadata.offset') END,
            CASE WHEN NOT full_json THEN json_extract(page,'$.metadata.returned_bytes') END FROM reads""",
                        (scope, releases, rid, digest[0]))
    ranges = {}
    for author, size, full_json, start, length in rows:
        if full_json:
            return True
        if length:
            ranges.setdefault((author, size), []).append((start, start + length))
    for (_, size), pages in ranges.items():
        covered = 0
        for start, end in sorted(pages):
            if start > covered:
                break
            covered = max(covered, end)
        if covered >= size:
            return True
    return False


def _state(conn, task, item, visible):
    rid, ref = task["run_id"], item["ref"]
    released = visible if harness_policy.current(conn, rid) else ()
    if item.get("identifiers"):
        if ref not in visible:
            return None
        aliases = set(item["identifiers"])
        acquired = []
        for r in conn.execute("SELECT blob_artifact_ref,provenance_json FROM sources WHERE namespace=?", (rid,)):
            if r[0] in visible:
                provenance = json.loads(r[1])
                original_ids = set(provenance.get("parsing", {}).get("publication_identifiers", []))
                if provenance.get("source_identity_status") == "matched_accession":
                    original_ids.add(provenance.get("external_source", {}).get("accession", ""))
                if aliases.intersection(str(x).casefold() for x in original_ids):
                    if _reads(conn, rid, task["scope"], r[0], item.get("segment_id"), released):
                        return None
                    acquired.append(r[0])
        if acquired:
            return ("acquired_unread", acquired[0])
        jobs = list(_fetch_jobs(conn, rid, ref, item["identifiers"]))
        if jobs and all(status in ("FAILED", "CANCELLED", "TIMED_OUT") for _,status in jobs):
            return ("fetch_failed",ref)
        return ("upstream_claims_fetchable" if item.get("upstream_claims_fetchable") else "candidate_listed",ref)
    if not ref or ref not in visible:
        return None
    record = item.get("query_record")
    if record and (record.get("status") == "unavailable" or any(
            window.get("error") or window.get("status") in ("unavailable", "timeout", "failed")
            or window.get("has_more") or (window.get("has_more") is None and window.get("next_cursor"))
            for window in record.get("sources", []))):
        return ("query_incomplete", ref)
    if _reads(conn, rid, task["scope"], ref, item.get("segment_id"), released):
        return None
    is_report = conn.execute("SELECT 1 FROM v62_module_reports WHERE run_id=? AND report_ref=?",
                                        (rid,ref)).fetchone()
    return ("report_delivered_unread" if is_report else "acquired_unread",ref)


def observed_prompt(conn, rid):
    return next(_events(conn, rid, PROMPT), None) is not None


def _advice(payload, event_id):
    materials = payload["materials"]
    return {**payload, "outcome":"core_evidence_available", "materials":materials[:6],
            "material_count":len(materials), "event_id":event_id,
            "next":"Inspect a relevant original, use equivalent evidence, or submit your closing decision with the remaining limitation."}


def maybe_prompt(conn, task, decision, *, already_observed=None):
    """Return and persist a non-terminal native answer in the current decision receipt transaction."""
    if decision.get("stop_reason") not in ("INSUFFICIENT_DATA", "UNREAD_EVIDENCE"):
        return None
    rid = task["run_id"]
    existing = next(_events(conn, rid, PROMPT), None)
    question = decision["ops"][0].get("core_question", "goal")
    visible = set(json.loads(task["visible_refs_json"]))
    items = {}
    index, nominations = _materials_for(conn, task, _frozen(conn, task), visible)
    current = harness_policy.current(conn, rid)
    qid = index.lookup(question) if current else None
    if existing:
        if already_observed is False:
            # An overlapping proposal was prepared before this callback committed.
            # Return the same fact so a concurrent retry cannot close over unseen advice.
            return _advice(json.loads(existing["payload_json"]), existing["event_id"])
        return None
    for item in nominations:
        if current and item["question_id"] != qid:
            continue
        if not current and item["question"] != question and not (
                question == "goal" and item["linked_by"].startswith("join:") and item["question"] == index.brief["goal"]):
            continue
        if result := _state(conn, task, item, visible):
            state, ref = result
            identity = (ref, item.get("segment_id"), item.get("candidate_id"))
            items[identity] = {"ref":ref, "state":state, "linked_by":item["linked_by"],
                               **{k:item[k] for k in ("reason","segment_id","candidate_id","fetch_job","query_record") if k in item}}
    if not items:
        return None
    rank = {"report_delivered_unread":0, "acquired_unread":1, "fetch_failed":2, "query_incomplete":2,
            "upstream_claims_fetchable":3, "candidate_listed":4}
    materials = sorted(items.values(), key=lambda it: (rank[it["state"]], it["ref"]))
    identity = {"question":question}
    if current:
        identity.update(question_id=qid, goal_version=task["goal_version"])
    eid = store.append_event(conn, run_id=rid, kind=PROMPT, actor="gx", subject_kind="run", subject_id=rid,
                             payload={**identity, "materials": materials})
    return _advice({**identity, "materials":materials}, eid)


def signoff(conn, rid, decision_ref, principal):
    from .research_report import completed_close
    agreement = _inputs(conn,rid)["inputs"]["p0_spec"].get("closing_terms", {})
    terms = agreement.get("signoff")
    if agreement.get("signer",principal) != principal:
        raise ScopeDenied("The local operator is not the signer named in the PI agreement")
    if not terms:
        raise SchemaInvalid("The PI did not request a formal signoff for this Goal")
    goal = conn.execute("SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
    if not goal or goal["status"] != "CLOSED" or goal["stop_decision_ref"] != decision_ref:
        raise ScopeDenied("Signoff requires the exact formally closed decision reference")
    existing = next(_events(conn, rid, SIGNED), None)
    if existing:
        return json.loads(existing["payload_json"])
    closure = completed_close(conn,rid)
    payload = {"status":"SIGNED", "agreement":terms, "decision_ref":decision_ref,
               "signer":principal, "closed_event":closure["closed_event"]}
    store.append_event(conn,run_id=rid,kind=SIGNED,actor=principal,subject_kind="decision",
                       subject_id=decision_ref,payload=payload)
    return payload


def signoff_status(conn, rid):
    saved = conn.execute("SELECT json_extract(inputs_json,'$.inputs.p0_spec.closing_terms') FROM runs WHERE run_id=?",
                         (rid,)).fetchone()[0]
    terms = json.loads(saved) if saved else {}
    agreement = terms.get("signoff")
    if not agreement:
        return None
    row = next(_events(conn,rid,SIGNED), None)
    return json.loads(row["payload_json"]) if row else {"status":"AWAITING_PI", "agreement":agreement,
        **({"signer":terms["signer"]} if "signer" in terms else {})}
