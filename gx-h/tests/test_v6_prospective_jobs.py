"""Admission and availability of a predicted scalar and an independently produced sufficient table."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core.p0 import v6_claims, v6_decisions, v6_goal, v6_jobs, v62_maturity
from gx_core.persistence import store
from tests import test_v62_outputs as instrument
from tests.test_v6_decisions import bind_native_fixture, branch, draft


METHOD = instrument.METHOD
TABLE = 'instrument-selected-rows@1'


def instrument_package(directory, declare_table):
    """Give the existing fixture a selection and a second method that exports its selected samples."""
    source = directory / 'instrument'
    shutil.copytree(instrument.FIXTURE, source)
    card = json.loads((source / 'series.json').read_text())
    manifest = card['manifest']
    manifest['entry']['argv_template'].append('{param.selection}')
    mean = manifest['outputs'][0]
    mean['metric_specs'] = {'reading': {'op': 'decimal/1', 'reader': 'json/1', 'pointer': '/value',
                                        'unit': '1', 'places': 2},
                            'sample_count': {'op': 'integer/1', 'reader': 'json/1', 'pointer': '/count',
                                             'unit': 'samples', 'places': 0}}
    if declare_table:
        mean['sufficient_outputs'] = [{'capability': TABLE, 'output': 'rows.json', 'input_role': 'readings',
                                       'match_parameters': ['selection']}]
    manifest['outputs'] = [mean]
    (source / 'series.json').write_text(json.dumps(card))
    select_samples = """import json
from pathlib import Path
import sys

source, output, selection = sys.argv[1:]
samples = json.loads(Path(source).read_text())["readings"]
rows = samples[-2:] if selection == "tail" else samples
"""
    (source / 'measure.py').write_text(select_samples + """
mean = sum(rows) / len(rows) if rows else None
summary = {"schema": "mean/1", "value": mean, "count": len(rows)}
(Path(output) / "mean.json").write_text(json.dumps(summary))
""")
    manifest['capability_id'] = TABLE.split('@')[0]
    manifest['outputs'] = [{'name': 'rows.json', 'kind': 'file:instrument_rows', 'required': True,
                            'nature': 'analysis', 'source_roles': ['readings']}]
    manifest['source']['path'] = manifest['source']['path'].replace('measure.py', 'rows.py')
    card['entry_script'] = 'rows.py'
    (source / 'rows.json').write_text(json.dumps(card))
    (source / 'rows.py').write_text(select_samples + """
