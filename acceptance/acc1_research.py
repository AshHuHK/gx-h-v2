"""Run the released CLI on the shipped experimental structure and check its saved receipts.

The state directory is private. The evidence directory contains the scientific
products, their hashes and selected GX receipts. Codex home and HTTP traces
are never copied. Run from the release root with Python 3.
"""
import argparse
from functools import partial
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
import subprocess
import sys
import time
import tempfile
import tomllib
from urllib.parse import urlsplit

from e2e_audit import require

PACKAGE = Path(__file__).resolve().parents[1]
SPEC = PACKAGE / 'examples/tl1a_geometry/project.json'
sys.path.insert(0, str(PACKAGE / 'gx-h'))


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def code_version():
    commit = None
    if (PACKAGE / '.git').exists() and shutil.which('git'):
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PACKAGE, text=True).strip()
    root = PACKAGE / 'gx-h'
    paths = sorted(p for directory in ('gx_core', 'workflows', 'adapters/scientific/tl1a_geometry',
                   'config', 'contracts') for p in (root / directory).rglob('*')
                   if p.suffix in {'.py', '.sql', '.json', '.toml', '.cif'} and p.is_file())
    digest = hashlib.sha256()
    for path in paths:
        digest.update(f'{path.relative_to(root)} {sha(path.read_bytes())}\n'.encode())
    return {'git_commit': commit, 'scientific_and_runtime_tree_sha256': digest.hexdigest(),
            'files_hashed': len(paths)}


def run_cli(args, env, history, *, timeout):
    started = time.monotonic()
    result = subprocess.run([str(PACKAGE / 'bin/gx-p0'), *map(str, args)], cwd=PACKAGE, env=env,
                            text=True, capture_output=True, timeout=timeout)
    record = {'command': [str(a) for a in args], 'seconds': round(time.monotonic() - started, 3),
              'exit': result.returncode}
    history.append(record)
    if result.returncode:
        # Provider failures can carry HTTP details. Retain only the exception class.
        classes = re.findall(r'^([A-Za-z][A-Za-z0-9_.]*):', result.stderr, flags=re.M)
        record['error_class'] = classes[-1] if classes else 'command_failed'
        if env.get('GX_P0_TEST_PROVIDER') == 'gx_loopback':
            record['diagnostic'] = result.stderr.strip()[-1800:]
    try:
        doc = json.loads(result.stdout)
    except ValueError:
        doc = {}
    for key in ('driver', 'status', 'node', 'round', 'report_ref', 'login', 'chatgpt_oauth', 'run_id'):
        if key in doc:
            record[key] = doc[key]
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode} ({record["error_class"]})')
    return doc


