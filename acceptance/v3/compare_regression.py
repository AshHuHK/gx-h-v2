"""Compare completed v2.1 regression runner output with the frozen case identities and receipts."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


FROZEN = Path(__file__).resolve().parents[1] / 'v21/final-a05dcef'
TEST_ROW = re.compile(r'^test_\w+ \(([^()]+)\) \.\.\. (.*)$', re.M)
NODE_ROW = re.compile(r'^ok \d+ - ([^\n]+)$', re.M)


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def compare(evidence):
    baseline = read(FROZEN / 'aggregate.json')
    candidate = read(evidence / 'summary.json')
    errors = []
    operations = candidate['operations']
    baseline_ops = set(read(FROZEN / 'summary.json')['operations']) | set(baseline['official_focused'])
    for name in sorted(baseline_ops):
        if operations.get(name, {}).get('returncode') != 0:
            errors.append(f'Operation missing or failed: {name}')
    if candidate.get('passed') is not True:
        errors.append('Candidate runner did not report a completed pass')
    expected_binary = read(FROZEN / 'summary.json')['native_binary']
    if candidate.get('native_binary') != expected_binary:
        errors.append('Codex version or digest differs from frozen release')

    groups = {}
    for name, counts_key in [('python-full', 'python_counts'), ('p0-focused', 'p0_counts')]:
        filename = name + '.log'
        before = dict(TEST_ROW.findall((FROZEN / filename).read_text()))
        after = dict(TEST_ROW.findall((evidence / filename).read_text()))
        removed = sorted(before.keys() - after.keys())
        widened_skips = sorted(k for k, s in after.items() if s.startswith('skipped ') and s != before.get(k))
        counts = candidate[counts_key]
        if removed or widened_skips or counts['total'] != len(after) or counts['failures'] or counts['errors']:
            errors.append(f'{name} lost tests, added skips, or reported a count/failure discrepancy')
        groups[name] = {'frozen_test_ids': len(before), 'measured_test_ids': len(after),
                        'removed': removed, 'added': sorted(after.keys() - before.keys()),
                        'changed_or_new_skips': widened_skips, 'counts': counts}

    frozen_node = set(NODE_ROW.findall((FROZEN / 'node.log').read_text()))
    node = set(NODE_ROW.findall((evidence / 'node.log').read_text()))
    nc = candidate['node_counts']
    if not frozen_node <= node or nc['tests'] != nc['pass'] or len(node) != nc['pass']:
        errors.append('Missing or unsuccessful Node tests')

    receipts = {}
    for name in baseline['official_existing']:
        path = evidence / 'native' / 'official-suite' / (name + '.json')
        data = read(path)
        if (candidate.get('official_cases', {}).get(name) != 'PASS' or data.get('status') != 'PASS'
                or data.get('test') != name or data.get('binary_sha256') != expected_binary['sha256']):
            errors.append(f'Official case receipt invalid: {name}')
        receipts['official/' + name] = sha(path)
    for name, case in baseline['official_focused'].items():
        path = evidence / 'native' / (name + '.json')
        frozen_path = FROZEN / case['evidence']
        frozen = read(frozen_path)
        current = read(path)
        checks = current.get('checks', {})
        if (sha(frozen_path) != case['sha256'] or current.get('sha256') != expected_binary['sha256']
                or any(checks.get(k) is not True for k in frozen.get('checks', {}))
                or any(value is not True for value in checks.values())
                or ('result' in frozen and current.get('result') != frozen['result'])):
            errors.append(f'Focused case failed or omitted a frozen assertion: {name}')
        receipts[name] = sha(path)
    return {'scope': 'Frozen v2.1 software and scripted-loopback regression',
            'source_revision': candidate['commit'], 'candidate_runner_summary_sha256': sha(evidence / 'summary.json'),
            'frozen_aggregate_sha256': sha(FROZEN / 'aggregate.json'), 'python': groups, 'node': sorted(node),
            'official_frozen_cases': len(baseline['official_existing']),
            'focused_frozen_cases': len(baseline['official_focused']), 'receipt_sha256': receipts,
            'errors': errors, 'passed': not errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    args = parser.parse_args()
    try:
        result = compare(args.evidence)
    except (OSError, KeyError, TypeError, ValueError) as error:
        result = {'passed': False, 'error': str(error)}
    print(json.dumps(result, indent=2, sort_keys=True))
    sys.exit(0 if result['passed'] else 1)
