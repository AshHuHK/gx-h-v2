"""Recompute a finished GX study's named CPU outputs from separately pinned original mmCIFs."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

if __package__:
    from .independent_geometry import reference
    from .independent_compare import compare
else:
    from independent_geometry import reference
    from independent_compare import compare

ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = Path(__file__).with_name('operator') / 'reference_protocol.json'


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def read_artifact(conn, state, source, run_id, job_id=None):
    meta = conn.execute('SELECT * FROM artifacts WHERE artifact_ref=? AND run_id=?',
                        (source['artifact_ref'], run_id)).fetchone()
    checked(meta is not None, 'Named artifact is missing from the Run')
    if job_id:
        checked(meta['job_id'] == job_id and meta['origin'] == 'JOB_OUTPUT', 'Wrong output owner')
    path = state / 'artifacts' / meta['namespace'] / meta['rel_path']
    raw = path.read_bytes()
    checked(meta['sha256'] == source['sha256'] == sha256(raw).hexdigest(), 'Artifact bytes differ from receipt')
    return raw


def audit(state, run_id, protocol, directory, expected_refs):
    started = datetime.now(timezone.utc).isoformat()
    registered = json.loads(protocol.read_bytes())
    pins = {item['sha256']: item for item in registered['inputs']}
    result = []
    with closing(sqlite3.connect((state / 'gx.db').resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT j.job_id,j.status,m.admission_json,m.completion_json FROM jobs j '
                            'JOIN p0_execution_manifests m ON m.job_id=j.job_id WHERE j.run_id=? '
                            'AND j.capability_id=? ORDER BY j.rowid', (run_id, 'deposited-block-influence')).fetchall()
        checked(rows, 'No admitted block-influence Jobs found for this Run')
        # Only registered inputs and admitted parameters select the reference computation.
        # Open the product bytes after completing the independent reference for every Job.
        pending = []
        for job in rows:
            admission = json.loads(job['admission_json'])
            checked(admission['run_id'] == run_id and admission['capability']['version'] == '1', 'Wrong method binding')
            inputs = [x for x in admission['inputs'] if x['role'] == 'structure']
            checked(len(inputs) == 1 and inputs[0]['sha256'] in pins, 'Job input does not match a registered original')
            source = inputs[0]
            pin = pins[source['sha256']]
            raw = (ROOT / pin['repository_path']).read_bytes()
            checked(read_artifact(conn, state, source, run_id) == raw, 'Registered source and GX input bytes differ')
            mask = admission['parameters']['mask']
            independent = reference(raw, pin['entry'], pin['sha256'], mask)
            pending.append((job, source, independent))
        for job, source, independent in pending:
            completion = json.loads(job['completion_json'])
            jid = job['job_id']
            checked(job['status'] == completion['status'] == 'SUCCEEDED' and completion['job_id'] == jid,
                    'Original Job did not finish')
            outputs = [x for x in completion['outputs'] if x['output_name'] == 'block-influence.json']
            checked(len(outputs) == 1, 'Expected exactly one named scientific output for this Job')
            evidence = outputs[0]
            checked(evidence['runner_sha256'] == evidence['sha256'] and source in evidence['source_inputs'],
                    'Named output did not retain original runner digest or source binding')
            product = json.loads(read_artifact(conn, state, evidence, run_id, jid))
            verified = compare(independent, product)
            stem = independent['entry'] + '-' + independent['mask'] + '-' + jid
            verified.update(job_id=jid, input_ref=source['artifact_ref'], artifact_ref=evidence['artifact_ref'],
                            artifact_sha256=evidence['sha256'], output_name=evidence['output_name'],
                            job_finished_at=completion['finished_at'],
                            verified_at_utc=datetime.now(timezone.utc).isoformat())
            (directory / (stem + '-reference.json')).write_text(json.dumps(independent, allow_nan=False) + '\n')
            (directory / (stem + '-comparison.json')).write_text(json.dumps(verified, indent=2, allow_nan=False) + '\n')
            result.append({'entry': independent['entry'], 'mask': independent['mask'], 'status': verified['status'],
                           'input_sha256': source['sha256'], 'job_id': jid, 'artifact_ref': evidence['artifact_ref'], 'product_sha256': evidence['sha256'],
                           'comparison': stem + '-comparison.json', 'reference': stem + '-reference.json',
                           'endpoint_scope': verified['endpoint_scope'], 'max_abs_errors': verified['max_abs_errors']})
    if expected_refs:
        checked({v['artifact_ref'] for v in result} == set(expected_refs), 'Observed output refs differ from receipt')
    return {'status': 'PASS' if all(v['status'] == 'PASS' for v in result) else 'FAIL', 'run_id': run_id,
            'study': registered['series'], 'protocol_sha256': sha256(protocol.read_bytes()).hexdigest(),
            'review': 'Independent numerical recomputation from the same deposited originals.',
            'reviewer_class': 'SCRIPTED_NUMERICAL_REFERENCE', 'started_at_utc': started,
            'finished_at_utc': datetime.now(timezone.utc).isoformat(),
            'source_files': {name: sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ('independent_geometry.py', 'independent_compare.py', 'independent_study.py')},
            'covered': result, 'registered_inputs': [{'entry': v['entry'], 'sha256': k} for k, v in pins.items()]}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--state', type=Path, required=True)
    cli.add_argument('--run-id', required=True)
    cli.add_argument('--protocol', type=Path, default=PROTOCOL)
    cli.add_argument('--output-ref', action='append', default=[], help='Exact set from the study receipt (repeatable)')
    cli.add_argument('--out', type=Path, required=True, help='Fresh custodian directory outside the Lead workspace')
    args = cli.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    result = audit(args.state.resolve(), args.run_id, args.protocol, args.out, args.output_ref)
    (args.out / 'result.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(result['status'], len(result['covered']), 'independently checked named Job outputs')
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
