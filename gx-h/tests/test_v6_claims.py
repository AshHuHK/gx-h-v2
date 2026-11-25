"""The scientific state changes only with acceptance of a scoped, evidence-grounded decision."""
import json
import sqlite3
import unittest
from pathlib import Path

from gx_core.artifacts.service import ArtifactService
from gx_core.domain import clock, ids
from gx_core.errors import IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from gx_core.p0 import intent, schema, v6_claims
from gx_core.persistence import store
from tests import test_p0_roots


class ClaimRevisions(unittest.TestCase):
    def setUp(self):
        fixture = test_p0_roots.DurableRoots()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.roots._request = fixture.fake
        binding = fixture.bind()
        self.conn, self.run, self.task = fixture.conn, fixture.run, fixture.task
        self.dbfile = Path(fixture.tmp.name) / 'test.db'
        self.lease, self.thread = binding['lease_id'], binding['thread_id']
        self.context = ids.new_id('research_context')
        self.conn.execute("UPDATE tasks SET kind='decide_revision' WHERE task_id=?", (self.task,))
        intent._apply_local(self.conn, self.run, 'goal', {'kind': 'change', 'items': [
            {'item_id': 'objective', 'expected_version': 0, 'item_type': 'objective', 'value': 'geometry'}]})
        material = self.publish({'matched_positions': 164, 'comparison': 0.1})
        neutral = self.publish({'filename_changed': True})
        self.first, self.neutral = material, neutral
        snapshot = {'observations': [self.observation('reading', material), self.observation('new-name', neutral)]}
        refs = [material, neutral, 'reading', 'new-name']
        self.conn.execute("INSERT INTO v6_goals(run_id,goal,capabilities_json,materials_json,limits_json) "
                          "VALUES(?,'geometry','[]','[]','{}')", (self.run,))
        self.conn.execute("INSERT INTO research_contexts(context_ref,run_id,version,snapshot_json,sha256,event_cursor,"
                          "created_by,created_at) VALUES(?,?,1,?,?,0,'GX',?)",
                          (self.context, self.run, ids.canonical_json(snapshot), ids.sha256_of_obj(snapshot), clock.now_iso()))
        self.conn.execute("INSERT INTO v6_decision_tasks(task_id,run_id,scope,context_ref,goal_version,"
                          "visible_refs_json,event_key) VALUES(?,?,'run',?,1,?,'first')",
                          (self.task, self.run, self.context, json.dumps(refs)))
        self.conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,"
                          "interrupt,state,method,turn_id) VALUES(?,'first',?,'client','xx',0,0,'acknowledged','turn/start','turn')",
                          (self.run, self.thread))
        self.refs = refs

    def publish(self, value):
        return ArtifactService(self.conn).publish_bytes(namespace=self.run, run_id=self.run,
                  rel_path=f'{ids.sha256_of_obj(value)}.json', data=ids.canonical_json(value),
                  kind='synthetic_observation', media_type='application/json').artifact_ref

    def observation(self, name, ref):
        return {'id': name, 'subject_ref': name, 'artifact_ref': ref,
                'observation': ArtifactService(self.conn).read_json(ref)}

    def submit(self, key='stage', **overrides):
        payload = dict(statement='The measured comparison meets the stated threshold.', scope='sample A/B only',
                       criteria='difference <= 0.15', limitations='one deposited structure', rationale='measured 0.1',
                       level='limited_support', evidence_refs=['reading'], chain_pairs=['A/B'])
        payload.update(overrides)
        return v6_claims.stage(self.conn, self.run, self.task, self.lease, self.context, payload,
                              allowed_refs=self.refs, thread_id=self.thread, turn_id='turn', call_id=key)

    def adopt(self, staged, *, refs=('reading',), close=False):
        decision = {'evidence_refs': list(refs), 'ops': []}
        if close:
            decision['ops'] = [{'op': 'close_run', 'claim_refs': [staged['claim_ref']]}]
        else:
            decision['evidence_refs'].append(staged['claim_ref'])
        with store.transaction(self.conn, transaction_owner='ClaimTest', operation='adopt', run_id=self.run):
            return v6_claims.adopt(self.conn, self.run, self.task, self.lease, self.context, 'test-decision', decision)

    def test_unadopted_claim_has_no_effect_replay_reuses_artifact_conflict_is_explicit(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        with self.assertRaises(IdempotencyConflict):
            self.submit(statement='Another claim')
        self.assertEqual(v6_claims.current(self.conn, self.run), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE kind='v6.claim.staged'").fetchone()[0], 1)
        self.assertEqual(v6_claims.staged_refs(self.conn, self.run, self.task, 'other-lease', self.context,
                                               [first['claim_ref']]), [])
        with self.assertRaises(SchemaInvalid):
            self.adopt(first, refs=[])
        self.assertEqual(v6_claims.current(self.conn, self.run), [])
        self.assertEqual(self.adopt(first, close=True)[0]['claim_ref'], first['claim_ref'])

    def test_reinterpretation_preserves_source_and_revision_after_cold_open(self):
        first = self.adopt(self.submit())[0]
        self.refs.extend([first['claim_ref'], first['claim_revision_id']])
        self.conn.execute('UPDATE v6_decision_tasks SET visible_refs_json=? WHERE task_id=?',
                          (json.dumps(self.refs), self.task))
        second = self.submit(key='revise', previous_claim_ref=first['claim_revision_id'], level='contested',
                             criteria='difference <= 0.01', statement='The stricter comparison fails.', rationale='0.1 > 0.01')
        stale = self.submit(key='stale', previous_claim_ref=first['claim_ref'], statement='A simultaneous revision')
        revised = self.adopt(second)[0]
        with self.assertRaises(StaleRevision):
            self.adopt(stale)
        self.assertNotEqual(revised['criteria_sha256'], first['criteria_sha256'])
        self.assertEqual(revised['source_records'], first['source_records'])
        self.assertEqual(revised['source_records'][0]['artifact_ref'], self.first)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('DELETE FROM hypothesis_revisions WHERE revision=1')
        reopened = store.open_store(self.dbfile)
        self.addCleanup(reopened.close)
        schema.install(reopened)
        self.assertEqual(v6_claims.current(reopened, self.run), [revised])
        self.assertEqual(first, v6_claims.read_revision(reopened, self.run, first['claim_ref']))

    def test_neutral_material_unknown_sources_and_unbounded_promotions(self):
        with self.assertRaises(SchemaInvalid):
            self.submit(evidence_refs=['new-name'])
        with self.assertRaises(ScopeDenied):
            self.submit(evidence_refs=['foreign'])
        with self.assertRaises(SchemaInvalid):
            self.submit(level='confirmed')
        with self.assertRaises(SchemaInvalid):
            self.submit(level='refuted', evidence_refs=[])
        self.assertEqual(self.submit(level='open', evidence_refs=['new-name'])['status'], 'PROPOSED')

    def test_old_native_turn_and_goal_are_fenced_but_precise_replay_survives(self):
        first = self.submit()
        self.conn.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status) VALUES(?,'turn','completed')",
                          (self.thread,))
        with self.assertRaises(ScopeDenied):
            self.submit(key='after-terminal')
        self.conn.execute('UPDATE v6_goals SET goal_version=2 WHERE run_id=?', (self.run,))
        with self.assertRaises(ScopeDenied):
            self.submit(key='after-change')
        self.assertEqual(first, self.submit())


