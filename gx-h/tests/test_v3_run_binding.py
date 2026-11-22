"""Public keyed starts persist an optional plugin selection with its Run and replay exact receipts."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from gx_core.jobs.service import JobService
from gx_core.persistence import store


ROOT = Path(__file__).resolve().parents[2]


class RunPluginBinding(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.stage = work / 'release'
        for name in ('bin', 'gx-h', 'examples'):
            shutil.copytree(ROOT / name, self.stage / name, symlinks=True, ignore=shutil.ignore_patterns(
                'node_modules', '__pycache__', '*.pyc', 'var', '.plugin-install.lock'))
        self.env = {**os.environ, 'GX_SCOPE': 'operator', 'GX_PROJECTS': '*',
                    'GX_P0_CODEX': str(work / 'unused-model-runtime')}
        self.state = work / 'workspace'
        self.state.mkdir()
        self.spec_path = work / 'study.json'
        self.spec = json.loads((self.stage / 'examples/paired-grid.json').read_bytes())
        self.cli('run', '--state', self.state, '--timeout', '2', error='Start a project before')
        self.db = self.enterContext(closing(store.open_store(self.state / 'gx.db')))
        self.addCleanup(self.settle_jobs)
        self.source = self.stage / 'examples/plugins/hello_research'
        self.manifest = json.loads((self.source / 'gx_plugin.json').read_bytes())
        self.ref = self.manifest['id'] + '@' + self.manifest['version']
        self.sha = self.cli('plugin', 'digest', self.source)['sha256']
        self.cli('plugin', 'install', self.source, '--sha256', self.sha)

    def cli(self, *args, error=None):
        proc = subprocess.run([sys.executable, '-B', str(self.stage / 'bin/gx-p0'), *map(str, args)],
                              cwd=self.stage, env=self.env, capture_output=True, text=True, timeout=30)
        if error:
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn(error, proc.stderr)
        else:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

    def start(self, key, spec, *, error=None):
        self.spec_path.write_text(json.dumps(spec))
        return self.cli('start', '--state', self.state, '--run-key', key, '--spec', self.spec_path, error=error)

    def settle_jobs(self):
        jobs = JobService(self.db)
        for row in self.db.execute('SELECT job_id FROM jobs').fetchall():
            self.assertTrue(jobs.wait(row['job_id'], 15)['terminal'])

    def receipts(self, run_id):
        return [tuple(row) for row in self.db.execute(
            'SELECT job_id,job_attempt,idempotency_key,scheduler_ref,reservation_ref,input_manifest_ref, '
            'outcome_json FROM jobs WHERE run_id=? ORDER BY job_id', (run_id,))]

    def assert_no_creation(self):
        for table in ('runs', 'objectives', 'jobs', 'budgets', 'budget_reservations', 'p0_workspace_runs'):
            self.assertEqual(self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)
        self.assertEqual(self.db.execute("SELECT count(*) FROM idempotency WHERE scope='gx_run_start'").fetchone()[0], 0)

    def test_unknown_version_and_mismatched_workflow_leave_zero_runs_and_jobs(self):
        self.start('unknown', {**self.spec, 'plugins': [self.manifest['id'] + '@99.0.0']},
                   error='no operator-approved installation')
        self.assert_no_creation()
        self.start('wrong-graph', {**self.spec, 'workflow_version': '1', 'plugins': ['org.gx.science-grid@2.0.0']},
                   error='selected graph science_grid@1')
        self.assert_no_creation()

    def test_rollback_after_pin_and_cold_retry_preserve_one_run_and_its_jobs(self):
        # The fault fires only after the exact pin and the workspace allocation exist in the creating transaction.
        self.db.execute('''CREATE TRIGGER refuse_test_objective BEFORE INSERT ON objectives
            WHEN (SELECT json_extract(inputs_json, '$.plugin_binding.packages[0].sha256') FROM runs
                  WHERE run_id=NEW.run_id) IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'test fault after plugin pin'); END''')
        spec = {**self.spec, 'plugins': [self.ref]}
        self.start('retry', spec, error='test fault after plugin pin')
        self.assert_no_creation()
        self.db.execute('DROP TRIGGER refuse_test_objective')
        first = self.start('retry', spec)
        self.settle_jobs()
        rid = first['run_id']
        before = self.receipts(rid)
        self.assertEqual(len(before), len(self.spec['proposal']['paths']))
        row = self.db.execute('SELECT inputs_json,workflow_sha256 FROM runs WHERE run_id=?', (rid,)).fetchone()
        binding = first['plugin_binding']
        self.assertEqual(binding, json.loads(row['inputs_json'])['plugin_binding'])
        self.assertEqual(binding['packages'], [{'id': self.manifest['id'], 'version': self.manifest['version'],
                                               'sha256': self.sha, 'api_version': self.manifest['api_version']}])
        self.assertEqual(binding['workflow'], {'id': 'science_grid', 'version': '2', 'sha256': row['workflow_sha256']})
        self.assertEqual(binding['capabilities'], [])
        replay = self.start('retry', spec)
        self.assertEqual((replay['run_id'], replay['plugin_binding']), (rid, binding))
        self.assertEqual(self.receipts(rid), before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1)

    def test_post_commit_partial_start_keeps_its_original_binding_before_any_job(self):
        self.db.execute("""CREATE TRIGGER stop_test_bootstrap BEFORE INSERT ON p0_operator_messages
            BEGIN SELECT RAISE(ABORT, 'test fault before bootstrap'); END""")
        spec = {**self.spec, 'plugins': [self.ref]}
        self.start('partial', spec, error='test fault before bootstrap')
        run, = self.db.execute('SELECT run_id,budget_ref,inputs_json FROM runs').fetchall()
        rid = run['run_id']
        binding = json.loads(run['inputs_json'])['plugin_binding']
        self.assertEqual(binding['packages'][0]['sha256'], self.sha)
        self.assertFalse(self.receipts(rid))
        self.assertIsNone(store.latest_checkpoint(self.db, rid))
        self.db.execute('DROP TRIGGER stop_test_bootstrap')
        installed = self.stage / 'gx-h/plugins/installed' / self.manifest['id'] / self.manifest['version']
        held = installed.rename(self.state.parent / 'held-package')
        self.start('partial', spec, error='plugins unavailable')
        self.assertFalse(self.receipts(rid))
        held.rename(installed)
        recovered = self.start('partial', spec)
        self.assertEqual((recovered['run_id'], recovered['plugin_binding']), (rid, binding))
        budgets = self.db.execute('SELECT budget_ref FROM runs').fetchall()
        self.assertEqual([item[0] for item in budgets], [run['budget_ref']])
        self.settle_jobs()
        jobs = self.receipts(rid)
        self.assertEqual(len(jobs), len(self.spec['proposal']['paths']))
        self.assertEqual(self.start('partial', spec)['run_id'], rid)
        self.assertEqual(self.receipts(rid), jobs)

    def test_changed_package_blocks_only_dependent_run_and_preserves_receipts(self):
        spec = {**self.spec, 'plugins': [self.ref]}
        original = self.start('composed', spec)
        rid = original['run_id']
        self.settle_jobs()
        before = self.receipts(rid)
        self.assertEqual(len(before), len(self.spec['proposal']['paths']))
        module = (self.stage / 'gx-h/plugins/installed' / self.manifest['id'] / self.manifest['version'] / 'plugin.py')
        approved_bytes = module.read_bytes()
        module.write_bytes(approved_bytes + b'\n# changed in isolated installation\n')
        status = self.cli('status', '--state', self.state, '--run-id', rid)
        self.assertEqual(status['plugin_binding'], original['plugin_binding'])
        blocked = self.cli('run', '--state', self.state, '--run-id', rid, '--timeout', '2')
        self.assertEqual(blocked['driver'], 'plugin_unavailable')
        self.assertIn('digest', blocked['reason'])
        # A replacement deployment can also change its catalog entry; the old Run still requires its saved bytes.
        catalog = self.stage / 'gx-h/config/workflow_packages.json'
        original_catalog = catalog.read_bytes()
        changed = json.loads(original_catalog)
        record, = [entry for entry in changed['plugins'] if entry['path'] == self.ref.replace('@', '/')]
        record['sha256'] = self.cli('plugin', 'digest', module.parent)['sha256']
        catalog.write_text(json.dumps(changed))
        repinned = self.cli('run', '--state', self.state, '--run-id', rid, '--timeout', '2')
        self.assertEqual(repinned['driver'], 'plugin_unavailable')
        self.assertIn('digest', repinned['reason'])
        self.assertEqual(repinned['plugin_binding'], original['plugin_binding'])
        self.assertEqual(self.receipts(rid), before)
        # Fresh public start and cold replay of an old spec in the same workspace still produce real CPU Jobs.
        legacy = self.start('legacy', self.spec)
        other = legacy['run_id']
        self.assertNotEqual(other, rid)
        self.assertNotIn('plugin_binding', legacy)
        self.settle_jobs()
        other_receipts = self.receipts(other)
        self.assertEqual(len(other_receipts), len(self.spec['proposal']['paths']))
        self.assertEqual(self.start('legacy', self.spec)['run_id'], other)
        self.assertEqual(self.receipts(other), other_receipts)
        self.cli('stop', '--state', self.state, '--run-id', other)
        aggregate = self.cli('run', '--state', self.state, '--timeout', '5')
        self.assertEqual(set(aggregate['plugin_waits']), {rid})
        self.assertEqual({r['run_id']: r['status'] for r in aggregate['runs']}[other], 'PAUSED')
        module.write_bytes(approved_bytes)
        catalog.write_bytes(original_catalog)
        replay = self.start('composed', spec)
        self.assertEqual((replay['run_id'], replay['plugin_binding']), (rid, original['plugin_binding']))
        self.assertEqual(self.receipts(rid), before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM p0_root_deliveries').fetchone()[0], 0)
