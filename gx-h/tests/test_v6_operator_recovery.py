"""Independent Goal CLI and recovery scenarios. Only native turn acknowledgments are scripted."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import BudgetExceeded, GxError
from gx_core.orchestration import reactor
from gx_core.p0 import v6_decisions, v6_goal, v6_protein
from gx_core.p0.driver import Driver, context
from gx_core.p0.roots import Roots
from gx_core.persistence import store


REPO = Path(__file__).resolve().parents[2]
COORDINATES = REPO / 'gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif'


def decision(*ops, refs=()):
    return {'mode': 'amend' if ops else 'resolve',
            'rationale': 'Read the new observations under the fixed question and compare coverage.',
            'evidence_refs': list(refs),
            'reconsider_when': [{'event': 'evidence_added', 'subject_ref': 'run'}], 'ops': list(ops)}


def branch(local, jobs=1):
    return {'op': 'open_branch', 'branch': local, 'hypothesis': 'Common sites may be comparable.',
            'alternative': 'Missing sites may bias coverage.', 'method_family': 'Inventory',
            'discriminating_observation': 'A count of the comparable sites.',
            'budget_limits': {'job_submissions': jobs, 'cpu_seconds': jobs * 30}, 'stop_condition': 'Coverage is available.'}


def node(local, cap, inputs, depends_on=()):
    return {'op': 'add_node', 'branch': 'local:counts', 'node': local, 'capability': cap,
            'inputs': list(inputs), 'depends_on': list(depends_on), 'parameters': {},
            'question': 'How much of the original measurement can be reproduced?'}


class HumanReceipt:
    """A deterministic substitute for the native main's delivery acknowledgment only."""
    def ensure_main(self):
        return 'test-native-main'

    def human_turn(self, text, *, before_start, on_started, message_id):
        before_start(self.ensure_main(), message_id)
        on_started(self.ensure_main(), 'test-turn-' + message_id, message_id)


class OperatorRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.state = self.directory / 'project'
        self.db_path = self.state / 'gx.db'
        self.decoy = self.directory / 'notes.json'
        self.decoy.write_text('{"kind":"operator_note","contents":"instrument log"}')
        config = self.directory / 'config.json'
        config.write_text(json.dumps({'run_budget': {'job_submissions': 5, 'cpu_seconds': 300}, 'input_files': [
            {'role': 'coordinates', 'path': str(COORDINATES)}, {'role': 'note', 'path': str(self.decoy)}]}))
        start = self.cli('start', '--run-key', 'audit', '--goal', 'Is the coordinate measurement reproducible?',
                         '--goal-config', str(config))
        self.run_id = start['run_id']
        self.structure, self.note = start['materials']

    def cli(self, command, *args, expected=0):
        result = subprocess.run([sys.executable, '-B', str(REPO / 'bin/gx-p0'), command,
                                 '--state', str(self.state), *args],
                                capture_output=True, text=True, timeout=30, env={**os.environ, 'GX_CODEX_OFFLINE': '1'})
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.returncode in (0, 75) else result.stderr + result.stdout

    def action(self, mid, payload, **kwargs):
        path = self.directory / (mid + '.json')
        path.write_text(json.dumps(payload))
        return self.cli('message', '--run-id', self.run_id, '--message-id', mid, '--action', str(path), **kwargs)

    def claim(self, conn):
        task_id, = conn.execute("SELECT d.task_id FROM v6_decision_tasks d JOIN tasks t USING(task_id) "
                                "WHERE d.run_id=? AND t.status='READY'", (self.run_id,)).fetchone()
        roots = Roots(conn, run_id=self.run_id, cwd=self.directory)
        root = roots._claim(task_id)
        thread = 'scripted-root-' + task_id
        turn = 'scripted-turn-' + task_id
        with store.transaction(conn):
            conn.execute("UPDATE p0_roots SET state='ready', thread_id=? WHERE root_key=?", (thread, root['root_key']))
            conn.execute('UPDATE leases SET principal=? WHERE lease_id=?', (thread, root['lease_id']))
            conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,"
                         "interrupt,state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged','turn/start',?)",
                         (self.run_id, task_id, thread, 'cli-audit-'+task_id, ids.sha256_of_obj(root['brief']), turn))
        return dict(run_id=self.run_id, task_id=task_id, lease_id=root['lease_id'], thread_id=thread,
                    turn_id=turn, call_id='decision-'+task_id)

    def supplement(self, mid, text):
        self.cli('message', '--run-id', self.run_id, '--message-id', mid, '--supplement', text, '--queue-only')
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute('SELECT * FROM p0_operator_messages WHERE message_id=?', (mid,)).fetchone()
            if not row['result_json']:
                driver = Driver(conn, self.state, self.run_id)
                try:
                    driver._human_turn(HumanReceipt(), row)
                finally:
                    driver.close()
            answer, = conn.execute('SELECT result_json FROM p0_operator_messages WHERE message_id=?', (mid,)).fetchone()
            return json.loads(answer)

    def assert_no_science_writes(self, conn):
        for table in ('v6_graph_versions', 'v6_decisions', 'v6_nodes', 'jobs'):
            self.assertEqual(conn.execute(f'SELECT count(*) FROM {table} WHERE run_id=?',
                                          (self.run_id,)).fetchone()[0], 0, table)
        self.assertFalse(conn.execute("SELECT 1 FROM outbox WHERE run_id=? AND topic='v6.node'", (self.run_id,)).fetchone())

    def test_supplement_replay_and_cold_wake_preserve_the_frozen_brief(self):
        with closing(store.open_store(self.db_path)) as conn:
            identity = self.claim(conn)
            before = conn.execute('SELECT snapshot_json FROM research_contexts WHERE run_id=?', (self.run_id,)).fetchone()[0]
        text = 'A calibration control in the instrument log needs a second reading.'
        receipt = self.supplement('instrument-note', text)
        self.assertEqual(self.supplement('instrument-note', text), receipt)
        with closing(store.open_store(self.db_path)) as conn:
            maintained = v6_decisions.adopt(conn, **identity, decision=decision())
            self.assertEqual(maintained['outcome'], 'maintained')
        for _ in range(2):
            with closing(store.open_store(self.db_path)) as conn:
                reactor.drive_science(context(conn), self.run_id)
                snapshots = conn.execute('SELECT snapshot_json FROM research_contexts WHERE run_id=? ORDER BY version',
                                         (self.run_id,)).fetchall()
                self.assertEqual(len(snapshots), 2)
                self.assertEqual(snapshots[0][0], before)
                newer = json.loads(snapshots[1][0])
                source_id = receipt['source_id']
                self.assertEqual(newer['goal_version'], 1)
                self.assertIn(source_id, newer['evidence_refs'])
                self.assertEqual([o['id'] for o in newer['observations']], [source_id])
                self.assertIn(text, json.dumps(newer['observations'][0]['observation']))
                self.assertEqual(conn.execute('SELECT count(*) FROM jobs WHERE run_id=?', (self.run_id,)).fetchone()[0], 0)

    def test_confirmed_goal_change_fences_prepared_decision(self):
        with closing(store.open_store(self.db_path)) as conn:
            first = self.claim(conn)
            prepared = v6_decisions.prepare(conn, **first, decision=decision(branch('local:counts')))
        new_goal = 'Which input chains have enough evidence for comparison?'
        preview = self.action('new-goal', {'kind':'change','items':[{'item_id':'objective','item_type':'objective',
                              'expected_version':1,'value':new_goal}]})['result']
        self.assertEqual(preview['status'], 'awaiting_confirmation')
        confirmation = {'kind':'confirm','change_id':preview['change_id'],'preview_sha256':preview['preview_sha256']}
        if preview.get('required_confirmation'):
            confirmation['acknowledge_consequences'] = preview['required_confirmation']['acknowledge_consequences']
        approved = self.action('approved-goal', confirmation)['result']
        self.assertEqual(approved['status'], 'committed')
        self.assertEqual(self.action('approved-goal', confirmation)['result'], approved)
        with closing(store.open_store(self.db_path)) as conn:
            with self.assertRaises(GxError):
                v6_decisions.commit(conn, prepared)
            self.assert_no_science_writes(conn)
            reactor.drive_science(context(conn), self.run_id)
            fresh = self.claim(conn)
            current = v6_decisions.adopt(conn, **fresh, decision=decision())
            self.assertEqual(current['goal_version'], 2)
            self.assertNotEqual(current['task_id'], first['task_id'])
        self.assertEqual(self.cli('status', '--run-id', self.run_id)['goal'], new_goal)

    def test_existing_branch_and_spent_budget_fence_new_allocation(self):
        with closing(store.open_store(self.db_path)) as conn:
            budget_ref, = conn.execute('SELECT budget_ref FROM runs WHERE run_id=?', (self.run_id,)).fetchone()
            budgets.spend(conn, budget_ref=budget_ref, subject_kind='operator-fixture', subject_id='completed-work',
                          amounts={'job_submissions': 3}, actor='test')
            first = self.claim(conn)
            original = v6_decisions.adopt(conn, **first, decision=decision(branch('local:counts', jobs=2)))
        self.supplement('new-observation', 'An additional calculation may discriminate the two explanations.')
        with closing(store.open_store(self.db_path)) as conn:
            reactor.drive_science(context(conn), self.run_id)
            second = self.claim(conn)
            with self.assertRaises(BudgetExceeded):
                v6_decisions.adopt(conn, **second, decision=decision(branch('local:further', jobs=2)))
            self.assertEqual(v6_decisions.head(conn,self.run_id),original['result_head'])
            self.assertEqual(budgets.state(conn,budget_ref)['spent']['job_submissions'],3)
            self.assertEqual(conn.execute('SELECT count(*) FROM v6_branches WHERE run_id=?',(self.run_id,)).fetchone()[0],1)

    def test_invalid_ordered_inputs_reject_the_entire_first_draft(self):
        inventory = node('local:inventory', v6_protein.QUALITY, [self.structure])
        wrong_input = node('local:wrong', v6_protein.QUALITY, [self.note])
        wrong_source = node('local:review', v6_protein.REVIEW, [self.structure, 'local:inventory'], ['local:inventory'])
        with closing(store.open_store(self.db_path)) as conn:
            first = self.claim(conn)
            for bad in (wrong_input, wrong_source):
                with self.subTest(capability=bad['capability']):
                    with self.assertRaises(GxError):
                        v6_decisions.adopt(conn, **first, decision=decision(branch('local:counts'),inventory,bad))
                    self.assert_no_science_writes(conn)
            accepted = v6_decisions.adopt(conn, **first, decision=decision(branch('local:counts'), inventory))
            self.assertEqual(accepted['result_head']['version'], 1)
            self.assertEqual(len(accepted['dispatches']), 1)

    def test_confirm_preview_after_another_node_was_authorized(self):
        with closing(store.open_store(self.db_path)) as conn:
            first = self.claim(conn)
            initial = v6_decisions.adopt(conn, **first, decision=decision(branch('local:counts', jobs=3),
                node('local:inventory', v6_protein.QUALITY, [self.structure])))
        replacement = 'Which uncertainties should we measure in the next dataset?'
        change = {'kind': 'change', 'items': [{'item_id': 'objective', 'item_type': 'objective',
                  'expected_version': 1, 'value': replacement}]}
        preview = self.action('show-withdrawal', change)['result']
        self.assertEqual([item['object_id'] for item in preview['impact'] if item['action'] == 'withdraw'],
                         [initial['aliases']['local:inventory']])
        receipt = self.supplement('before-pi-confirms', 'Evidence arrived during the review window.')
        with closing(store.open_store(self.db_path)) as conn:
            # Freeze a new Lead brief after the packet arrives, before the Job consumer gets the scheduler.
            self.assertIsNotNone(v6_goal.ensure_task(conn, self.run_id))
            later = node('local:second', v6_protein.QUALITY, [self.structure])
            later['branch'] = initial['aliases']['local:counts']
            second = v6_decisions.adopt(conn, **self.claim(conn), decision=decision(later, refs=[receipt['source_id']]))
        confirmation = {'kind': 'confirm', 'change_id': preview['change_id'], 'preview_sha256': preview['preview_sha256'],
                        'acknowledge_consequences': preview['required_confirmation']['acknowledge_consequences']}
        refused = self.action('confirm-stale', confirmation, expected=1)
        self.assertIn('consequences changed', refused)
        status = self.cli('status', '--run-id', self.run_id)
        self.assertEqual(status['goal_version'], 1)
        self.assertEqual(status['graph_head'], second['result_head'])
        self.assertEqual([n['status'] for n in status['node_attempts']], ['PENDING', 'PENDING'])

    def test_operator_reopened_watch_fires_once_without_a_new_observation(self):
        with closing(store.open_store(self.db_path)) as conn:
            original = self.claim(conn)
            hold = decision()
            hold['reconsider_when'] = [{'event': 'operator_reopened', 'subject_ref': 'run'}]
            v6_decisions.adopt(conn, **original, decision=hold)
        self.cli('stop', '--run-id', self.run_id)
        self.cli('resume', '--run-id', self.run_id, '--timeout', '0.6', expected=75)
        self.cli('run', '--run-id', self.run_id, '--timeout', '0.3', expected=75)
        with closing(store.open_store(self.db_path)) as conn:
            tasks = conn.execute('SELECT t.task_id,t.status FROM tasks t JOIN v6_decision_tasks d USING(task_id) '
                                 'WHERE t.run_id=? ORDER BY d.rowid', (self.run_id,)).fetchall()
            self.assertEqual([row['status'] for row in tasks], ['ACCEPTED', 'READY'])
            self.assertEqual(tasks[0]['task_id'], original['task_id'])
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs WHERE run_id=?', (self.run_id,)).fetchone()[0], 0)

    def test_granted_material_and_method_pin_drift_are_rejected(self):
        draft = decision(branch('local:counts'), node('local:inventory', v6_protein.QUALITY, [self.structure]))
        with closing(store.open_store(self.db_path)) as conn:
            first = self.claim(conn)
            assets = ArtifactService(conn)
            original = assets.describe(self.structure)
            path = assets.resolve_path(original['namespace'], original['rel_path'])
            self.assertTrue(path.is_relative_to(self.state))
            preserved = path.with_name('original-grant')
            path.rename(preserved)
            path.write_bytes(b'different content under the same name')
            try:
                with self.assertRaises(GxError):
                    v6_decisions.adopt(conn, **first, decision=draft)
                self.assert_no_science_writes(conn)
            finally:
                preserved.replace(path)
            self.assertTrue(assets.verify(self.structure)['ok'])
            pins = conn.execute('SELECT capabilities_json FROM v6_goals WHERE run_id=?', (self.run_id,)).fetchone()[0]
            altered = {**json.loads(pins), v6_protein.QUALITY: '0' * 64}
            conn.execute('UPDATE v6_goals SET capabilities_json=? WHERE run_id=?', (json.dumps(altered), self.run_id))
            with self.assertRaises(GxError):
                v6_decisions.adopt(conn, **first, decision=draft)
            self.assert_no_science_writes(conn)
            conn.execute('UPDATE v6_goals SET capabilities_json=? WHERE run_id=?', (pins, self.run_id))
            accepted = v6_decisions.adopt(conn, **first, decision=draft)
            self.assertEqual(accepted['result_head']['version'], 1)

    def test_only_confirmed_budget_change_wakes_and_funds_a_larger_branch(self):
        with closing(store.open_store(self.db_path)) as conn:
            lead = self.claim(conn)
            hold = decision()
            hold['reconsider_when'] = [{'event': 'budget_changed', 'subject_ref': 'run'}]
            v6_decisions.adopt(conn, **lead, decision=hold)
        plan = self.cli('operator', 'budget-preview', '--run-id', self.run_id, '--message-id', 'raise-preview',
                        '--resource', 'job_submissions', '--limit', '7')['result']
        with closing(store.open_store(self.db_path)) as conn:
            reactor.drive_science(context(conn), self.run_id)
            self.assertEqual(conn.execute('SELECT count(*) FROM v6_decision_tasks WHERE run_id=?',
                                          (self.run_id,)).fetchone()[0], 1)
        applied = self.cli('operator', 'budget-apply', '--run-id', self.run_id, '--message-id', 'raise-applied',
                           '--preview-id', plan['preview_id'], '--preview-sha256', plan['preview_sha256'])['result']
        self.assertEqual((applied['status'], applied['limit']), ('committed', 7))
        with closing(store.open_store(self.db_path)) as conn:
            reactor.drive_science(context(conn), self.run_id)
            lead = self.claim(conn)
            adopted = v6_decisions.adopt(conn, **lead, decision=decision(branch('local:broader-survey', jobs=6)))
            self.assertEqual(adopted['result_head']['version'], 1)
            reactor.drive_science(context(conn), self.run_id)
            self.assertEqual(conn.execute('SELECT count(*) FROM v6_decision_tasks WHERE run_id=?',
                                          (self.run_id,)).fetchone()[0], 2)
