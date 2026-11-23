"""Reconcile published totals and classify SHA-indexed official rollouts from the original paid runs."""
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'gx-h'))
from gx_core.p0 import response_attribution as attr

PERFORMANCE = ROOT / 'acceptance/v3/performance'
ARMS = [('gate0', 'baseline', 'v21-gate0-xhigh'), ('p1', 'candidate', 'p1-2476f2b-xhigh')]
COUNTERS = {'input_tokens': 'inputTokens', 'cached_input_tokens': 'cachedInputTokens',
            'output_tokens': 'outputTokens', 'reasoning_output_tokens': 'reasoningOutputTokens',
            'total_tokens': 'totalTokens'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def memory_db():
    db = sqlite3.connect(':memory:', isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript((ROOT / 'gx-h/gx_core/p0/zz_v31_response_attribution.sql').read_text())
    db.executescript('''CREATE TABLE p0_meter_responses(thread_id,turn_id);
        CREATE TABLE p0_native_child_meters(child_id,run_id,metadata_verified);
        CREATE TABLE p0_roots(root_key,run_id,thread_id,task_id,attempt,role,model,provider);''')
    return db


def receipts(directory):
    with gzip.open(directory / 'receipts.jsonl.gz', 'rt') as stream:
        records = defaultdict(list)
        for rec in map(json.loads, stream):
            records[rec['kind']].append(rec['data'])
    return records


def classify_arm(arm, label, private, callbacks):
    directory = PERFORMANCE / arm
    records = receipts(directory)
    roots = {r['thread_id']: r for r in records['root']}
    turns = {(r['thread_id'], r['turn_id']): r for r in records['official_native_turn']}
    verdicts = {r['submission_id']: r['result'] for r in records['submission']}
    hashes = json.loads((directory / 'native-session-hashes.json').read_text())
    files = {p.name: p for p in (private / 'codex-home/sessions').rglob('*.jsonl')}
    # Compare the exact IDs/totals with the original response receipts, opened read-only.
    db_receipts = {}
    for source in sorted(private.glob('state-*/gx.db')):
        c = sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro&immutable=1', uri=True)
        try:
            for tid, turn, rid, total, conflict in c.execute(
                    'SELECT thread_id,turn_id,response_id,tokens,conflicting FROM p0_meter_responses'):
                assert not conflict
                db_receipts[tid, turn, rid] = total
        finally:
            c.close()
    db = memory_db()
    stamps, seen_callbacks = {}, set()
    for meta in hashes:
        tid = meta['thread_id']
        path = files[meta['file']]
        assert digest(path) == meta['sha256'] and path.stat().st_size == meta['bytes']
        root = roots[tid]
        db.execute('INSERT INTO p0_roots VALUES(?,?,?,?,?,?,?,?)', tuple(root[k] for k in
                   ('root_key', 'run_id', 'thread_id', 'task_id', 'attempt', 'role', 'model', 'provider')))
        observer = attr.Attribution(db, root['run_id'])
        turn = None
        with path.open() as stream:
            for line in map(json.loads, stream):
                payload = line['payload']
                kind, event = line['type'], payload.get('type')
                if kind == 'event_msg' and event in ('task_started', 'turn_started'):
                    turn = payload['turn_id']
                    assert (tid, turn) in turns
                    observer.start(tid, turn)
                elif kind == 'response_item' and turn:
                    observer.item(tid, turn, payload)
                elif kind == 'token_usage_record' and turn:
                    rid = payload['response_id']
                    assert (payload['thread_id'], payload['turn_id'], payload['root_turn_id']) == (tid, turn, turn)
                    assert db_receipts[tid, turn, rid] == payload['usage']['total_tokens']
                    assert (tid, turn, rid) not in stamps
                    stamps[tid, turn, rid] = line['timestamp']
                    usage = {target: payload['usage'][key] for key, target in COUNTERS.items() if key in payload['usage']}
                    observer.completed(tid, tid, turn, {'responseId': rid, 'usage': usage})
                elif kind == 'event_msg' and event == 'item_completed' and turn:
                    item = payload.get('item') or {}
                    if item.get('type') != 'DynamicToolCall':
                        continue
                    key = (tid, turn, item['id'])
                    original = callbacks.get(key)
                    assert original and original['tool'] == item['tool']
                    assert key not in seen_callbacks
                    seen_callbacks.add(key)
                    result = 'SUCCESS' if item.get('success') is True else 'ERROR'
                    if original['submission_id']:
                        result = {'ACCEPTED': 'ACCEPTED', 'REJECTED': 'TERMINAL_REJECTED'}[verdicts[original['submission_id']]]
                    elif original['tool'] == 'gx_submit_product' and original['tool_error'] == 'SCHEMA_INVALID':
                        result = 'SCHEMA_INVALID'
                    observer.outcome({'threadId': tid, 'turnId': turn, 'callId': item['id'], 'tool': item['tool']}, result)
                elif kind == 'event_msg' and event in ('task_complete', 'turn_complete', 'turn_aborted') and turn:
                    observer.end(tid, turn)
                    turn = None
    assert set(stamps) == db_receipts.keys() and seen_callbacks == callbacks.keys()
    rows, unlinked = [], []
    for run in records['run']:
        report = attr.report(db, run['run_id'])
        for row in report['responses']:
            row['observed_at'] = stamps[row['thread_id'], row['turn_id'], row['response_id']]
            row['arm'] = arm
            rows.append(row)
        unlinked.extend(report['unlinked_tools'])
    db.close()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['thread_id'], row['turn_id']].append(row)
    assert set(grouped) == set(turns)
    for key, group in grouped.items():
        assert sum(row['total_tokens'] for row in group) == turns[key]['tokens']['total_tokens']
        assert len(group) == turns[key]['native_responses']
    first = [r['input_tokens'] for r in rows if r['request_ordinal'] == 1]
    return rows, {'paid_arm': label, 'published_input_sha256': {
        name: digest(directory / name) for name in ('receipts.jsonl.gz', 'native-session-hashes.json')},
        'sessions_verified': len(hashes), 'runs': len(records['run']), 'turns': len(turns),
        'request_count': len(rows), 'total_tokens': sum(r['total_tokens'] for r in rows),
        'reviewer_first_submissions': dict(Counter(r['result'] for r in records['submission']
                                                   if r['role'] == 'evidence_reviewer' and r['attempt'] == 1)),
        'published_aggregate_first_request_input_tokens': None,
        'direct_rollout_first_inputs': {'count': len(first), 'minimum': min(first), 'maximum': max(first),
                                        'sum': sum(first), 'mean': sum(first) / len(first)},
        'matched_callbacks': len(seen_callbacks), 'unlinked_tools': unlinked, 'by_class': attr.summarize(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    calls_path = PERFORMANCE / 'callback-samples.jsonl.gz'
    callbacks = defaultdict(dict)
    with gzip.open(calls_path, 'rt') as source:
        for row in map(json.loads, source):
            callbacks[row['arm']][row['thread_id'], row['turn_id'], row['call_id']] = row
    all_rows = []
    summary = {'classification_version': attr.RULE_VERSION, 'callback_sha256': digest(calls_path),
               'source': 'Input/output/total are actual token_usage_record.usage from hash-indexed native rollouts. '
                         'No values reconstructed from turn totals. Published aggregate receipts alone lack the breakdown. '
                         'Zero optional cache/reasoning details are null because Codex normalizes omission to zero. '
                         'Model/provider are absent on completions; requested settings come from the root receipt. '
                         'observed_at is the native rollout timestamp. Tools are actual call IDs/names; no model prose used.',
               'arms': {}}
    for arm, label, folder in ARMS:
        rows, evidence = classify_arm(arm, label, args.private / folder, callbacks[label])
        all_rows.extend(rows)
        summary['arms'][arm] = evidence
    output = args.output / 'historical-responses.jsonl.gz'
    with gzip.GzipFile(filename=str(output), mode='wb', mtime=0) as stream:
        for row in all_rows:
            stream.write((json.dumps(row, separators=(',', ':'), sort_keys=True) + '\n').encode())
    summary['responses_sha256'] = digest(output)
    (args.output / 'historical-attribution.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: {'responses': v['request_count'], 'unlinked': len(v['unlinked_tools']),
                          'classes': {c: g['responses'] for c, g in v['by_class'].items()}}
                      for k, v in summary['arms'].items()}))


if __name__ == '__main__':
    main()
