"""Project module measurements, host turn intervals and published evidence used by another route."""
from datetime import datetime
from functools import cache
from itertools import combinations
import hashlib
import json
from pathlib import Path

from ..artifacts import ArtifactService
from . import v6_native, v621_metrics, v62_outputs


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _history_turns(path, root):
    """Use only the official root's own turns. Delivery, reservations and queue time are separate."""
    def record(line):
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            return {}
        return value if isinstance(value, dict) and isinstance(value.get('payload'), dict) else {}

    turns = {}
    try:
        with path.open('rb') as stream:
            header = record(next(stream, b''))
            meta = header.get('payload', {})
            thread = root['thread_id']
            if (header.get('type') != 'session_meta' or meta.get('id') != thread
                    or meta.get('session_id', thread) != thread or meta.get('model_provider') != root['provider']):
                return turns
            active = None
            for line_number, line in enumerate(stream, 2):
                row = record(line)
                p = row.get('payload', {})
                kind, sub = row.get('type'), p.get('type')
                tid = p.get('turn_id')
                if tid is not None and not isinstance(tid, str):
                    continue
                timestamp = row.get('timestamp')
                if not isinstance(timestamp, str):
                    timestamp = None
                if kind == 'event_msg' and sub in ('task_started', 'turn_started') and tid:
                    active = tid
                    turns[tid] = dict(start=timestamp, end=None, model=None, response=False, report_reads={}, calls={})
                current = turns.get(tid or active)
                if current is None:
                    continue
                if kind == 'turn_context' and tid == active:
                    current['model'] = p.get('model')
                elif (kind == 'token_usage_record' and tid == active and p.get('thread_id') == thread
                      and isinstance(p.get('response_id'), str) and p['response_id']):
                    current['response'] = True
                elif kind == 'event_msg' and sub == 'item_completed' and tid == active:
                    item = p.get('item')
                    if (p.get('thread_id') != thread or not isinstance(item, dict)
                            or item.get('type') != 'DynamicToolCall' or not isinstance(item.get('id'), str) or not item['id']):
                        continue
                    current['calls'].setdefault(item['id'], line_number)
                    if item.get('tool') != 'gx_read_artifact' or item.get('success') is not True:
                        continue
                    parts = item.get('content_items')
                    args = item.get('arguments')
                    if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
                            or parts[0].get('type') != 'inputText' or not isinstance(parts[0].get('text'), str)
                            or not isinstance(args, dict) or not isinstance(args.get('ref'), str) or not args['ref']):
                        continue
                    try:
                        raw = parts[0]['text'].encode('utf-8')
                        body = json.loads(raw)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(body, dict) and body.get('findings'):
                        digest = hashlib.sha256(raw).hexdigest()
                        current['report_reads'].setdefault(args['ref'], (item['id'], line_number, digest))
                elif kind == 'response_item' and active and sub == 'function_call':
                    if not isinstance(p.get('call_id'), str) or not p['call_id']:
                        continue
                    current['calls'][p['call_id']] = line_number
                    current['response'] = True
                elif kind == 'response_item' and active and (sub in ('reasoning','custom_tool_call')
                        or sub == 'message' and p.get('role') == 'assistant'):
                    current['response'] = True
                elif kind == 'event_msg' and sub in ('task_complete','turn_complete','turn_aborted') and tid == active:
                    current['end'] = timestamp
                    active = None
    except OSError:
        # Preserve observations already read when the host file becomes unavailable.
        pass
    return turns


