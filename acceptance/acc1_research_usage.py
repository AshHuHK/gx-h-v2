"""Count returned tool bytes and official provider token receipts for one saved study.

Only GX-bound threads with an actual turn/start are inspected. No message bodies,
coordinates, tokens used for authentication, or rollout files are copied.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from acc1_research import save
from e2e_audit import require


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def covered(intervals):
    result, high = 0, -1
    for start, end in sorted(intervals):
        result += max(0, end - max(start, high))
        high = max(end, high)
    return result


def metering(paths, tid, turns):
    require(len(paths) == 1, 'Expected one official rollout matching the specific GX-paid Root')
    bucket, seen, settings = Counter(), set(), {}
    with paths[0].open('rb') as stream:
        first = json.loads(next(stream))
        require(first['type'] == 'session_meta' and first['payload']['id'] == tid, 'Rollout identity mismatch')
        for line in stream:
            row = json.loads(line)
            if row['type'] == 'turn_context' and row['payload'].get('turn_id') in turns:
                ctx = row['payload']
                permissions = ctx.get('permission_profile') or {}
                settings[ctx['turn_id']] = {'approval_policy': ctx.get('approval_policy'),
                    'sandbox': (ctx.get('sandbox_policy') or {}).get('type'), 'model': ctx.get('model'),
                    'reasoning_effort': ctx.get('effort'), 'network': permissions.get('network'),
                    'file_system_type': (permissions.get('file_system') or {}).get('type')}
            if row['type'] != 'token_usage_record':
                continue
            p = row['payload']
            if p['thread_id'] != tid or p['turn_id'] not in turns:
                continue
            require(p['response_id'] not in seen, 'A provider response has duplicate usage')
            seen.add(p['response_id'])
            usage = p['usage']
            for k in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens'):
                bucket[k] += int(usage.get(k) or 0)
    return {'provider_responses': len(seen), **bucket}, settings


def analyze(state, include_official):
    with closing(sqlite3.connect((state / 'gx.db').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        artifacts = list(db.execute('SELECT artifact_ref,kind,rel_path,namespace FROM artifacts'))
        refs = {a['artifact_ref']: a['kind'] for a in artifacts}
        direct = {digest(['gx_read_artifact', {'ref': ref}]): ('gx_read_artifact', kind)
                  for ref, kind in refs.items()}
        direct.update({digest([name, {}]): (name, name) for name in ('gx_task_context', 'gx_project_state')})
        for protocol in ('common_ca', 'common_backbone', 'ordered_backbone'):
            direct[digest(['gx_recalculate_geometry', {'protocol': protocol}])] = ('gx_recalculate_geometry', protocol)
        for a in artifacts:
            if a['kind'] in ('geometry_assessment', 'geometry_decision', 'geometry_plan'):
                body = json.loads((state / 'artifacts' / a['namespace'] / a['rel_path']).read_text())
                direct[digest(['gx_submit_product', {'product': body}])] = ('gx_submit_product', a['kind'])
        roles, all_ranges = [], []
        source_total = 0
        first, last = db.execute("SELECT min(created_at), max(created_at) FROM artifacts").fetchone()
        first, last = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
        for root in db.execute('SELECT role,thread_id,attempt,model FROM p0_roots ORDER BY rowid'):
            tid = root['thread_id']
            receipts = list(db.execute("SELECT d.turn_id,u.total_tokens,u.terminal_status,b.status FROM p0_root_deliveries d "
                "LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) "
                "LEFT JOIN budget_reservations b ON b.reservation_ref=d.reservation_ref WHERE d.thread_id=? "
                "AND d.method='turn/start' AND d.turn_id IS NOT NULL", (tid,)))
            if not receipts:
                continue
            root_ranges, groups = [], defaultdict(lambda: {'count': 0, 'result_utf8_bytes': 0})
            for r in db.execute('SELECT request_sha,result_json FROM p0_root_calls WHERE thread_id=?', (tid,)):
                value = json.loads(r['result_json'])
                meta = value.get('metadata', {}) if isinstance(value, dict) else {}
                kind = direct.get(r['request_sha'], ('unidentified_callback', ''))[0]
                subkind = direct.get(r['request_sha'], ('', ''))[1]
                if meta.get('media_type') == 'chemical/x-mmcif':
                    ref, start, size = meta['artifact_ref'], meta['offset'], meta['returned_bytes']
                    require(r['request_sha'] in {digest(['gx_read_artifact', args]) for args in
                            ({'ref': ref}, {'ref': ref, 'offset': start})}, 'Unidentified coordinate delivery')
                    require(len(value['content'].encode()) == size, 'Coordinate page size mismatch')
                    root_ranges.append((start, start + size))
                    kind, subkind = 'gx_read_artifact', 'raw_mmcif'
                entry = groups[f'{kind}/{subkind}']
                entry['count'] += 1
                entry['result_utf8_bytes'] += len(r['result_json'].encode())
            total = sum(end - start for start, end in root_ranges)
            source_total += total
            all_ranges.extend(root_ranges)
            row = {**dict(root), 'turn_ids': [r['turn_id'] for r in receipts],
                   'gx_reported_total_tokens': max((r['total_tokens'] or 0) for r in receipts),
                   'charges_settled': all(r['status'] == 'SETTLED' for r in receipts),
                   'turn_status': [r['terminal_status'] for r in receipts],
                   'mmcif_page_calls': len(root_ranges), 'mmcif_content_bytes_returned': total,
                   'mmcif_distinct_bytes_in_root': covered(root_ranges), 'tool_responses': dict(groups)}
            if include_official:
                home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
                paths = []
                for i in range((last - first).days + 1):
                    folder = home / 'sessions' / (first + timedelta(days=i)).strftime('%Y/%m/%d')
                    paths.extend(folder.glob('rollout-*-' + tid + '.jsonl'))
                row['official_usage'], row['effective_turn_permissions'] = metering(paths, tid, row['turn_ids'])
                require(row['official_usage']['total_tokens'] == row['gx_reported_total_tokens'],
                        'Official response receipts and GX thread cumulative usage differ')
            roles.append(row)
    return {'method': 'Exact GX callback request digests, canonical UTF-8 result sizes and union of raw CIF byte intervals. '
                      'Token counts come from official provider responses by thread. They include previous context and repeat '
                      'input tokens. The counters do not assign a particular token cost to any individual artifact.',
            'roots': roles, 'mmcif_bytes_returned': source_total, 'distinct_mmcif_bytes': covered(all_ranges),
            'repeated_mmcif_bytes': source_total - covered(all_ranges)}


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--state', type=Path, required=True)
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--official-usage', action='store_true', help='Read numeric receipt fields for the bound local Codex sessions')
    args = cli.parse_args()
    save(args.output, analyze(args.state.resolve(), args.official_usage))