def capture(state):
    db = state / 'gx.db'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        def rows(sql):
            return [dict(row) for row in conn.execute(sql)]
        data = {
            'runs': rows('SELECT run_id,project_ref,workflow_id,workflow_version,workflow_sha256,objective,status FROM runs'),
            'jobs': rows('SELECT job_id,capability_id,capability_version,scheduler_ref,idempotency_key,'
                         'input_manifest_ref,status,exit_code,measured_json,submitted_at,finished_at '
                         'FROM jobs ORDER BY rowid'),
            'tasks': rows('SELECT task_id,stage,role,status,attempt FROM tasks ORDER BY rowid'),
            'submissions': rows('SELECT s.task_id,s.submission_id,s.result,s.attempt,s.artifact_refs_json,'
                                's.content_sha256,l.principal,t.stage FROM submissions s JOIN leases l USING(lease_id) '
                                'JOIN tasks t ON t.task_id=s.task_id ORDER BY s.rowid'),
            'roots': rows('SELECT task_id,role,attempt,thread_id,model,effort FROM p0_roots ORDER BY rowid'),
            'deliveries': rows('SELECT event_id,thread_id,turn_id,method,state FROM p0_root_deliveries ORDER BY rowid'),
            'usage': rows('SELECT thread_id,turn_id,total_tokens,terminal_status FROM p0_root_turn_usage ORDER BY rowid'),
            'budget': rows('SELECT limits_json,spent_json,reserved_json FROM budgets ORDER BY rowid'),
            'native_reservations': rows("SELECT subject_id,amounts_json,status FROM budget_reservations "
                                        "WHERE subject_kind='native_turn' ORDER BY rowid"),
            'decisions': rows('SELECT round_index,submission_id,reviewer_submission_id,decision_ref,actions_json,'
                              'reason,stop_reason FROM science_decisions ORDER BY round_index'),
            'settlements': rows('SELECT round_index,path_id,job_id,submission_id,kind,reason,eval_id,evidence_id,'
                                'direction FROM science_settlements ORDER BY rowid'),
            'artifacts': rows('SELECT artifact_ref,kind,run_id,job_id,task_id,sha256,bytes FROM artifacts ORDER BY rowid'),
            'manifests': []
        }
        for item in conn.execute('SELECT * FROM p0_execution_manifests ORDER BY rowid'):
            admission = json.loads(item['admission_json'])
            require(sha(item['admission_json'].encode()) == item['admission_sha256'], 'Invalid admission digest')
            entry = {k: item[k] for k in ('job_id', 'admission_sha256', 'completion_sha256')}
            entry['admission'] = {k: admission.get(k) for k in (
                'inputs', 'input_manifest', 'capability', 'parameters', 'owner', 'evaluator', 'runtime',
                'source', 'intent_dependencies', 'limits')}
            if item['completion_json']:
                completion = json.loads(item['completion_json'])
                require(sha(item['completion_json'].encode()) == item['completion_sha256']
                        and completion['admission_sha256'] == item['admission_sha256'], 'Invalid completion digest')
                entry['completion'] = {k: completion.get(k) for k in ('status', 'outputs', 'measured', 'backend_job_id')}
            data['manifests'].append(entry)
        if {'provider', 'effective_profile_json'} <= {r[1] for r in conn.execute('PRAGMA table_info(p0_roots)')}:
            data['effective_roots'] = rows("SELECT task_id,role,attempt,thread_id,provider,model,effort,"
                                           "effective_profile_json FROM p0_roots ORDER BY rowid")
        protocols = ('common_backbone', 'common_ca', 'ordered_backbone')
        from gx_core.domain.ids import sha256_of_obj
        check_calls = {sha256_of_obj(['gx_recalculate_geometry', {'protocol': protocol}]): protocol
                       for protocol in protocols}
        data['reviewer_recalculations'] = []
        for call in conn.execute('SELECT thread_id,turn_id,call_id,request_sha,result_json FROM p0_root_calls'):
            if call['request_sha'] in check_calls:
                data['reviewer_recalculations'].append({
                    'thread_id': call['thread_id'], 'turn_id': call['turn_id'], 'call_id': call['call_id'],
                    'protocol': check_calls[call['request_sha']], 'result': json.loads(call['result_json'])})
        return data


def verify_provenance(data, first, report_ref):
    run, = [row for row in data['runs'] if row['workflow_id'] == 'tl1a_geometry']
    quality, = [r for r in data['runs'] if r['workflow_id'] == 'tl1a_coordinate_quality']
    require(quality['status'] == 'COMPLETED' and quality['project_ref'] == run['project_ref'],
            'Missing the completed coordinate-quality workflow under this project')
    require(run['workflow_id'] == 'tl1a_geometry' and run['status'] == 'COMPLETED',
            'The completed run does not identify the domain workflow')
    jobs = {job['job_id']: job for job in data['jobs']}
    require(bool(jobs) and all(j['status'] == 'SUCCEEDED' and j['scheduler_ref'] for j in jobs.values()),
            'An executed CPU Job has no terminal success receipt')
    require(any(j['status'] in ('SUBMITTED', 'QUEUED', 'RUNNING', 'SUBMITTING') for j in first['jobs']),
            'No active GX Job was awaiting its receipt at operator stop')
    require(all((job['job_id'] in jobs and jobs[job['job_id']]['scheduler_ref'] == job['scheduler_ref'])
                for job in first['jobs']), 'A resumed Job changed its backend identity')
    require(len({job['idempotency_key'] for job in jobs.values()}) == len(jobs), 'Duplicate execution operation')
    hashes = {a['artifact_ref']: a['sha256'] for a in data['artifacts']}
    require(report_ref in hashes, 'Missing final report artifact')
    masks = {a['artifact_ref'] for a in data['artifacts']
             if a['kind'] == 'geometry_masks' and a['run_id'] == quality['run_id']}
    compared = [m for m in data['manifests'] if jobs[m['job_id']]['capability_id'] == 'tl1a-geometry']
    require(len(compared) >= 2 and masks and all(
        any(i['artifact_ref'] in masks and i['sha256'] == hashes[i['artifact_ref']]
            for i in m['admission']['inputs']) for m in compared),
        'Comparison did not consume the immutable mask produced by the coordinate-quality workflow')
    roots = {(r['task_id'], r['attempt']): r for r in data['roots']}
    submissions = {s['submission_id']: s for s in data['submissions'] if s['result'] == 'ACCEPTED'}
    for s in submissions.values():
        require(roots[s['task_id'], s['attempt']]['thread_id'] == s['principal'],
                'Submission did not originate in its assigned native Root')
        require(any(hashes.get(ref) == s['content_sha256'] for ref in json.loads(s['artifact_refs_json'])),
                'Submission hash does not bind its scientific product')
    require(len({s['principal'] for s in submissions.values()}) >= 3,
            'Proposal, independent review and decision require distinct scientific worker receipts')
    require({s['principal'] for s in submissions.values()} <=
            {r['thread_id'] for r in data['deliveries'] if r['turn_id'] and r['state'] == 'acknowledged'},
            'Missing official delivery for one of the submitting native workers')
    reviewer_threads = {s['principal'] for s in submissions.values()
                        if roots[s['task_id'], s['attempt']]['role'] == 'evidence_reviewer'}
    require(any(r['thread_id'] in reviewer_threads for r in data['reviewer_recalculations']),
            'No accepted independent reviewer read the result of a source-coordinate recomputation')
    require(len(jobs) == len(data['manifests']) and
            all(m['job_id'] in jobs and m['admission_sha256'] and m['completion_sha256'] for m in data['manifests']),
            'Missing prospective or terminal execution manifest')


