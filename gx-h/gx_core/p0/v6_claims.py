"""Scoped Lead claims. A native call stages one reading; decision adoption publishes it.

The existing hypothesis revision is the immutable scientific record. Its source
identities and selection criteria survive a later reinterpretation of the same bytes.
"""
from __future__ import annotations

import json

from .. import contracts
from ..artifacts.service import ArtifactService
from ..domain import clock, ids
from ..errors import IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from . import manifest


SCHEMA = 'gx.v6.claim.1'
DRAFT_KIND = 'v6_claim_proposal'

def tool_spec():
    return contracts.native_goal_tool_spec("gx_stage_claim")


LEVELS = tuple(tool_spec()["inputSchema"]["properties"]["level"]["enum"])
REQUIRED = tuple(tool_spec()["inputSchema"]["required"])


def _validate(payload):
    schema = tool_spec()['inputSchema']
    if not isinstance(payload, dict) or not set(REQUIRED) <= payload.keys() or payload.keys() - schema['properties'].keys():
        raise SchemaInvalid('claim needs the documented statement, scope, criteria, limits, rationale, level and grounds')
    for field, value in payload.items():
        if field in ('evidence_refs', 'chain_pairs'):
            if (not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value)
                    or len(set(value)) != len(value)):
                raise SchemaInvalid(f'{field} must contain distinct nonempty strings')
        elif not isinstance(value, str) or not value.strip():
            raise SchemaInvalid(f'claim {field} must be nonempty text')
    if payload['level'] not in LEVELS or not payload['evidence_refs']:
        raise SchemaInvalid('only bounded levels grounded in identified evidence can be staged')


def active_context(conn, run, task, lease, context, thread, turn):
    from .v6_decisions import authority
    _, delegation, _, _ = authority(conn, run_id=run, task_id=task, lease_id=lease, thread_id=thread, turn_id=turn)
    if delegation['context_ref'] != context:
        raise ScopeDenied('scientific tool requires the exact frozen decision context')
    return delegation


def _has_number(value):
    if isinstance(value, dict):
        return any(_has_number(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_number(v) for v in value)
    return type(value) in (int, float)


def _ground(conn, run, ref, observations, context):
    # A source alias can be reused by different branches. Require the immutable
    # artifact reference when multiple authorized originals have that name.
    matching = [item for item in observations if ref in (item.get('id'), item.get('subject_ref'), item.get('artifact_ref'))]
    known = conn.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v6.observation.added' "
                         "AND event_id<=? AND (subject_id=? OR json_extract(payload_json,'$.artifact_ref')=?) "
                         "AND (?='run' OR json_extract(payload_json,'$.scope') IN ('run',?)) ORDER BY event_id DESC",
                         (run, context['event_cursor'], ref, ref, context['scope'], context['scope'].removeprefix('branch:')))
    for event in known:
        matching.append(dict(store.loads(event, 'payload_json'), event_id=event['event_id']))
    originals = {item.get('artifact_ref') for item in matching}
    if len(originals) > 1:
        raise SchemaInvalid(f'{ref} names multiple originals; cite the exact artifact reference')
    observation = matching[0] if matching else {}
    artifact_ref = observation.get('artifact_ref') or ref
    row = conn.execute('SELECT a.*,j.status AS job_status,j.capability_id,j.capability_version FROM artifacts a '
                       'LEFT JOIN jobs j ON j.run_id=a.run_id AND j.job_id=a.job_id '
                       'WHERE a.run_id=? AND a.artifact_ref=? AND a.published=1', (run, artifact_ref)).fetchone()
    if not row or row['kind'] == DRAFT_KIND:
        raise ScopeDenied(f'{ref} does not resolve to a recorded source in this run')
    fact = {'ref': ref, 'artifact_ref': row['artifact_ref'], 'sha256': row['sha256'], 'kind': row['kind']}
    if row['job_id']:
        fact.update(job_id=row['job_id'], job_status=row['job_status'], capability=row['capability_id'],
                    version=row['capability_version'], source_class='execution_record')
        node = conn.execute("SELECT 1 FROM v6_nodes WHERE run_id=? AND job_id=? AND result_ref=? "
                            "AND status IN ('SUCCEEDED','REUSED')", (run, row['job_id'], row['artifact_ref'])).fetchone()
        if node and row['job_status'] == 'SUCCEEDED' and row['origin'] == 'JOB_OUTPUT':
            record = manifest.read_manifest(conn, row['job_id'])
            result = record['completion'] or {}
            if (record['admission']['run_id'] != run or result.get('status') != 'SUCCEEDED' or not any(
                    out['artifact_ref'] == row['artifact_ref'] and out['sha256'] == row['sha256']
                    for out in result.get('outputs', []))):
                raise SchemaInvalid('result provenance does not match its native execution manifest')
            fact.update(source_class='computed_result', admission_sha256=record['admission_hash'],
                        completion_sha256=record['manifest_hash'], inputs=record['admission']['inputs'])
    elif row['kind'] == 'geometry_reevaluation' and row['origin'] == 'BACKEND':
        value = ArtifactService(conn).read_json(row['artifact_ref'])
        if value.get('schema') != 'gx.v6.geometry_reevaluation/1':
            raise SchemaInvalid('unexpected retrospective evaluation schema')
        fact.update(source_class='retrospective_evaluation', criteria_ref=value['criteria_ref'],
                    criteria_sha256=value['criteria_sha256'], evaluator_sha256=value['evaluator_sha256'],
                    measurements=value['measurements'])
    elif observation:
        body = observation.get('observation')
        if body is None:
            body = ArtifactService(conn).read_json(artifact_ref)
        # A timestamp or identifier in an arbitrary operator note does not make it a measurement.
        measurement = (body if row['kind'] == 'synthetic_observation' else
                       body.get('measurement') if isinstance(body, dict) else None)
        fact['source_class'] = 'supplied_measurement' if _has_number(measurement) else 'supplied_information'
        fact['source_event_id'] = observation.get('event_id')
    else:
        fact['source_class'] = 'input'
    return fact