(Path(output) / "rows.json").write_text(json.dumps({"samples": rows}))
""")
    with (source / 'plugin.py').open('a') as out:
        out.write('    ctx.capabilities.add_manifest("rows.json")\n')
        if declare_table:
            out.write('''    ctx.goal_methods.register("instrument-series@1", sufficient_metric=sufficient_metric)


def sufficient_metric(parameters, metric_id, table_capability, output_name, table_document):
    from math import isfinite
    if table_capability != "instrument-selected-rows@1" or output_name != "rows.json":
        return False
    rows = table_document.get("samples")
    if not isinstance(rows, list):
        return False
    if metric_id == "sample_count":
        return True
    return metric_id == "reading" and bool(rows) and all(type(n) in (int, float) and isfinite(n) for n in rows)
''')
    plugin = source / 'gx_plugin.json'
    spec = json.loads(plugin.read_text())
    spec['extensions'].append({'id': TABLE.split('@')[0], 'contract': 'capability', 'version': '1.0'})
    if declare_table:
        spec['extensions'].append({'id': 'series_index', 'contract': 'goal_method', 'version': '1.0'})
    plugin.write_text(json.dumps(spec))
    return source


class ProspectiveJobs(unittest.TestCase):
    def start(self, *, opening=False, declare_table=False, reimports=False, readings=None):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.enterContext(closing(store.open_store(self.directory / 'gx.db')))
        control = self.directory / 'control.json'
        control.write_text('{"readings":[2,4,6]}')
        extra_files = [{'role': 'control', 'path': str(control)}]
        if reimports:
            for role in ('renamed', 'independent'):
                path = self.directory / (role + '.json')
                path.write_text(json.dumps({'schema': 'instrument/1', 'readings': [1, 3, 5]}))
                extra_files.append({'role': role, 'path': str(path)})
        start_goal = v6_goal.start

        def start_with_methods(conn, spec, **kwargs):
            spec = dict(spec, capabilities=[METHOD, TABLE])
            if opening:
                spec['prospective_opening'] = {'capabilities': [METHOD], 'material_roles': ['readings'],
                                               'metric_ids': ['reading']}
            return start_goal(conn, spec, **kwargs)

        with patch.object(instrument, 'FIXTURE', instrument_package(self.directory, declare_table)), \
                patch.object(v6_goal, 'start', start_with_methods):
            self.run, self.raw = instrument.start_series_goal(self, self.db, self.directory, effort='medium',
                                    extra_files=extra_files, readings=readings)
        files = json.loads(self.db.execute('SELECT inputs_json FROM runs WHERE run_id=?', (self.run,)).fetchone()[0])['files']
        self.materials = {f['role']: f['artifact_ref'] for f in files}
        self.control = self.materials['control']
        for role, ref in self.materials.items():
            sample = 'sensor' if role in ('readings', 'renamed') else role
            v6_goal.record_observation(self.db, self.run, ref, source=role, acquisition=instrument.acquired(sample))
        self.actor = bind_native_fixture(self.db, self.run, self.directory)

    def plan(self, *measurements):
        ops = [dict(branch(), budget_limits={'job_submissions': len(measurements), 'cpu_seconds': 120})]
        for name, cap, material, selection in measurements:
            ops.append(dict(op='add_node', node='local:' + name, branch='local:sample', capability=cap,
                            inputs=[material], parameters={'selection': selection}, depends_on=[],
                            question='Measure the selected samples.'))
        accepted = v6_decisions.adopt(self.db, **self.actor, decision=draft(*ops))
        self.nodes = {name: accepted['aliases']['local:' + name] for name, *_ in measurements}

    def next_lead(self):
        v6_goal.record_observation(self.db, self.run, self.raw, source='operator-followup-' + self.actor['task_id'])
        task = v6_goal.ensure_task(self.db, self.run)
        self.actor = bind_native_fixture(self.db, self.run, self.directory, task_id=task)

    def stage(self, selection='all', material=None, call='predict', metric='reading'):
        raw = material or self.raw
        prediction = dict(target='analysis', data_refs=[raw], observable='/metrics/' + metric, operator='gt',
                          threshold=2, method=dict(capability=METHOD, output='mean.json', metric_id=metric,
                                                   input_role='readings', parameters={'selection': selection},
                                                   unit='samples' if metric == 'sample_count' else '1'))
        measured = 'sample count' if metric == 'sample_count' else 'mean'
        request = dict(statement=f'The {measured} will exceed the reference.', scope=instrument.SCOPE, level='open',
                       criteria=f'Compare the {measured} with the declared threshold.', limitations='This sample only.',
                       rationale='Register the expectation before running the analysis.', evidence_refs=[raw], prediction=prediction)
        ctx, refs = self.db.execute('SELECT context_ref,visible_refs_json FROM v6_decision_tasks WHERE task_id=?',
                                    (self.actor['task_id'],)).fetchone()
        return v6_claims.stage(self.db, **{**self.actor, 'call_id': call}, context_ref=ctx,
                               allowed_refs=json.loads(refs), payload=request)['claim_ref']

    def accept(self, *claims):
        return v6_decisions.adopt(self.db, **self.actor, decision={**draft(), 'evidence_refs': [self.raw, *claims]})

    def state(self, name):
        return self.db.execute('SELECT status,job_id FROM v6_nodes WHERE node_id=?', (self.nodes[name],)).fetchone()

    def settle(self, name):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            v6_jobs.advance(self.db, self.run)
            state = self.state(name)
            if state['status'] in v6_jobs.NODE_TERMINAL:
                self.assertEqual(state['status'], 'SUCCEEDED')
                return state['job_id']
            time.sleep(.03)
        self.fail(f'{name} failed to settle: {tuple(self.state(name))}')

    def test_opening_waits_only_for_candidate_material_and_requires_adoption(self):
        self.start(opening=True)
        self.plan(('target', METHOD, self.raw, 'all'), ('control', METHOD, self.control, 'all'))
        self.settle('control')
        self.assertEqual(tuple(self.state('target')), ('PENDING', None))
        self.next_lead()
        separate_metric = self.stage(call='count', metric='sample_count')
        self.accept(separate_metric)
        v6_jobs.advance(self.db, self.run)
        self.assertEqual(tuple(self.state('target')), ('PENDING', None))
        self.next_lead()
        proposal = self.stage()
        v6_jobs.advance(self.db, self.run)
        self.assertEqual(tuple(self.state('target')), ('PENDING', None))
        registered, = self.accept(proposal)['claim_revisions']
        job = self.settle('target')
        launch = self.db.execute("SELECT min(event_id) FROM events WHERE subject_id=? AND kind='job.transition'",
                                 (job,)).fetchone()[0]
        self.assertLess(registered['maturity']['registration']['event_id'], launch)

    def test_all_opening_nodes_park_and_wake_one_lead_without_a_new_observation(self):
        self.start(opening=True, declare_table=True)
        self.plan(('endpoint', METHOD, self.raw, 'all'), ('intermediate', TABLE, self.raw, 'all'))
        for _ in range(2):
            v6_jobs.advance(self.db, self.run)
        for name in ('endpoint', 'intermediate'):
            self.assertEqual(tuple(self.state(name)), ('PENDING', None))
        wake, = self.db.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND "
                                "kind='v6.prospective.opening_wait'", (self.run,))
        self.assertEqual(json.loads(wake['payload_json']), {'goal_version': 1})
        followup = v6_goal.ensure_task(self.db, self.run)
        self.assertIsNotNone(followup)
        self.assertIsNone(v6_goal.ensure_task(self.db, self.run))
        packet = self.db.execute('SELECT input_json FROM tasks WHERE task_id=?', (followup,)).fetchone()[0]
        changes = json.loads(packet)['brief']['trigger']['changes']
        self.assertIn(wake['event_id'], [change['event_id'] for change in changes])
        self.actor = bind_native_fixture(self.db, self.run, self.directory, task_id=followup)
        self.accept(self.stage())
        self.settle('endpoint')
        self.settle('intermediate')

    def test_staged_prediction_holds_reimport_but_not_an_independent_sample_with_identical_bytes(self):
        self.start(declare_table=True, reimports=True)
        alias = self.materials['renamed']
        independent = self.materials['independent']
        self.assertEqual(len({self.raw, alias, independent}), 3)
        hashes = self.db.execute('SELECT DISTINCT sha256 FROM artifacts WHERE artifact_ref IN (?,?,?)',
                                 (self.raw, alias, independent)).fetchall()
        self.assertEqual(len(hashes), 1)
        self.plan(('renamed', TABLE, alias, 'all'), ('independent', TABLE, independent, 'all'))
        self.next_lead()
        proposal = self.stage()
        job = self.settle('independent')
        self.assertEqual(tuple(self.state('renamed')), ('PENDING', None))
        self.accept(proposal)
        # An equal-input computation may reuse the now-complete Job after the decision.
        v6_jobs.advance(self.db, self.run)
        self.assertEqual(tuple(self.state('renamed')), ('REUSED', job))

    def test_staged_prediction_holds_only_its_material_and_selection_until_adopted(self):
        self.start(declare_table=True)
        self.plan(('direct', METHOD, self.raw, 'all'), ('table', TABLE, self.raw, 'all'),
                  ('other-selection', TABLE, self.raw, 'tail'), ('other-material', TABLE, self.control, 'all'))
        self.next_lead()
        proposal = self.stage()
        self.settle('other-selection')
        self.settle('other-material')
        for name in ('direct', 'table'):
            self.assertEqual(tuple(self.state(name)), ('PENDING', None))
        self.accept(proposal)
        self.settle('direct')
        self.settle('table')

    def test_completed_decision_leaves_no_hold_from_an_unused_draft(self):
        self.start()
        self.plan(('target', METHOD, self.raw, 'all'))
        self.next_lead()
        self.stage()
        v6_jobs.advance(self.db, self.run)
        self.assertEqual(tuple(self.state('target')), ('PENDING', None))
        self.assertEqual(self.accept()['claim_revisions'], [])
        self.settle('target')

    def test_already_generated_sufficient_table_marks_only_matching_endpoint_retrospective(self):
        self.start(declare_table=True)
        self.plan(('table', TABLE, self.raw, 'all'))
        self.next_lead()  # This actor's brief predates production, and it never reads the table.
        job = self.settle('table')
        produced = self.db.execute("SELECT event_id FROM events WHERE subject_id=? AND kind='job.transition' "
                                   "AND new_state='SUCCEEDED'", (job,)).fetchone()[0]
        same = self.stage()
        other = self.stage('tail', call='different-selection')
        ref_a, ref_b = self.accept(same, other)['claim_revisions']
        by_ref = {claim['claim_ref']: claim for claim in (ref_a, ref_b)}
        late, = by_ref[same]['maturity']['material_coverage']
        self.assertEqual(late['qualification'], 'retrospective')
        self.assertLessEqual(produced, late['endpoint_first_available_event_id'])
        self.assertLess(late['endpoint_first_available_event_id'], late['registration_event_id'])
        unrelated, = by_ref[other]['maturity']['material_coverage']
        self.assertEqual(unrelated['qualification'], 'unmeasured')
        with closing(store.open_store(self.directory / 'gx.db')) as cold:
            replay = v62_maturity.recompute(cold, self.run, by_ref[same]['claim_revision_id'])
        self.assertEqual(replay['material_coverage'], [late])

    def test_empty_table_determines_sample_count_but_leaves_the_mean_unavailable(self):
        self.start(declare_table=True, readings={'schema': 'instrument/1', 'readings': []})
        self.plan(('table', TABLE, self.raw, 'all'))
        self.next_lead()
        self.settle('table')
        mean = self.stage()
        count = self.stage(metric='sample_count', call='count')
        claims = {r['claim_ref']: r for r in self.accept(mean, count)['claim_revisions']}
        unavailable, = claims[mean]['maturity']['material_coverage']
        self.assertEqual(unavailable['qualification'], 'unmeasured')
        self.assertIsNone(unavailable['endpoint_first_available_event_id'])
        available, = claims[count]['maturity']['material_coverage']
        self.assertEqual(available['qualification'], 'retrospective')
        self.assertLess(available['endpoint_first_available_event_id'], available['registration_event_id'])