def export_science(state, out, receipts):
    from adapters.scientific.tl1a_geometry.review import independent_check, compare_protocols
    products = {}
    source = None
    checks = []
    with closing(sqlite3.connect((state / 'gx.db').as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for a in conn.execute('SELECT artifact_ref,kind,job_id,task_id,namespace,rel_path,sha256,bytes FROM artifacts'):
            raw = (state / 'artifacts' / a['namespace'] / a['rel_path']).read_bytes()
            require(sha(raw) == a['sha256'] and len(raw) == a['bytes'], f'artifact mismatch {a["artifact_ref"]}')
            if raw.lstrip().startswith(b'data_2RE9'):
                source = raw
                continue
            if not raw.lstrip().startswith(b'{'):
                continue
            doc = json.loads(raw)
            # Only explicitly scientific products leave private project state.
            if a['kind'] in SCIENCE_KINDS or doc.get('schema') in SCIENCE_SCHEMAS:
                products[a['artifact_ref']] = {'kind': a['kind'], 'job_id': a['job_id'], 'body': doc}
    require(source is not None, 'No experimental 2RE9 source in the project artifacts')
    criteria = next(v['body'] for v in products.values() if v['kind'] == CRITERIA_KIND)
    for ref, entry in products.items():
        if entry['job_id'] and entry['body'].get('schema') == 'gx.tl1a.geometry/1':
            result = independent_check(source, entry['body'], criteria)
            require(result['status'] == 'matches', f'Independent source-byte comparison failed for {ref}')
            checks.append({'observation_ref': ref, 'reference': result})
    require(bool(checks), 'No geometric observation was independently reproduced')
    comparisons = []
    for ref, entry in products.items():
        if entry['kind'] == 'geometry_evidence':
            evidence = entry['body']
            observations = [products[route['observations_ref']]['body'] for route in evidence['routes']]
            comparison = compare_protocols(observations, products[evidence['criteria_ref']]['body'])
            require(comparison['status'] == evidence['comparison']['status'],
                    'Saved evidence disagrees with independent cross-protocol comparison')
            comparisons.append({'evidence_ref': ref, **comparison})
    require(comparisons and receipts['decisions'], 'No assessed and decided comparison')
    report, = [p['body'] for p in products.values() if p['kind'] == 'geometry_report']
    previous = None
    for entry in report['rounds']:
        plan = products[entry['proposal_ref']]['body']
        assessment = products[entry['assessment_ref']]['body']
        evidence = products[entry['evidence_ref']]['body']
        decision = products[entry['artifact_ref']]['body']
        require(plan['criteria_ref'] == evidence['criteria_ref'] and assessment['evidence_ref'] == entry['evidence_ref']
                and decision['assessment_ref'] == entry['assessment_ref'] and decision['evidence_ref'] == entry['evidence_ref']
                and set(plan['protocols']) == {r['protocol'] for r in evidence['routes']},
                'Broken proposal/evidence/assessment/decision provenance')
        if previous:
            old = products[previous['artifact_ref']]['body']
            prior_plan = products[previous['proposal_ref']]['body']
            require(plan['previous_decision_ref'] == previous['artifact_ref']
                    and set(plan['protocols']) - set(prior_plan['protocols']) == set(old['next_protocols'])
                    and old['next_protocols'] and plan['evaluator'] == prior_plan['evaluator'],
                    'The follow-up measurements differ from the accepted decision or the frozen evaluation')
        previous = entry
    require(previous and products[previous['artifact_ref']]['body']['stop_reason'], 'Missing scientific stopping reason')

    save(out / 'products.json', products)
    save(out / 'recomputed.json', {'source_sha256': sha(source), 'results': checks, 'comparisons': comparisons})
    save(out / 'receipts.json', receipts)
    return len(checks)


# Names of the shipped domain artifacts. These also bound the public evidence export.
SCIENCE_KINDS = {
    'geometry_plan', 'geometry_proposal', 'geometry_criteria', 'geometry_checks', 'geometry_evidence',
    'geometry_assessment', 'geometry_decision', 'geometry_report',
}
SCIENCE_SCHEMAS = {'gx.tl1a.coordinate_quality/1', 'gx.tl1a.geometry/1', 'gx.tl1a.criteria/1'}
CRITERIA_KIND = 'geometry_criteria'


def execute(args):
    from gx_core.p0.profiles import clash_env

    out = args.output.resolve()
    state = args.state.resolve()
    out.mkdir(parents=True, exist_ok=args.resume)
    if not args.resume and (state / 'gx.db').exists():
        raise ValueError('Select a new state or use --resume on the original run')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(clash_env(), GX_P0_TEST='1', GX_P0_TEST_PROVIDER='openai', PYTHONDONTWRITEBYTECODE='1')
    result = {'started': utc(), 'mode': 'official Codex ChatGPT OAuth, real model and real local CPU Jobs',
              'commands': [], 'code_at_start': code_version(), 'status': 'INCOMPLETE'}
    if args.resume and (out / 'summary.json').is_file():
        result['prior_attempts'] = json.loads((out / 'summary.json').read_text())
    args_timeout = args.timeout
    def cli(command, *extra):
        return run_cli([command, '--state', state, *extra], env, result['commands'], timeout=args_timeout + 90)

    stack = ExitStack()
    try:
        if args.loopback:
            from unittest.mock import patch
            from acc1_research_peer import peer_action
            from e2e_grid import fixture_env
            from e2e_responses import FixtureServer, local_responses
            home = state / 'codex'
            if args.resume and (home / 'config.toml').is_file():
                with (home / 'config.toml').open('rb') as config:
                    endpoint = tomllib.load(config)['model_providers']['gx_loopback']['base_url']
                address = urlsplit(endpoint)
                require(address.hostname == '127.0.0.1' and address.port, 'Expected a saved local scripted endpoint')
                class ResumeServer(FixtureServer):
                    def server_bind(self):
                        self.server_address = ('127.0.0.1', address.port)
                        super().server_bind()
                stack.enter_context(patch('e2e_responses.FixtureServer', ResumeServer))
            stack.enter_context(patch('e2e_responses.fixture_action',
                                      partial(peer_action, finish_if_stable=args.fixture_stop_if_stable)))
            server = stack.enter_context(local_responses(out / 'requests.jsonl'))
            codex = shutil.which(os.environ.get('GX_P0_CODEX', 'codex'))
            env_dir = (Path(stack.enter_context(tempfile.TemporaryDirectory())) if home.is_dir() else state)
            env = fixture_env(env_dir, server, codex)
            env.update(HOME=str(state), CODEX_HOME=str(home))
            require(subprocess.check_output([codex, '--version'], env=env, text=True).strip()
                    == 'codex-cli 0.169.0', 'Unexpected official CLI version')
            result['mode'] = 'official Codex app-server, scripted loopback Responses, real local CPU Jobs'
            result['scripted_policy'] = ('finish when the measured common-atom comparison is stable'
                                         if args.fixture_stop_if_stable else 'investigate the unmeasured ordered subset when funded')
        else:
            profile = {'default': {'provider': 'openai', 'model': 'gpt-6.2-astra'},
                       'roles': {'evidence_reviewer': {'provider': 'openai', 'model': args.review_model}}}
            saved_profile = out / 'model-profile.json'
            if args.resume and saved_profile.is_file():
                require(json.loads(saved_profile.read_text()) == profile, 'Resume using the original model profile')
            else:
                save(saved_profile, profile)
            env['GX_P0_PROFILE'] = str(saved_profile)
            login = run_cli(['login-status'], env, result['commands'], timeout=40)
            require(login['chatgpt_oauth'] is True, 'Official CLI has no ChatGPT OAuth session')
            doctor = run_cli(['doctor'], env, result['commands'], timeout=80)
            selected = {model: doctor.get('models', {}).get(model, []) for model in ('gpt-6.2-astra', args.review_model)}
            require(doctor.get('ready') and all('xhigh' in levels for levels in selected.values()),
                    'Official provider catalog does not offer a selected model at xhigh')
            result['deployment'] = {k: doctor.get(k) for k in ('codex_version','provider','account_type','http_proxy',
                                                               'socks_proxy','no_proxy')}
            result['deployment']['catalog_efforts'] = selected
        if not args.resume:
            spec = json.loads(args.spec.read_text())
            spec.setdefault('intent_items', []).append({
                'item_id': 'acceptance-native-turns', 'item_type': 'resource_limit',
                'value': {'resource': 'api_calls', 'max': 9, 'unit': 'gx_calls'}})
            save(out / 'spec.json', spec)
            doc = cli('start', '--spec', out / 'spec.json')
            before = capture(state)
            save(out / 'before-stop.json', before)
            require(doc['status'] != 'COMPLETED', 'No opportunity to interrupt the study')
            paused = cli('stop')
            require(paused['status'] == 'PAUSED', 'Public stop did not pause the run')
            save(out / 'paused.json', capture(state))
        doc = cli('resume', '--timeout', str(args_timeout))
        completed = capture(state)
        save(out / 'receipts.json', completed)
        require(doc['status'] == 'COMPLETED', 'The scientific project did not reach a final decision')
        first = json.loads((out / 'before-stop.json').read_text()) if (out / 'before-stop.json').exists() else completed
        verify_provenance(completed, first, doc.get('report_ref'))
        if not args.loopback:
            workers = {s['principal'] for s in completed['submissions'] if s['result'] == 'ACCEPTED'}
            actual = [r for r in completed['effective_roots'] if r['thread_id'] in workers]
            require(all(r['provider'] == 'openai' and r['effort'] == 'xhigh' for r in actual)
                    and any(r['model'] == args.review_model and r['role'] == 'evidence_reviewer' for r in actual)
                    and any(r['model'] == 'gpt-6.2-astra' and r['role'] != 'evidence_reviewer' for r in actual),
                    'Effective profiles of the submitting scientific workers differ from the validated selection')
        result['observations_recomputed'] = export_science(state, out, completed)
        terminal = cli('run', '--timeout', '30')
        reopened = capture(state)
        require(terminal.get('report_ref') == doc.get('report_ref') and reopened == completed,
                'Cold reopen changed a completed project')
        spent = [json.loads(b['spent_json']) for b in completed['budget']]
        lingering = [v for v in completed['native_reservations'] if v['status'] == 'HELD']
        result['native_accounting'] = {
            'status': 'RECONCILIATION_PENDING' if lingering else 'NO_HELD_NATIVE_TURNS',
            'charged_gx_turns': sum(b.get('api_calls', 0) for b in spent),
            'charged_tokens': sum(b.get('tokens', 0) for b in spent),
            'remaining_native_holds': lingering,
            'provider_billing_usd': None,
        }
        result['decisions'] = [{'round': d['round_index'], 'actions': json.loads(d['actions_json']),
                                'reason': d['reason'], 'stop_reason': d['stop_reason']}
                               for d in completed['decisions']]
        result.update(status='PASS', job_count=len(completed['jobs']), task_count=len(completed['tasks']),
                      report_ref=doc.get('report_ref'), replay_new_jobs=0, replay_new_turns=0)
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        if (state / 'gx.db').exists():
            save(out / 'receipts.json', capture(state))
        raise
    finally:
        stack.close()
        result.update(finished=utc(), code_at_end=code_version())
        save(out / 'summary.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--spec', type=Path, default=SPEC)
    parser.add_argument('--loopback', action='store_true', help='Use the scripted local reviewer and genuine GX CPU Jobs')
    parser.add_argument('--fixture-stop-if-stable', action='store_true',
                        help='Local control: stop when the common-atom comparison already answers the narrower question')
    parser.add_argument('--review-model', default='gpt-6.2-sol')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--resume', action='store_true', help='Continue the existing state, preserving its history')
    execute(parser.parse_args())