class EvidenceDuringDecision(unittest.TestCase):
    def test_counter_observation_after_freeze_is_presented_in_successor(self):
        from gx_core.p0 import v6_goal
        from tests import test_v6_native
        native = test_v6_native.ScientificLead()
        native.setUp()
        self.addCleanup(native.doCleanups)
        db, run = native.conn, native.run
        native.roots._call(native.params())  # First frozen decision, awaiting evidence.

        def record(source, measurement):
            output = ArtifactService(db).put(run_id=run, kind='synthetic_observation', media_type='application/json',
                        content=ids.canonical_json(measurement), origin='HOST_PUT', put_by='fixture')
            return v6_goal.record_observation(db, run, output.artifact_ref, source=source)

        original = record('first', {'difference': 0.1})
        task = v6_goal.ensure_task(db, run)
        row = native.roots._claim(task)
        thread = native.roots._bind(row, {'id': 'next-lead', 'cwd': row['cwd'], 'source': 'appServer'})
        native.roots.active_turns[thread] = 'next-turn'
        db.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,"
                   "interrupt,state,method,turn_id) VALUES(?,'second',?,'two','sha',0,0,'acknowledged','turn/start','next-turn')",
                   (run, thread))
        def call(call_id, tool, arguments):
            req = {**native.params(call_id, tool, arguments), 'turnId':'next-turn', 'threadId':thread}
            return native.roots._call(req)

        late = record('second', {'difference': 0.9})
        claim = {'statement': 'The first sample falls below the stated threshold.', 'scope': 'first sample',
                 'criteria': 'difference < 0.15', 'level': 'limited_support', 'evidence_refs':[original['artifact_ref']],
                 'rationale': '0.1 is below 0.15', 'limitations': 'one measurement'}
        with self.assertRaises(ScopeDenied):
            call('too-soon', 'gx_stage_claim', {**claim, 'evidence_refs': [late['artifact_ref']]})
        proposed = call('stage', 'gx_stage_claim', claim)
        decision = {**native.body, 'evidence_refs': [original['artifact_ref'], proposed['claim_ref']]}
        receipt = call('choose', 'gx_decide_revision', {'decision': decision})
        self.assertEqual(receipt['claim_revisions'][0]['event_cursor'], original['event_id'])
        self.assertEqual(receipt['claim_revisions'][0]['evidence_refs'], [original['artifact_ref']])
        again = store.open_store(store.data_root(db) / 'gx.db')
        self.addCleanup(again.close)
        schema.install(again)
        fresh = v6_goal.ensure_task(again, run)
        frozen = again.execute('SELECT snapshot_json FROM v6_decision_tasks JOIN research_contexts USING(context_ref) '
                               'WHERE task_id=?', (fresh,)).fetchone()
        brief = json.loads(frozen['snapshot_json'])
        self.assertEqual([o['artifact_ref'] for o in brief['observations'] if o['event'] == 'v6.observation.added'],
                         [late['artifact_ref']])
        self.assertEqual(brief['claims'][0]['decision_id'], receipt['decision_id'])
        self.assertIsNone(v6_goal.ensure_task(again, run))


class JobSourceProvenance(unittest.TestCase):
    def test_successful_output_and_runner_logs_from_same_actual_job_are_distinct(self):
        from tests import test_v6_jobs
        fixture = test_v6_jobs.DynamicJobs()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.new('measure')
        node = fixture.settle('measure')
        self.assertEqual(node['status'], 'SUCCEEDED')
        context = {'scope':'run', 'event_cursor': 0}
        result = v6_claims._ground(fixture.conn, fixture.run_id, node['result_ref'], [], context)
        self.assertEqual(result['source_class'], 'computed_result')
        self.assertEqual(result['job_id'], node['job_id'])
        self.assertIn(fixture.art, {entry['artifact_ref'] for entry in result['inputs']})
        log = fixture.conn.execute("SELECT artifact_ref FROM artifacts WHERE job_id=? AND kind='job_stdout'",
                                   (node['job_id'],)).fetchone()
        self.assertIsNotNone(log)
        diagnostic = v6_claims._ground(fixture.conn, fixture.run_id, log[0], [], context)
        self.assertEqual(diagnostic['source_class'], 'execution_record')


if __name__ == '__main__':
    unittest.main()