def _turns(conn, rid, directory, root):
    thread = root['thread_id']
    if not thread:
        return []
    located = conn.execute('SELECT path FROM p0_usage_host_histories WHERE run_id=? AND root_id=? AND thread_id=?',
                           (rid, thread, thread)).fetchone()
    path = Path(located[0]) if located else None
    if not path or not path.is_file():
        # Portable copies of releases retain the same official rollout filenames under the slot's codex home.
        files = list((Path(directory).parent/'codex/sessions').rglob(f'rollout-*-{thread}.jsonl'))
        path = files[0] if len(files) == 1 else None
    seen = _history_turns(path, root) if path else {}
    known = dict(conn.execute('SELECT turn_id,terminal_status FROM p0_root_turn_usage WHERE thread_id=? ORDER BY rowid', (thread,)))
    for (tid,) in conn.execute("SELECT DISTINCT turn_id FROM p0_root_deliveries WHERE thread_id=? AND state='acknowledged' AND turn_id IS NOT NULL ORDER BY rowid", (thread,)):
        known.setdefault(tid, None)
    out = []
    for tid in dict.fromkeys([*known, *seen]):
        span = seen.get(tid, {})
        verified_reads = {}
        for ref, (call, line, digest) in span.get('report_reads', {}).items():
            raw = conn.execute('''SELECT c.result_json FROM p0_root_calls c JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
                WHERE c.thread_id=? AND c.turn_id=? AND c.call_id=? AND json_extract(n.request_json,'$.tool')='gx_read_artifact'
                AND json_extract(n.request_json,'$.arguments.ref')=?''',(thread,tid,call,ref)).fetchone()
            if raw and hashlib.sha256(raw[0].encode()).hexdigest() == digest:
                verified_reads[ref] = (call,line)
        real = root['provider'] == 'openai' and span.get('model') == root['model'] and span.get('response', False)
        kind = 'NATIVE_MODEL' if real else 'SCRIPTED' if root['provider'] == 'gx_loopback' else 'UNVERIFIED'
        out.append(dict(turn_id=tid, thread_id=thread, provider=root['provider'], model=root['model'],
                        report_reads=verified_reads,tool_call_lines=span.get('calls',{}),
                        classification=kind, terminal_status=known.get(tid), start=span.get('start'), end=span.get('end')))
    return out


def _interval(a, b):
    bounds = [_instant(item.get(field)) for item in (a,b) for field in ('start','end')]
    if None in bounds:
        return None
    seconds = min(bounds[1], bounds[3]) - max(bounds[0], bounds[2])
    return round(seconds, 6) if seconds > 0 else None


def _jobs(conn, rid, branch):
    result = []
    for r in conn.execute('''SELECT n.node_id,n.job_id,n.status,n.spec_json,n.decision_ref,n.result_ref,
                j.status AS job_status,m.completion_json FROM v6_nodes n LEFT JOIN jobs j USING(run_id,job_id)
                LEFT JOIN p0_execution_manifests m ON m.job_id=n.job_id WHERE n.run_id=? AND n.branch_id=? ORDER BY n.rowid''',(rid,branch)):
        spec = json.loads(r['spec_json'])
        node = dict(node_id=r['node_id'],job_id=r['job_id'],status=r['status'],job_status=r['job_status'],
                    decision_ref=r['decision_ref'],capability=spec.get('capability'),parameters=spec.get('parameters'),
                    result_ref=r['result_ref'],outputs=(json.loads(r['completion_json'] or '{}') or {}).get('outputs', []),start=None,end=None)
        if node['job_id'] and node['status'] != 'REUSED':
            for event in conn.execute("SELECT new_state,prev_state,ts FROM events WHERE run_id=? AND kind='job.transition' AND subject_id=? ORDER BY event_id", (rid,r['job_id'])):
                if event['new_state'] == 'RUNNING':
                    node['start'] = event['ts']
                elif event['prev_state'] == 'RUNNING':
                    node['end'] = event['ts']
        result.append(node)
    return result


def _comparison_pair(conn, rid, output, selected, later_decisions):
    """Find an adopted comparison of this Job's measured endpoint and the report's reading."""
    if not selected or not later_decisions:
        return None
    for reading in v621_metrics.from_output(conn, rid, output):
        if reading['schema'] != v621_metrics.SCHEMA:
            continue
        for source in selected:
            if all(reading[key] == source[key] for key in ('method', 'metric_id', 'unit')):
                pair = {source['metric_ref'], reading['metric_ref']}
                for later in later_decisions:
                    if pair.issubset(later['used_metric_refs']):
                        return dict(compared_metric_refs=sorted(pair), comparison_decision_ref=later['decision_ref'])
    return None


def _finding_sources(report):
    refs = {source for finding in report.get('findings', []) for source in finding.get('source_refs', [])}
    refs.update(metric['artifact_ref'] for metric in report.get('verified_metrics', []))
    return refs


