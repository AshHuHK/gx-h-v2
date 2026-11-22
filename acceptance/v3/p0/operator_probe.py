"""Exercise the public plugin installer in an isolated byte-for-byte snapshot and save a receipt."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[3]
CUT_BEFORE_CATALOG_RENAME = '''import os
import sys
import signal

def before_catalog_rename(event, args):
    if event == 'os.rename' and os.fspath(args[1]) == os.environ['QA_CATALOG']:
        with open(os.environ['QA_REACHED'], 'x') as marker:
            marker.write(str(os.getpid()))
        os.kill(os.getpid(), signal.SIGSTOP)

sys.addaudithook(before_catalog_rename)
'''


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def files(directory):
    return {p.relative_to(directory).as_posix(): sha(p.read_bytes()) for p in sorted(directory.rglob('*')) if p.is_file()}


def digest(records):
    return sha(json.dumps(sorted(records.items()), ensure_ascii=False, separators=(',', ':')).encode())


def exercise(work, result, revision):
    base = work / 'snapshot'
    base.mkdir()
    paths = ['bin', 'gx-h', 'examples/plugins/hello_research']
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', (revision or 'HEAD') + '^{commit}'],
                                     cwd=ROOT, text=True).strip()
    if revision:
        with tempfile.TemporaryFile(dir=work) as stream:
            subprocess.run(['git', 'archive', commit, '--', *paths], cwd=ROOT, stdout=stream, check=True)
            stream.seek(0)
            with tarfile.open(fileobj=stream) as archive:
                archive.extractall(base, filter='data')
    else:
        ignore = shutil.ignore_patterns('__pycache__', '*.pyc', 'node_modules', '.plugin-install.lock')
        for path in paths:
            shutil.copytree(ROOT / path, base / path, symlinks=True, ignore=ignore)
    source_index = files(base)
    result.update(base_revision=commit, source_origin='immutable git archive' if revision else 'working-source copy',
                  snapshot_files=len(source_index), snapshot_sha256=digest(source_index),
                  snapshot_digest_method='SHA-256 compact sorted JSON array of [relative path, file SHA-256]',
                  source_sha256={k: v for k, v in source_index.items() if k == 'bin/gx-p0' or
                                 k == 'gx-h/gx_core/p0/cli.py' or k.startswith(('gx-h/gx_core/plugins/', 'examples/plugins/'))})
    example = base / 'examples/plugins/hello_research'
    approved = digest(files(example))
    manifest = json.loads((example / 'gx_plugin.json').read_bytes())
    family, version = manifest['id'], manifest['version']
    ref = family + '@' + version
    installed = base / 'gx-h/plugins/installed' / family / version
    catalog = base / 'gx-h/config/workflow_packages.json'
    legacy = json.loads(catalog.read_bytes())
    path = base / 'bin/gx-p0'
    steps = result['operations'] = []
    def launch(action, *args, extra_env=None):
        cmd = [sys.executable, '-B', str(path), 'plugin', action, *map(str, args)]
        env = {**os.environ, **(extra_env or {})}
        return subprocess.Popen(cmd, cwd=base, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def collect(proc, label, code):
        stdout, stderr = proc.communicate(timeout=20)
        try:
            output = json.loads(stdout) if stdout else None
        except ValueError:
            output = stdout
        steps.append({'case': label, 'pid': proc.pid, 'argv': proc.args, 'exit': proc.returncode,
                      'stdout': output, 'stderr': stderr.strip()})
        if proc.returncode != code:
            raise AssertionError(f'{label}: expected exit {code}, received {proc.returncode}, {stderr[:300]}')
        return output
    def run(label, action, *args, code=0):
        return collect(launch(action, *args), label, code)

    assert run('fresh catalog', 'list') == {'plugins': []}
    assert run('independent package hash', 'digest', example)['sha256'] == approved
    assert run('unapproved cold verify', 'verify', ref, code=1)['failures'].get(ref)
    package_filename = 'package/text/messages.py'
    original = (example / package_filename).read_bytes()
    (example / package_filename).write_bytes(original + b'\n# staging change\n')
    run('changed reviewed source refused', 'install', example, '--sha256', approved, code=1)
    assert not installed.exists() and json.loads(catalog.read_bytes()) == legacy
    (example / package_filename).write_bytes(original)

    # Stop an owned child after the package rename, immediately before catalog rename.
    instrumentation = work / 'observer'
    instrumentation.mkdir()
    (instrumentation / 'sitecustomize.py').write_text(CUT_BEFORE_CATALOG_RENAME)
    marker = work / 'catalog-before-rename'
    extra_env = {'PYTHONPATH': str(instrumentation), 'QA_CATALOG': str(catalog), 'QA_REACHED': str(marker)}
    crash = launch('install', example, '--sha256', approved, extra_env=extra_env)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not marker.exists() and crash.poll() is None:
            time.sleep(.01)
        assert marker.is_file() and int(marker.read_text()) == crash.pid
        assert digest(files(installed)) == approved and json.loads(catalog.read_bytes()) == legacy
        result['fault'] = {'signal': 'SIGKILL', 'owned_pid': crash.pid,
                           'cut': 'package published, immediately before catalog atomic rename',
                           'observer_sha256': sha(CUT_BEFORE_CATALOG_RENAME.encode()), 'env': extra_env}
    finally:
        if crash.poll() is None:
            crash.kill()
        collect(crash, 'first install interrupted before catalog publication', -signal.SIGKILL)
    assert run('cold list after process death', 'list') == {'plugins': []}
    assert run('cold verify after process death', 'verify', ref, code=1)['failures'].get(ref)
    run('retry interrupted install', 'install', example, '--sha256', approved)
    ok = run('cold load after install retry', 'verify', ref)
    assert ok['plugins'][0]['sha256'] == approved and ok['plugins'][0]['contributions']['tools'] == ['hello_research']

    (installed / package_filename).write_bytes(original + b'\n# installed bytes changed\n')
    denied = run('cold load refuses changed installed bytes', 'verify', ref, code=1)
    assert denied['failures'].get(ref) and not denied['plugins']
    run('installer refuses to overwrite changed installed version', 'install', example, '--sha256', approved, code=1)
    parked = work / 'parked-changed-package'
    installed.rename(parked)
    assert run('cold verify missing installed path', 'verify', ref, code=1)['failures'].get(ref)
    assert len(run('installed pin remains visible', 'list')['plugins']) == 1
    run('reinstall original bytes at missing path', 'install', example, '--sha256', approved)
    assert run('restored exact pin after cold reopen', 'verify', ref) == ok

    writers = []
    versions = [version, *[f'{version}-qa.{label}' for label in ('one', 'two')]]
    expected = {f'{family}/{version}': approved}
    for v in versions:
        package = example if v == version else work / ('reviewed-' + v)
        if package != example:
            shutil.copytree(example, package)
            (package / 'gx_plugin.json').write_text(json.dumps({**manifest, 'version': v}, indent=2) + '\n')
        package_sha = digest(files(package))
        expected[f'{family}/{v}'] = package_sha
        for i in range(2):
            writers.append(launch('install', package, '--sha256', package_sha))
    result['overlapping_writer_pids_before_wait'] = [proc.pid for proc in writers if proc.poll() is None]
    assert len(result['overlapping_writer_pids_before_wait']) >= 2
    for proc in writers:
        collect(proc, 'concurrent duplicate install', 0)
    rows = run('list after concurrent writers', 'list')['plugins']
    assert len(rows) == len(expected) and {r['path']:r['sha256'] for r in rows} == expected
    latest = json.loads(catalog.read_bytes())
    latest.pop('plugins')
    assert latest == legacy
    verified = run('cold load all exact versions', 'verify', *(family + '@' + v for v in versions))
    assert not verified['failures'] and len({p['namespace'] for p in verified['plugins']}) == 3
    assert {f"{p['id']}/{p['version']}": p['sha256'] for p in verified['plugins']} == expected
    result.update(approved_sha256=approved, restored_namespace=ok['plugins'][0]['namespace'],
                  final_records=rows, legacy_catalog_fields_unchanged=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--revision', help='Export exact committed product and example into staging')
    args = parser.parse_args()
    receipt = {'started_utc': utc(), 'probe_sha256': sha(Path(__file__).read_bytes()),
               'command': [sys.executable, *sys.argv], 'scope': 'Public CLI in isolated staging; local files only; observed process death during catalog publication'}
    with tempfile.TemporaryDirectory(prefix='gx-p0-operator-') as scratch:
        receipt['scratch'] = scratch
        try:
            exercise(Path(scratch), receipt, args.revision)
        except Exception as error:
            receipt.update(passed=False, error=f'{type(error).__name__}: {error}')
        else:
            receipt['passed'] = True
    receipt['ended_utc'] = utc()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print(f"{args.out}: {'PASS' if receipt['passed'] else receipt['error']}")
    sys.exit(0 if receipt['passed'] else 1)