def read_revision(conn, run, claim_ref):
    row = conn.execute("SELECT r.* FROM hypothesis_revisions r JOIN hypotheses h USING(hypothesis_id) "
                       "WHERE h.run_id=? AND json_extract(r.document_json,'$.schema')=? "
                       "AND (r.proposal_id=? OR json_extract(r.document_json,'$.claim_revision_id')=?)",
                       (run, SCHEMA, claim_ref, claim_ref)).fetchone()
    return json.loads(row['document_json']) if row else None


def current(conn, run_id):
    rows = conn.execute("SELECT r.document_json FROM hypotheses h JOIN hypothesis_revisions r "
                        "ON h.hypothesis_id=r.hypothesis_id AND h.head_revision=r.revision WHERE h.run_id=? "
                        "AND json_extract(r.document_json,'$.schema')=? ORDER BY h.hypothesis_id", (run_id, SCHEMA))
    return [json.loads(row[0]) for row in rows]


def lookup_replay(conn, key, request):
    row = conn.execute('SELECT request_sha,response_json,completed_at FROM idempotency WHERE idempotency_key=?',
                       (key,)).fetchone()
    if row:
        if row['request_sha'] != ids.sha256_of_obj(request):
            raise IdempotencyConflict('native scientific tool call was already used with a different request')
        if row['completed_at']:
            return json.loads(row['response_json'])
    return None


def evaluation_refs(conn, run, task, lease, thread, turn):
    rows = conn.execute("SELECT json_extract(payload_json,'$.evaluation_ref') FROM events "
                        "WHERE run_id=? AND kind='v6.geometry.reevaluated' AND subject_id=? "
                        "AND json_extract(payload_json,'$.lease_id')=? AND json_extract(payload_json,'$.thread_id')=? "
                        "AND json_extract(payload_json,'$.turn_id')=?", (run, task, lease, thread, turn))
    return {row[0] for row in rows}


def stage(conn, run_id, task_id, lease_id, context_ref, payload, *, allowed_refs, thread_id, turn_id, call_id):
    _validate(payload)
    binding = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, context_ref=context_ref,
                   thread_id=thread_id, turn_id=turn_id, call_id=call_id)
    if any(not isinstance(v, str) or not v for v in binding.values()):
        raise ScopeDenied('missing native claim call identity')
    key = 'v6-claim:' + ids.sha256_of_obj([thread_id, call_id])
    request = dict(binding=binding, claim=payload)
    replay = lookup_replay(conn, key, request)
    if replay:
        return replay
    task = active_context(conn, run_id, task_id, lease_id, context_ref, thread_id, turn_id)
    visible = set(json.loads(task['visible_refs_json'])) & set(allowed_refs)
    visible |= evaluation_refs(conn, run_id, task_id, lease_id, thread_id, turn_id)
    if not set(payload['evidence_refs']) <= visible:
        raise ScopeDenied('claim cites evidence outside the presented context')
    brief = json.loads(task['snapshot_json'])
    sources = [_ground(conn, run_id, ref, brief.get('observations', []), task) for ref in payload['evidence_refs']]
    if (payload['level'] in ('limited_support', 'contested', 'mixed', 'refuted')
            and not any(s['source_class'] in ('computed_result', 'supplied_measurement', 'retrospective_evaluation') for s in sources)):
        raise SchemaInvalid('this level requires a result or measurement; execution and input records are insufficient')
    previous = payload.get('previous_claim_ref')
    if previous:
        prior = read_revision(conn, run_id, previous)
        if previous not in visible or not prior or (task['scope'] != 'run' and prior['decision_scope'] != task['scope']):
            raise ScopeDenied('previous claim is absent or outside the decision scope')
    draft = dict(schema=SCHEMA, **binding, decision_scope=task['scope'], event_cursor=task['event_cursor'],
                 claim=payload, sources=sources)
    digest = ids.sha256_of_obj(draft)
    # The artifact is inert until an authenticated staging event references it.
    artifact = ArtifactService(conn).publish_bytes(namespace=f'{run_id}/claims', rel_path=f'{digest}.json',
                  data=ids.canonical_json(draft), media_type='application/json', kind=DRAFT_KIND,
                  run_id=run_id, task_id=task_id, actor=thread_id, origin='HOST_PUT')
    with store.transaction(conn, transaction_owner='v6.claims', operation='stage', run_id=run_id):
        idem = store.begin_idempotent(conn, key, 'v6.stage_claim', request)
        if idem.replay:
            return idem.response
        active_context(conn, run_id, task_id, lease_id, context_ref, thread_id, turn_id)
        store.append_event(conn, run_id=run_id, kind='v6.claim.staged', actor=thread_id, subject_kind='artifact',
                           subject_id=artifact.artifact_ref, payload=draft)
        answer = {'status': 'PROPOSED', 'claim_ref': artifact.artifact_ref, 'sha256': digest}
        store.complete_idempotent(conn, key, answer)
    return answer