def _effects(conn, rid, decision, cited, report, publisher_branch, lineage, uses):
    ref = decision['decision_ref']
    metrics = report.get('verified_metrics', [])
    grounds = {source for finding in _finding_sources(report) for source in lineage(finding)}
    selected = [m for m in metrics if m['metric_ref'] in decision['used_metric_refs']]
    later = [d for d in uses if d['event_id'] > decision['event_id'] and d['used_metric_refs']]
    effects = []
    for row in conn.execute('''SELECT n.node_id,n.branch_id,n.job_id,n.result_ref FROM v6_nodes n
            JOIN jobs j USING(run_id,job_id) WHERE n.run_id=? AND n.decision_ref=? AND n.status='SUCCEEDED'
            AND j.status='SUCCEEDED' AND n.result_ref IS NOT NULL ORDER BY n.rowid''',(rid,ref)):
        matched = sorted(grounds.intersection(lineage(row['result_ref'])))
        if matched:
            effects.append(dict(kind='MEASUREMENT_COMPLETED', **dict(row), matched_source_refs=matched))
            continue
        comparison = _comparison_pair(conn, rid, row['result_ref'], selected, later)
        if comparison:
            effects.append(dict(kind='MEASUREMENT_COMPLETED', **dict(row), **comparison))
    for row in conn.execute("""SELECT event_id,kind,subject_id,payload_json FROM events WHERE run_id=? AND
            ((kind IN ('v6.claim.adopted','v62.claim.reassessed') AND json_extract(payload_json,'$.decision_ref')=?) OR
             (kind='v6.branch.stopped' AND json_extract(payload_json,'$.decision_id')=?)) ORDER BY event_id""",(rid,ref,ref)):
        data = json.loads(row['payload_json'])
        if row['kind'] == 'v6.branch.stopped':
            branch = row['subject_id']
            inputs = conn.execute("""SELECT a.artifact_ref FROM v6_nodes n, json_each(n.spec_json,'$.inputs') i
                        LEFT JOIN v6_nodes dep ON dep.run_id=n.run_id AND dep.node_id=i.value
                        JOIN artifacts a ON a.run_id=n.run_id AND a.artifact_ref=coalesce(dep.result_ref,i.value)
                        WHERE n.run_id=? AND n.branch_id=?""", (rid, branch))
            linked = branch == publisher_branch or any(grounds.intersection(lineage(item)) for (item,) in inputs)
        else:
            linked = cited.intersection(data.get('evidence_refs', []))
        if linked:
            effects.append(dict(kind=row['kind'],event_id=row['event_id'],subject=row['subject_id'],claim_revision_id=data.get('claim_revision_id')))
    return effects


