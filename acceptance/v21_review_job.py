"""Exercise reviewer-owned CPU Jobs through the pinned official App Server and a loopback-only scripted peer."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from acc1_research import PACKAGE, run_cli
from acc1_research_peer import peer_action
from e2e_grid import fixture_env
from e2e_responses import local_responses


def main():
    binary = PACKAGE / 'vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex'
    commands = []
    with tempfile.TemporaryDirectory() as tmp, patch('e2e_responses.fixture_action', peer_action):
        root = Path(tmp)
        state = root / 'state'
        with local_responses(root / 'trace.jsonl') as server:
            env = fixture_env(root, server, str(binary))
            def cli(command, *extra):
                try:
                    return run_cli([command, '--state', state, *extra], env, commands, timeout=210)
                except RuntimeError:
                    print(json.dumps(commands[-1], ensure_ascii=False), flush=True)
                    raise
            first = cli('start', '--tl1a-geometry')
            final = cli('run', '--timeout', '150')
            assert final['status'] == 'COMPLETED', (final['status'], final.get('node'), final.get('tasks'))
            reopened = cli('run', '--timeout', '10')
            assert final['jobs'] == reopened['jobs'] and final['report_ref'] == reopened['report_ref']
        with closing(sqlite3.connect((state/'gx.db').as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rid = first['run_id']
            flow = db.execute('SELECT workflow_id,workflow_version,workflow_sha256,status FROM runs WHERE run_id=?',(rid,)).fetchone()
            assert flow['workflow_version'] == '3'
            jobs = [dict(j) for j in db.execute('SELECT job_id,capability_id,status,scheduler_ref FROM jobs WHERE run_id=? ORDER BY rowid',(rid,))]
            assert len(jobs) == 6 and all(j['status']=='SUCCEEDED' for j in jobs)
            from gx_core.artifacts import ArtifactService
            from gx_core.p0 import manifest
            from gx_core.domain import ids
            assets = ArtifactService(db)
            evidence = {'manifests': {}, 'artifacts': {}}
            def retain(ref):
                desc = assets.describe(ref)
                artifact = {k: desc[k] for k in ('kind', 'origin', 'job_id', 'task_id', 'sha256', 'bytes')}
                if desc['media_type'] == 'chemical/x-mmcif':
                    file = 'gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif'
                    assert hashlib.sha256((PACKAGE / file).read_bytes()).hexdigest() == desc['sha256']
                    artifact['package_file'] = file
                else:
                    artifact['utf8'] = assets.read_bytes(ref, expected_sha256=desc['sha256']).decode()
                evidence['artifacts'][ref] = artifact
            reviews = []
            for m in db.execute("SELECT m.*,j.lease_ref FROM p0_execution_manifests m JOIN jobs j USING(job_id) WHERE j.capability_id='tl1a-review' ORDER BY j.rowid"):
                admit, done = json.loads(m['admission_json']), json.loads(m['completion_json'])
                output, = done['outputs']
                proof = manifest.read_manifest(db, m['job_id'])
                assert ids.sha256_of_obj(proof['completion']) == m['completion_sha256']
                evidence['manifests'][m['job_id']] = proof
                retain(output['artifact_ref'])
                retain(admit['input_manifest']['artifact_ref'])
                for item in admit['inputs']:
                    retain(item['artifact_ref'])
                owner = admit['owner']
                assert owner['lease_id'] == m['lease_ref'] and output['runner_sha256'] == output['sha256']
                receipts = [json.loads(r['result_json']) for r in db.execute('SELECT result_json FROM p0_root_calls WHERE thread_id=?',(owner['thread_id'],))]
                fit = {r['protocol']:r for r in receipts if r.get('check_ref') and r.get('review_job_id')==m['job_id']}
                live = db.execute('SELECT role,attempt,model FROM p0_roots WHERE root_key=?',(owner['attempt_id'],)).fetchone()
                assert set(r['job_output_ref'] for r in fit.values()) == {output['artifact_ref']}
                assert all(v['status']=='matches' and v['reviewer_thread_id']==owner['thread_id'] for v in fit.values())
                assert any(r.get('waiting') and r.get('job_id') == m['job_id'] for r in receipts)
                for check in fit.values():
                    for key in ('observations_ref', 'measurement_ref', 'evidence_ref', 'check_ref'):
                        retain(check[key])
                reviews.append({'job_id': m['job_id'], 'owner': owner, 'assigned_root': dict(live),
                                'protocols': sorted(fit), 'manifest_hash': m['completion_sha256'], 'raw_output': output,
                                'check_refs': {k:r['check_ref'] for k,r in fit.items()},
                                'request_turn': next(iter(fit.values()))['request_turn_id'],
                                'collection_turns': sorted({r['reviewer_turn_id'] for r in fit.values()})})
            assert [len(r['protocols']) for r in reviews]==[2,3]
            assert all(r['request_turn'] not in r['collection_turns'] for r in reviews)
            retain(final['report_ref'])
            for r in final['rounds']:
                for key in ('proposal_ref','evidence_ref','assessment_ref','artifact_ref'):
                    retain(r[key])
            target = PACKAGE/'acceptance/v21/review-job'
            (target/'public-products.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n')
            budget = db.execute('SELECT limits_json,spent_json FROM budgets WHERE run_id=? AND parent_ref IS NULL', (rid,)).fetchone()
            job_limit = next(row for row in final['resource_limits'] if row['resource'] == 'job_submissions')
            assert job_limit['limit'] == 6 and json.loads(budget['spent_json'])['job_submissions'] == 6
            project={'budget': {'root_job_limit': json.loads(budget['limits_json'])['job_submissions'],
                      'formal_job_limit': job_limit['limit'], 'jobs_charged': job_limit['spent']}, 'mode':'official Codex 0.169.0 with local scripted Responses, real detached CPU Jobs',
                     'external_model_calls':0,'workflow':dict(flow),'run':rid,'jobs':jobs,'reviews':reviews,
                     'report_ref':final['report_ref'],'rounds':len(final['rounds']),'cold_terminal_reopen_same_jobs_and_report':True}
            p=PACKAGE/'acceptance/v21/review-job/public-cli.json'
            p.write_text(json.dumps(project,indent=2,ensure_ascii=False)+'\n')
            print(p, flush=True)


if __name__ == '__main__':
    main()
