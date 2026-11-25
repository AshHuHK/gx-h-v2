"""Capture a read-only, reproducible V6 runtime contract and named dispatch inventory."""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys


RUNTIME_MODULES = (
    'orchestration/engine.py', 'orchestration/service.py', 'orchestration/reactor.py',
    'p0/roots.py', 'p0/driver.py', 'p0/cli.py', 'p0/coordinator.py',
)
FORMAL_NAMES = ('gx_decide_revision', 'gx_stage_claim', 'gx_delegate_branch', 'gx_reevaluate_geometry')
REVIEWED_DOCS = ('V6_ACCEPTANCE.md', 'V6_DEPLOYMENT.md', 'acceptance/v6/engineering/README.md',
                 'acceptance/v6/m0/baseline-audit.json')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(argv, root, *, env=None):
    process = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
    if process.returncode:
        raise RuntimeError(f"{argv!r} exited {process.returncode}\n{process.stdout}\n{process.stderr}")
    return process.stdout.strip()


def dispatch_inventory(core):
    counts = {k: [] for k in ('goal_markers', 'direct_mode_comparisons', 'grid_equality',
                                'other_grid_predicates', 'protein_imports')}
    for path in sorted(core.rglob('*.py')):
        rel = path.relative_to(core).as_posix()
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        def record(kind, lineno):
            record = {'path': 'gx-h/gx_core/' + rel, 'line': lineno, 'source': lines[lineno-1].strip()}
            if record not in counts[kind]:
                counts[kind].append(record)
        if rel in RUNTIME_MODULES:
            for n, line in enumerate(lines, 1):
                if re.search(r'\bis_goal\(|\b_goal_mode\b', line):
                    record('goal_markers', n)
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                values = {x.value for x in ast.walk(node)
                          if isinstance(x, ast.Constant) and isinstance(x.value, str)}
                if rel in RUNTIME_MODULES and 'goal' in values:
                    record('direct_mode_comparisons', node.lineno)
                if 'science_grid' in values:
                    use = 'grid_equality' if any(isinstance(op, ast.Eq) for op in node.ops) else 'other_grid_predicates'
                    record(use, node.lineno)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"workflow_id\s*=\s*'science_grid'", node.value, re.I):
                    record('other_grid_predicates', node.lineno)
            if isinstance(node, ast.ImportFrom) and ('v6_protein' in (node.module or '').split('.') or
                    any(a.name == 'v6_protein' for a in node.names)):
                record('protein_imports', node.lineno)
    return counts


def snapshot(root):
    sys.path.insert(0, str(root/'gx-h'))
    from gx_core import contracts
    core = root/'gx-h/gx_core'
    env = {**os.environ, 'GX_PYTHON': sys.executable, 'PYTHONDONTWRITEBYTECODE': '1'}
    doctor = json.loads(command([str(root/'gx-h/bin/gx'), 'doctor'], root, env=env))
    registry = doctor['registry']
    installed = root/'gx-h/plugins/installed'
    packages = []
    for path in sorted((installed/'org.gx.protein').glob('*/gx_plugin.json')):
        pin = '@'.join(path.parent.parts[-2:])
        verified = json.loads(command([sys.executable, str(root/'bin/gx-p0'), 'plugin', 'verify', pin], root, env=env))
        packages.extend(verified['plugins'])
    counts = dispatch_inventory(core)
    actual = contracts.contract_digest()
    pinned = json.loads((root/'gx-h/contracts/CONTRACT_SHA').read_text())
    if actual != pinned:
        raise ValueError('Contract files differ from CONTRACT_SHA')
    published = registry['published']
    native = published.get('native_goal_tools', {})
    return {
        'head': command(['git', 'rev-parse', 'HEAD'], root),
        'source_changes': command(['git', 'status', '--short', '--', 'gx-h/gx_core', 'gx-h/contracts'], root).splitlines(),
        'contract': actual,
        'registry': registry,
        'registry_counts': {key:len(items) for key,items in registry['compatibility_only'].items()},
        'tool_owners': {name: native.get(name, published['tools'].get(name)) for name in FORMAL_NAMES},
        'doctor': {'argv':['gx-h/bin/gx','doctor'], 'ok':doctor['ok'],
                   'providers_not_loaded':doctor['providers_not_loaded'], 'seams':doctor['seams'],
                   'shipped':doctor['shipped']},
        'protein_packages': packages,
        'inventory': counts,
        'inventory_counts': {**{k:len(v) for k,v in counts.items()},
                             'protein_modules':len({r['path'] for r in counts['protein_imports']})},
        'reviewed_documents_sha256': {name: sha(root/name) for name in REVIEWED_DOCS},
        'environment': {'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
                        'machine':platform.machine(),'node':command(['node','--version'],root),
                        'npm':command(['npm','--version'], root),
                        **{package:importlib.metadata.version(package) for package in ('Biopython','numpy')}},
    }


def compare(base, now):
    result = {}
    old_groups = base['registry']['compatibility_only']
    new_groups = now['registry']['compatibility_only']
    for name in sorted(old_groups.keys() | new_groups.keys()):
        old, current = set(old_groups.get(name, ())), set(new_groups.get(name, ()))
        result[name] = {'still_compatibility_only': sorted(old & current),
                        'newly_implemented':sorted(old - current), 'added_gaps':sorted(current - old)}
    return {'baseline_head':base['head'], 'candidate_head':now['head'], 'gap_comparison':result,
            'registered_tools': now['tool_owners'],
            'scan_baseline':base['inventory_counts'], 'scan_candidate':now['inventory_counts']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--compare', type=Path, metavar='BASE_JSON')
    args = parser.parse_args()
    current = snapshot(args.repo.resolve())
    if args.compare:
        current['comparison'] = compare(json.loads(args.compare.read_text()), current)
    args.out.write_text(json.dumps(current, indent=2, ensure_ascii=False) + '\n')