def _handoffs(conn, rid, modules, roots, decisions, turns, directory):
    result = []
    scopes = {m['scope']:m for m in modules}
    assets = ArtifactService(conn, Path(directory)/'artifacts')

    @cache
    def lineage(ref):
        data = v62_outputs.classify(conn, rid, ref)['data_refs']
        return {ref, *(pin['artifact_ref'] for pin in data)}

    read_report = cache(assets.read_json)
    for m in modules:
        pub = m['report']
        if not pub:
            continue
        ref = pub['report_ref']
        author = pub.get('author', {})
        source_turn = turns.get((author.get('thread_id'),author.get('turn_id')), {})
        base = dict(publisher=m['scope'],report_ref=ref,published_event=pub['event_id'],released_event=pub['released_event'],
                    publisher_classification=source_turn.get('classification','UNVERIFIED'),real_model_handoff=False)
        found = False
        if pub['released_event'] is not None:
            for root in roots:
                scope = root['scope']
                if scope == m['scope']:
                    continue
                # A report handed to another module is pinned in its scoped material grant.
                if scope != 'run' and (scope not in scopes or ref not in scopes[scope]['delegated_reports']):
                    continue
                thread = root['thread_id']
                delivered = conn.execute("SELECT turn_id FROM p0_root_deliveries WHERE run_id=? AND thread_id=? AND state='acknowledged' AND method='turn/start' ORDER BY rowid", (rid,thread))
                for (tid,) in delivered:
                    if not root['brief'].startswith(v6_native.PREFIX):
                        continue
                    ack = v6_native.acknowledged_brief(conn,root,tid)
                    packet = ack[0] if ack else {}
                    cites = {ref}
                    materials = [x['artifact_ref'] for x in packet.get('materials', [])]
                    granted = scopes.get(scope,{}).get('report_materials', {}).get(ref, [])
                    cites.update(set(granted).intersection(materials))
                    turn = turns.get((thread,tid),{})
                    read_call = turn.get('report_reads',{}).get(ref)
                    summaries = [x for x in packet.get('module_reports', []) if x.get('report_ref') == ref]
                    indexed = [x for x in packet.get('module_report_index', []) if x.get('report_ref') == ref]
                    messages = [x['researcher_message'] for x in (*summaries, *indexed) if x.get('researcher_message')]
                    authored = any(message.get('author_kind') == 'MODULE' and
                                   (message.get('finding', {}).get('text') or message.get('gaps') or message.get('next_steps'))
                                   for message in messages)
                    brief_content = authored or any(x.get('findings') or x.get('endpoint_rows') for x in summaries)
                    in_brief = bool(ref in materials or cites.intersection(materials) or summaries or indexed)
                    read = [r[0] for r in conn.execute('''SELECT e.event_id FROM events e WHERE e.run_id=? AND e.kind='v623.endpoints.received'
                        AND e.actor=? AND json_extract(e.payload_json,'$.turn_id')=? AND json_extract(e.payload_json,'$.task_id')=?
                        AND EXISTS (SELECT 1 FROM json_each(e.payload_json,'$.endpoints') ep WHERE json_extract(ep.value,'$.source_ref')=?)
                        AND e.event_id>? ORDER BY e.event_id''',(rid,thread,tid,root['task_id'],ref,pub['released_event']))]
                    if not in_brief and not read and not read_call:
                        continue
                    entry = dict(base, receiver_scope=scope,thread_id=thread,turn_id=tid,
                                 handoff_kind="MODULE_TO_LEAD" if scope == "run" else "MODULE_TO_MODULE",
                                 delivery_receipt=ack[1]['event_id'] if in_brief else None,endpoint_delivery_events=read,
                                 received_content=bool(read or read_call or brief_content),
                                 received_read_call=read_call[0] if read_call else None, receiver_classification=turn.get('classification','UNVERIFIED'),
                                 status='DELIVERED',decision_ref=None,effects=[])
                    d = decisions.get((thread,tid))
                    if d and d['event_id'] > pub['released_event'] and cites.intersection(d['evidence_refs']):
                        entry.update(status='CITED',decision_ref=d['decision_ref'],decision_event=d['event_id'],rationale=d['rationale'])
                        observed_before = any(event < d['event_id'] for event in read)
                        decision_line = turn.get('tool_call_lines',{}).get(d['call_id'])
                        read_before = bool(read_call and decision_line and read_call[1] < decision_line)
                        if read_before or observed_before or brief_content:
                            report = read_report(ref)
                            cited_findings = {ref} | cites.intersection(_finding_sources(report))
                            effects = (_effects(conn,rid,d,cited_findings,report,m['branch_id'],lineage,decisions.values())
                                       if cited_findings.intersection(d['evidence_refs']) else [])
                            if effects:
                                entry.update(status='ENACTED',effects=effects)
                    receiver_report = scopes.get(scope,{}).get('report') or {}
                    writer = receiver_report.get('author',{})
                    if (writer.get('thread_id'),writer.get('turn_id')) == (thread,tid) and cites.intersection(receiver_report['source_refs']):
                        # Only findings that use the source qualify; listing the handle in the appendix does not.
                        followup = read_report(receiver_report['report_ref'])
                        cited_findings = {ref} | cites.intersection(_finding_sources(read_report(ref)))
                        findings = [f['text'] for f in followup.get('findings',[])
                                    if cited_findings.intersection(f.get('source_refs',[]))]
                        report_line = turn.get('tool_call_lines',{}).get(writer.get('call_id'))
                        if findings and (any(n < receiver_report['event_id'] for n in read) or
                                         read_call and report_line and read_call[1] < report_line):
                            entry['status'] = 'ENACTED'
                            entry['effects'].append(dict(kind='SOURCED_FINDING_PUBLISHED',event_id=receiver_report['event_id'],
                                report_ref=receiver_report['report_ref'],findings=findings))
                    entry['real_model_handoff'] = (entry['status'] == 'ENACTED' and
                        entry['publisher_classification'] == entry['receiver_classification'] == 'NATIVE_MODEL')
                    result.append(entry)
                    found = True
        if not found:
            result.append(dict(base,receiver_scope=None,decision_ref=None,effects=[],status='RELEASED' if pub['released_event'] else 'PUBLISHED'))
    return result


