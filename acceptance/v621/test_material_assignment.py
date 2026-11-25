"""A deposited source enters the real scientific Job only after the operator assigns it."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import PRODUCT, QUALITY, Scenario, draft, save
from gx_core.errors import ScopeDenied
from gx_core.p0 import manifest, v6_goal, v6_native, v62_lead


class MaterialAssignment(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.file = self.work / 'specimen.cif'
        self.file.write_bytes((PRODUCT / 'gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif').read_bytes())
        self.digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.metadata = self.work / 'source.json'
        save(self.metadata, {'source': 'wwPDB:2RE9:rev1.4', 'sample': None, 'sampling': None,
                             'applicability': 'deposited model 1 positions', 'publication': ['doi:10.1016/j.bbrc.2007.09.097']})
        self.spec = dict(input_files=[], research_effort='deep', research_modules=['data_inventory@1'],
                         evidence_policy='gx.evidence/6.2.1')

    def assign(self, s, decision, *, module=None, source='assigned-material', expect=True, **overrides):
        args = dict(input=self.file, sha256=self.digest, role='structure', acquisition=self.metadata,
                    source=source, **{'media-type': 'chemical/x-mmcif', 'after-decision': decision})
        if module:
            args['module-instance'] = module
        args.update(overrides)
        cmd = [sys.executable, '-B', str(PRODUCT / 'bin/gx-p0'), 'assign-material', '--state', str(s.state),
               '--run-id', s.run_id]
        for key, value in args.items():
            cmd.extend(['--' + key, str(value)])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if expect:
            self.assertEqual(0, proc.returncode, proc.stderr + proc.stdout)
            return json.loads(proc.stdout)
        self.assertNotEqual(0, proc.returncode, proc.stdout)

    def test_scoped_handoff_fences_old_turn_and_executes_one_job_after_restart(self):
        target = self.work / 'study'
        with Scenario(target, spec_updates=self.spec) as s:
            first = s.open('worker', inputs=[], caps=[QUALITY])
            old = s.bind(first['task_id'])
            other = s.bind(s.open('peer', inputs=[], caps=[QUALITY])['task_id'])
            original_brief = s.brief(old)
            self.assertNotIn(self.digest, json.dumps(original_brief))
            self.assign(s, 'unknown-first-plan', expect=False)
            plan = s.call(s.lead, v6_native.DECIDE, {'decision':draft()}, 'first-plan')['decision_ref']
            self.assign(s, plan, module=first['module_instance_id'], sha256='0'*64, expect=False)
            self.assign(s, plan, module='unknown-module', expect=False)
            self.assertEqual([], s.rows("SELECT * FROM events WHERE kind='v621.material.assigned'"))
            before = s.rows('SELECT grant_json,snapshot_json FROM v62_modules WHERE scope=?', old.scope)[0]
            receipt = self.assign(s, plan, module=first['module_instance_id'])
            ref = receipt['artifact_ref']
            self.assertEqual(receipt, self.assign(s, plan, module=first['module_instance_id']))
            self.assign(s, plan, module=other.scope.removeprefix('module:'), expect=False)
            self.assertLess(receipt['acquisition_event_id'], receipt['event_id'])
            self.assertLess(receipt['event_id'], receipt['module_event_id'])
            self.assertEqual(before, s.rows('SELECT grant_json,snapshot_json FROM v62_modules WHERE scope=?', old.scope)[0])
            self.assertEqual(original_brief, s.brief(old))
            for stale in (old, other):
                with self.assertRaises(ScopeDenied):
                    s.call(stale, 'gx_read_artifact', {'ref': ref, 'operation_key':'guessed'}, 'guessed')
            new_lead = s.next_actor('run')
            self.assertEqual(receipt['event_id'], s.brief(new_lead)['materials'][0]['assignment_event_id'])
        with Scenario(target, reopen=True) as s:
            s.advance()
            worker = s.next_actor(old.scope)
            self.assertNotEqual(old.task_id, worker.task_id)
            packet = s.brief(worker)
            [actual] = packet['materials']
            self.assertEqual((ref, self.digest, receipt['module_event_id']),
                             (actual['artifact_ref'], actual['sha256'], actual['grant_event_id']))
            s.call(worker, 'gx_read_artifact', {'ref':ref,'operation_key':'read-assignment'}, 'inspect')
            job_decision = s.submit_node(worker, 'count', input_ref=ref, capability=QUALITY)
            node = s.settle(job_decision['aliases']['local:count'])
            record = manifest.read_manifest(s.conn, node['job_id'])
            self.assertIn((ref, self.digest), [(i['artifact_ref'], i['sha256']) for i in record['admission']['inputs']])
            created, = s.rows("SELECT event_id FROM events WHERE run_id=? AND subject_id=? AND kind='job.created'",
                              s.run_id, node['job_id'])
            self.assertGreater(created['event_id'], receipt['module_event_id'])
            self.assertEqual(1, len(s.rows('SELECT job_id FROM jobs WHERE run_id=?', s.run_id)))
            self.assertEqual(receipt, self.assign(s, plan, module=first['module_instance_id']))
            output = os.getenv('GX_V621_ASSIGNMENT_RECEIPT')
            if output:
                save(Path(output), dict(status='PASS', run_id=s.run_id, assignment=receipt,
                    original_task_id=old.task_id, receiving_task_id=worker.task_id, lease_id=worker.lease_id,
                    job_admitted_event=created['event_id'], graph_decision=job_decision['decision_ref'], admission_sha256=record['admission_hash'],
                    manifest_sha256=record['manifest_hash'], job_id=node['job_id'], output_ref=node['result_ref'],
                    receipt_replays=2, paid_model_calls=0, provider_tokens=None, real_cpu_jobs=1))

    def test_material_arriving_between_brief_capture_and_freeze_is_delivered(self):
        with Scenario(self.work / 'overlapping', spec_updates=self.spec) as s:
            plan = s.call(s.lead, v6_native.DECIDE, {'decision': draft()}, 'plan')['decision_ref']
            earlier = self.assign(s, plan, source='first-specimen')
            later = {}
            deliver = v62_lead.deliver

            def assign_between(conn, snapshot, limit):
                self.assertEqual([earlier['event_id']], [m['assignment_event_id'] for m in snapshot['materials']])
                later.update(self.assign(s, plan, source='second-specimen'))
                return deliver(conn, snapshot, limit)

            with patch.object(v62_lead, 'deliver', side_effect=assign_between):
                self.assertIsNone(v6_goal.ensure_task(s.conn, s.run_id))
            current = s.brief(s.next_actor('run'))
            self.assertEqual([earlier['event_id'], later['event_id']],
                             [m['assignment_event_id'] for m in current['materials']])

    def test_later_instance_receives_only_lead_selected_assigned_input(self):
        with Scenario(self.work / 'later-study', spec_updates=self.spec) as s:
            plan = s.call(s.lead, v6_native.DECIDE, {'decision': draft()}, 'plan')['decision_ref']
            receipt = self.assign(s, plan)
            self.assertIsNone(receipt['module_event_id'])
            s.lead = s.next_actor('run')
            selected = s.bind(s.open('after', inputs=[receipt['artifact_ref']], caps=[QUALITY])['task_id'])
            unrelated = s.bind(s.open('peer', inputs=[], caps=[QUALITY])['task_id'])
            [material] = s.brief(selected)['materials']
            self.assertEqual(receipt['event_id'], material['assignment_event_id'])
            self.assertNotIn('grant_event_id', material)
            s.call(selected, 'gx_read_artifact', {'ref':receipt['artifact_ref'], 'operation_key':'inspect'}, 'inspect')
            with self.assertRaises(ScopeDenied):
                s.call(unrelated, 'gx_read_artifact', {'ref':receipt['artifact_ref'], 'operation_key':'guess'}, 'guess')