def _drafts(conn, run, task, lease, context, refs):
    if not refs:
        return {}
    rows = conn.execute("SELECT a.artifact_ref,a.sha256,e.payload_json FROM artifacts a JOIN events e "
                        "ON a.run_id=e.run_id AND e.subject_id=a.artifact_ref AND e.kind='v6.claim.staged' "
                        "WHERE a.run_id=? AND a.task_id=? AND a.kind=? AND a.artifact_ref IN "
                        "(SELECT value FROM json_each(?))", (run, task, DRAFT_KIND, ids.canonical_json(list(refs))))
    result = {}
    for row in rows:
        draft = json.loads(row['payload_json'])
        if (draft['lease_id'] == lease and draft['context_ref'] == context
                and ids.sha256_of_obj(draft) == row['sha256']):
            result[row['artifact_ref']] = draft
    return result


def staged_refs(conn, run_id, task_id, lease_id, context_ref, refs):
    return list(_drafts(conn, run_id, task_id, lease_id, context_ref, refs))


def adopt(conn, run_id, task_id, lease_id, context_ref, decision_ref, decision):
    """Called in the same CAS transaction as the accepted decision and outbox."""
    if not conn.in_transaction:
        raise RuntimeError('claim adoption requires the decision adoption transaction')
    cited = set(decision['evidence_refs'])
    requested = cited | {ref for op in decision['ops'] for ref in op.get('claim_refs', ())}
    drafts = _drafts(conn, run_id, task_id, lease_id, context_ref, requested)
    receipts = []
    for ref, draft in drafts.items():
        claim = draft['claim']
        if not set(claim['evidence_refs']) <= cited:
            raise SchemaInvalid('an adopted claim must cite its original evidence in this decision')
        previous = read_revision(conn, run_id, claim['previous_claim_ref']) if 'previous_claim_ref' in claim else None
        principal = draft['thread_id']
        # Adoption's caller verifies the current Goal, graph head, task, scope and lease before this call.
        now = clock.now_iso()
        if previous:
            hid, parent = previous['hypothesis_id'], previous['revision']
            cur = conn.execute('UPDATE hypotheses SET head_revision=head_revision+1 WHERE run_id=? '
                               'AND hypothesis_id=? AND head_revision=?', (run_id, hid, parent))
            if cur.rowcount != 1:
                raise StaleRevision('claim has a newer revision; read its latest recorded interpretation')
        else:
            hid, parent = ids.new_id('hypothesis'), None
            conn.execute('INSERT INTO hypotheses (hypothesis_id,run_id,head_revision,created_by,created_at) '
                         'VALUES(?,?,1,?,?)', (hid, run_id, principal, now))
        revision = (parent or 0) + 1
        science = {'level': claim['level'], 'chain_pairs': claim.get('chain_pairs', [])}
        receipt = dict(schema=SCHEMA, hypothesis_id=hid, revision=revision, claim_revision_id=f'{hid}@{revision}',
                       claim_ref=ref, decision_id=decision_ref, decision_scope=draft['decision_scope'],
                       context_ref=draft['context_ref'], event_cursor=draft['event_cursor'],
                       criteria_sha256=ids.sha256_of_obj(claim['criteria']), source_records=draft['sources'],
                       science=science, **claim)
        conn.execute('INSERT INTO hypothesis_revisions (hypothesis_id,revision,parent_revision,document_json,sha256,'
                     'author_principal,author_task_id,proposal_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                     (hid, revision, parent, ids.canonical_json(receipt), ids.sha256_of_obj(receipt), principal,
                      task_id, ref, now))
        store.append_event(conn, run_id=run_id, kind='v6.claim.adopted', actor=principal, subject_kind='hypothesis',
                           subject_id=hid, prev_revision=parent, new_revision=revision,
                           payload={'decision_ref': decision_ref, 'claim_revision_id': receipt['claim_revision_id'],
                                    'claim_ref': ref, 'evidence_refs': claim['evidence_refs']})
        receipts.append(receipt)
    return receipts