def routes(conn, rid, directory, *, goal_version=None):
    """Return the routes for the requested Goal version, defaulting to current work."""
    if goal_version is None:
        goal = conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
        goal_version = goal[0] if goal else None
    modules = []
    decisions = {}
    if not conn.execute('SELECT 1 FROM v62_modules WHERE run_id=? AND goal_version=? LIMIT 1',(rid, goal_version)).fetchone():
        return dict(modules=[],real_turn_overlaps=[],job_turn_overlaps=[],handoffs=[])
    roots = conn.execute('''SELECT r.*,t.scope FROM p0_roots r JOIN v6_decision_tasks t USING(run_id,task_id)
            WHERE r.run_id=? AND t.goal_version=? ORDER BY r.rowid''',(rid, goal_version)).fetchall()
    by_thread = {root['thread_id']:_turns(conn,rid,directory,root) for root in roots}
    turn_lookup = {(t['thread_id'],t['turn_id']):t for records in by_thread.values() for t in records}
    for r in conn.execute('''SELECT d.*,t.scope FROM v6_decisions d JOIN v6_decision_tasks t USING(run_id,task_id)
            WHERE d.run_id=? AND t.goal_version=? ORDER BY d.rowid''',(rid, goal_version)):
        receipt = json.loads(r['receipt_json'])
        spec = json.loads(r['request_json'])['decision']
        decisions[(r['thread_id'],r['turn_id'])] = dict(decision_ref=r['decision_ref'],scope=r['scope'],event_id=receipt['event_id'],
                thread_id=r['thread_id'],turn_id=r['turn_id'],call_id=r['call_id'],rationale=spec['rationale'],evidence_refs=spec.get('evidence_refs',[]),
                used_metric_refs=receipt.get('endpoint_use', {}).get('used_metric_refs', []),
                outcome=receipt.get('outcome'),operations=[item['spec']['op'] for item in receipt.get('resolved_ops',[])])
    for r in conn.execute('''SELECT m.*,p.receipt_json AS report_json FROM v62_modules m
            LEFT JOIN v62_module_reports p USING(run_id,module_instance_id) WHERE m.run_id=? AND m.goal_version=?
            ORDER BY m.round,m.rowid''',(rid, goal_version)):
        module = {k:r[k] for k in ('module_instance_id','scope','branch_id','focus','module_id','module_version','round','parent_task_id','goal_version','status')}
        report = json.loads(r['report_json']) if r['report_json'] else None
        if report:
            released = conn.execute('''SELECT min(w.event_id) FROM v62_module_wakes w JOIN events e USING(event_id)
                 WHERE w.run_id=? AND EXISTS (SELECT 1 FROM json_each(e.payload_json,'$.report_refs') WHERE value=?)''', (rid,report['report_ref'])).fetchone()[0]
            report['released_event'] = released
        grant = json.loads(r['snapshot_json'])
        threads = [dict(thread_id=root['thread_id'],task_id=root['task_id'],state=root['state'],turns=[{k:v for k,v in t.items() if k not in ('report_reads','tool_call_lines')}
                       for t in by_thread[root['thread_id']]])
                   for root in roots if root['scope'] == r['scope']]
        report_materials = {}
        for item in grant['materials']:
            source = item.get('publication',{}).get('report_ref')
            if source and not item.get('original_input'):
                report_materials.setdefault(source, []).append(item['artifact_ref'])
        readings = []
        for row in conn.execute('''SELECT e.event_id,e.ts,e.actor,e.payload_json FROM events e JOIN p0_roots r
                ON r.run_id=e.run_id AND r.thread_id=e.actor JOIN v6_decision_tasks t ON t.run_id=r.run_id AND t.task_id=r.task_id
                WHERE t.scope=? AND t.run_id=? AND e.kind='v623.endpoints.received' ORDER BY e.event_id''', (r['scope'],rid)):
            body = json.loads(row['payload_json'])
            readings.append(dict(event_id=row['event_id'],utc=row['ts'],thread_id=row['actor'],turn_id=body['turn_id'],
                                 endpoints=body['endpoints']))
        modules.append(dict(module,threads=threads,deliveries=readings,report_materials=report_materials,nodes=_jobs(conn,rid,r['branch_id']),report=report,
            delegated_reports=sorted({x['publication']['report_ref'] for x in grant['materials'] if x.get('publication',{}).get('report_ref')}),
            decisions=[d for d in decisions.values() if d['scope'] == r['scope']]))
    turns = [(m['scope'],t) for m in modules for thread in m['threads'] for t in thread['turns'] if t['classification'] == 'NATIVE_MODEL']
    overlaps = [dict(left_scope=a,right_scope=b,left_turn=t['turn_id'],right_turn=u['turn_id'],left_thread=t['thread_id'],right_thread=u['thread_id'],seconds=seconds)
                for (a,t),(b,u) in combinations(turns,2) if a != b and t['thread_id'] != u['thread_id'] and (seconds := _interval(t,u))]
    job_overlaps = [dict(job_id=j['job_id'],job_scope=m['scope'],turn_id=t['turn_id'],thread_id=t['thread_id'],turn_scope=scope,seconds=seconds)
                    for m in modules for j in m['nodes'] for scope,t in turns if m['scope'] != scope and (seconds := _interval(j,t))]
    return dict(modules=modules,real_turn_overlaps=overlaps,job_turn_overlaps=job_overlaps,handoffs=_handoffs(conn,rid,modules,roots,decisions,turn_lookup,directory))


def route_lines(data, render_decision=None):
    def rationale(decision):
        text = decision['rationale']
        if render_decision is None:
            return v621_metrics.render_accepted_text(text, [])
        return render_decision(decision['decision_ref'], text)

    lines = ['## Research routes', '']
    if not data['modules']:
        return lines + ['The Lead worked directly in this Goal version.', '']
    for m in data['modules']:
        lines.extend([f"### {m['module_id']} · {m['scope']}", '', m['focus'], '', f"Round {m['round']} · {m['status']}", ''])
        for root in m['threads']:
            for t in root['turns']:
                lines.append(f"- {t['classification']} · `{t['thread_id']}` / `{t['turn_id']}` · {t['start'] or 'unknown'} to {t['end'] or 'unknown'}")
        for node in m['nodes']:
            outputs = ', '.join(f"{o['output_name']} `{o['artifact_ref']}`" for o in node['outputs'])
            lines.append(f"- {node['capability']} · `{node['job_id'] or node['node_id']}` · {node['status']} · {outputs}")
        for delivery in m['deliveries']:
            lines.append(f"- Delivery event {delivery['event_id']} · {len(delivery['endpoints'])} endpoints · `{delivery['turn_id']}`")
        for decision in m['decisions']:
            lines.append(f"- Accepted decision {decision['event_id']} · {rationale(decision)}")
        lines.append('')
    lines.extend([f"Observed overlaps between real module turns: {len(data['real_turn_overlaps'])}.", ''])
    for overlap in data['real_turn_overlaps']:
        lines.append(f"- `{overlap['left_turn']}` and `{overlap['right_turn']}` · {overlap['seconds']} s")
    for overlap in data['job_turn_overlaps']:
        lines.append(f"- Job `{overlap['job_id']}` and another module's turn `{overlap['turn_id']}` · {overlap['seconds']} s")
    for item in data['handoffs']:
        lines.append(f"- {item['publisher']} → {item['receiver_scope'] or 'awaiting receipt'} · `{item['report_ref']}` · {item['status']} · source {item['publisher_classification']} / receiver {item.get('receiver_classification','unknown')}"
                     + (f" · `{item['decision_ref']}` · {rationale(item)}" if item['decision_ref'] else ''))
        for effect in item['effects']:
            identity = effect.get('job_id') or effect.get('report_ref') or effect.get('subject')
            lines.append(f"  - {effect['kind']} · `{identity}`")
    return lines + ['']
