"""Local Goal entry points and scripted native transport, with independently bound source readers."""
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.native_release import native_state
from gx_core.p0 import cli, v6_claims, v6_goal, v62_sources as sources
from gx_core.p0.roots import Roots
from gx_core.persistence import store
from tests.test_v6_decisions import draft


class OfflineParser(unittest.TestCase):
    def test_namespace_inline_text_long_unicode_old_line_endings_and_abstract(self):
        xml = ('<?xml version="1.0"?><article xmlns="urn:jats"><front><abstract><p id="a1">'
               'A <italic>single</italic> summary.</p></abstract></front></article>').encode()
        coverage, segments = sources._segments(xml, 'jats')
        self.assertEqual(coverage, 'ABSTRACT_ONLY')
        self.assertEqual(segments[0][3:5], ('abstract/p/a1', 'A single summary.'))
        text = '量\\"\t' * 4000
        _, segments = sources._segments(('Intro\r\n\r\n' + text).encode(), 'markdown')
        self.assertEqual(segments[0][4], 'Intro')
        self.assertEqual(''.join(segment[4] for segment in segments[1:]), text)
        self.assertTrue(all(len(ids.canonical_json(s[4]).encode()) - 2 <= sources.SEGMENT_BYTES for s in segments))
        for bad in (b'<note/>', b'<article>', b'<!DOCTYPE article [<!ENTITY foo "private">]><article/>'):
            with self.subTest(bad=bad), self.assertRaises(SchemaInvalid):
                sources._segments(bad, 'jats')


class SourceGoal(unittest.TestCase):
    def setUp(self, *, note="North wall.\n\nThe west wall was damp.\n"):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(native_state(self.directory))
        self.path = self.directory / 'gx.db'
        self.conn = store.open_store(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.enterContext(patch('gx_core.p0.roots.tempfile.gettempdir', return_value=str(self.directory)))
        materials = [('note.txt', 'literature', note),
                     ('manual.md', 'source', '# Survey\n\nOnly the western transect was sampled.\n'),
                     ('article.nxml', 'literature', '<article><body><sec id="results"><p id="contrast">The '
                      '<italic>second</italic> station remained dry.</p></sec></body></article>'),
                     ('fake.txt','test_fixture','A synthetic test condition.')]
        files = []
        for name, role, content in materials:
            source = self.directory / name
            source.write_text(content, encoding='utf-8')
            files.append({'path':str(source), 'role':role})
        budget = {'api_calls':16, 'tokens':200000, 'worker_tasks':20, 'job_submissions':0, 'cpu_seconds':0,
                  'gpu_jobs':0, 'gpu_minutes':0, 'wall_hours':1}
        self.spec = {'mode':'goal', 'project':'wetness', 'objective':'Which independent accounts describe the west wall?',
            'research_effort':'deep', 'research_modules':['offline_literature_mechanism@1'], 'capabilities':[],
            'run_budget':budget, 'workspace_budget':{unit:value*2 for unit,value in budget.items()}, 'input_files':files}
        self.run = cli.start(self.conn, self.directory, self.spec, run_key='offline')['run_id']
        self.attach()
        task = self.conn.execute('SELECT task_id FROM v6_decision_tasks WHERE run_id=?', (self.run,)).fetchone()[0]
        self.lead = self.bind(task)
        materials = self.call(self.lead, 'gx_task_context', {})['materials']
        self.refs = {m['name']:m['artifact_ref'] for m in materials}

    def attach(self):
        self.roots = Roots(self.conn, run_id=self.run, cwd=self.directory)
        self.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))

    def bind(self, task):
        row = self.roots._claim(task)
        thread = self.roots._bind(row, {'id':'source-thread-' + task, 'cwd':row['cwd'], 'source':'appServer'})
        turn = 'turn-' + task
        self.roots.active_turns[thread] = turn
        self.conn.execute("""INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,
            human,interrupt,state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged','turn/start',?)""",
            (self.run, turn, thread, task, hashlib.sha256(row['brief'].encode()).hexdigest(), turn))
        return {'threadId':thread, 'turnId':turn}

    def call(self, actor, tool, args, key=None):
        envelope = {**actor, 'tool':tool, 'arguments':args, 'callId':key or ids.new_id('request')}
        if tool in self.roots.mode.decision_tools:
            envelope['namespace'] = 'gx'
        return self.roots._call(envelope)

    def open(self, actor, inputs, number=1, target_claim_refs=()):
        args = {'module_id':'offline_literature_mechanism', 'module_version':'1',
            'operation_key':ids.new_id('request'), 'focus':'literature', 'round':number, 'input_refs':inputs,
            'expected_delivery':'A located and independently checkable passage.', 'capabilities':[],
            'sub_budget':{'api_calls':2, 'tokens':3000, 'worker_tasks':3}}
        if target_claim_refs:
            args['target_claim_refs'] = list(target_claim_refs)
        receipt = self.call(actor, 'gx_open_module', args)
        return receipt, self.bind(receipt['task_id'])

    def paragraph(self, actor, name='note.txt', segment='p2'):
        return self.call(actor, sources.READ, {'source_ref':self.refs[name], 'segment_id':segment})

    @staticmethod
    def interpretation(read, **changes):
        return {**{k:read[k] for k in ('source_ref','segment_id','receipt_id')}, 'claim':'The west wall was damp.',
                'quote':read['text'], 'direction':'supports', 'applicability':'Western transect at the first visit',
                'confidence_basis':'The signed field note identifies the surveyed wall.', **changes}

    def publish(self, actor, refs, text):
        return self.call(actor, 'gx_submit_module_report', {'operation_key':ids.new_id('request'), 'status':'COMPLETE',
            'findings':[{'kind':'observation', 'text':text, 'source_refs':refs}], 'artifact_refs':[], 'gaps':[], 'next_steps':[]})

    def cursor(self):
        return self.conn.execute('SELECT max(event_id) FROM events WHERE run_id=?', (self.run,)).fetchone()[0]

    def test_formats_versions_scope_and_warm_receipts(self):
        _, reader = self.open(self.lead, list(self.refs.values()))
        cases = [('note.txt','p2','The west wall was damp.'), ('manual.md','p2','Only the western transect was sampled.'),
                 ('article.nxml','p1','The second station remained dry.'), ('fake.txt','p1','A synthetic test condition.')]
        for name, sid, text in cases:
            with self.subTest(name=name):
                listing = self.call(reader, sources.READ, {'source_ref':self.refs[name]})
                self.assertIn(sid, [seg['segment_id'] for seg in listing['segments']])
                self.assertNotIn('receipt_id', listing)
                before = self.conn.execute('SELECT count(*) FROM source_reads').fetchone()[0]
                args = {'source_ref':self.refs[name], 'segment_id':sid}
                read = self.call(reader, sources.READ, args, name)
                self.assertEqual(read, self.call(reader, sources.READ, args, name))
                self.assertEqual(read, self.call(reader, sources.READ, args))
                self.assertEqual(read['source_sha256'], hashlib.sha256((self.directory / name).read_bytes()).hexdigest())
                self.assertEqual(read['text_sha256'], hashlib.sha256(text.encode()).hexdigest())
                self.assertEqual(read['text'], text)
                self.assertEqual(self.conn.execute('SELECT count(*) FROM source_reads').fetchone()[0], before + 1)
                owner = self.conn.execute('SELECT principal,task_id,lease_id FROM source_reads WHERE receipt_id=?',
                                          (read['receipt_id'],)).fetchone()
                self.assertEqual(owner['principal'], reader['threadId'])
                found = self.call(reader, sources.EXTRACT, self.interpretation(read))
                self.assertEqual(found['record_nature'], 'test_fixture' if name=='fake.txt' else 'module_suggestion')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence').fetchone()[0], 4)

    def test_wrong_locator_and_missing_foreign_and_self_receipts(self):
        src = self.refs['note.txt']
        grant, worker = self.open(self.lead, [src])
        read = self.paragraph(worker)
        with self.assertRaisesRegex(ScopeDenied, 'receipt'):
            self.call(worker, sources.EXTRACT, self.interpretation(read, receipt_id='invented'))
        with self.assertRaises(ScopeDenied):
            self.paragraph(worker, 'article.nxml', 'p1')
        other = cli.start(self.conn, self.directory, self.spec, run_key='foreign')['run_id']
        foreign = self.conn.execute('SELECT json_extract(materials_json,\'$[0]\') FROM v6_goals WHERE run_id=?', (other,)).fetchone()[0]
        with self.assertRaises(ScopeDenied):
            self.call(worker, sources.READ, {'source_ref':foreign, 'segment_id':'p2'})
        wrong = self.call(worker, sources.EXTRACT, self.interpretation(read, quote='North wall.'))
        self.assertFalse(wrong['accepted'])
        self.assertEqual((wrong['status'], wrong['anchor_status'], wrong['found_in']), ('LOCATOR_UNRESOLVED','ELSEWHERE',['p1']))
        valid = self.call(worker, sources.EXTRACT, self.interpretation(read, direction='opposes'))
        uncertain = self.call(worker, sources.EXTRACT, self.interpretation(
            read, direction='unresolved', claim='The signed note gives no observation for the eastern transect.'))
        unresolved = sources.claim_ground(self.conn,self.run,uncertain['evidence_ref'],grant['scope'],self.cursor())
        self.assertEqual((unresolved['direction'],unresolved['status']), ('unresolved','LOCATOR_VERIFIED'))
        review = dict(evidence_ref=valid['evidence_ref'], receipt_id=read['receipt_id'],
                      quote_sha256_seen=valid['quote_sha256'], verdict='SUPPORTED', note='The note names the west wall.')
        with self.assertRaisesRegex(ScopeDenied, 'different task'):
            self.call(worker, sources.REVIEW, review)
        self.assertIsNone(sources.claim_ground(self.conn,self.run,valid['evidence_ref'],'run',self.cursor()))
        fact = sources.claim_ground(self.conn,self.run,valid['evidence_ref'],grant['scope'],self.cursor())
        self.assertEqual(fact['direction'], 'opposes')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_reviews').fetchone()[0], 0)

    def test_real_module_release_independent_review_and_cold_replay(self):
        # All identities below use the normal Roots router, with the native host transport scripted.
        src = self.refs['note.txt']
        premise = {
            'level':'open', 'statement':'The western transect was dry throughout the first visit.',
            'rationale':'A signed first-visit field note could challenge continuous dryness.',
            'scope':'Western transect at the first visit', 'criteria':'A located report of dampness opposes this claim.',
            'limitations':'A note cannot establish conditions everywhere beyond its survey.',
            'evidence_refs':[src]}
        staged = self.call(self.lead, 'gx_stage_claim', premise)
        second_claim = self.call(self.lead, 'gx_stage_claim', {**premise, 'statement':'The station remained dry overnight.'})
        proposal = {**draft(), 'evidence_refs':[src,staged['claim_ref'],second_claim['claim_ref']]}
        self.call(self.lead, 'gx_decide_revision', {'decision':proposal})
        claim = v6_claims.read_revision(self.conn,self.run,staged['claim_ref'])
        note = ArtifactService(self.conn).put(run_id=self.run, kind='site_note', origin='INGEST',
            media_type='application/json', content='{"question":"Is a located contrary observation available?"}',
            put_by='scripted-test').artifact_ref
        v6_goal.record_observation(self.conn,self.run,note,source='first-visit-question')
        self.lead = self.bind(v6_goal.ensure_task(self.conn,self.run))
        origin, worker = self.open(self.lead, [src], target_claim_refs=[claim['claim_revision_id']])
        read = self.paragraph(worker)
        with self.assertRaisesRegex(ScopeDenied, 'target'):
            self.call(worker, sources.EXTRACT, self.interpretation(read, target_claim_ref=second_claim['claim_ref']))
        found = self.call(worker, sources.EXTRACT, self.interpretation(
            read, direction='opposes', target_claim_ref=claim['claim_revision_id']))
        ref = found['evidence_ref']
        self.call(self.lead, 'gx_decide_revision', {'decision':draft()})
        self.assertIsNone(sources.claim_ground(self.conn,self.run,ref,'run',self.cursor()))
        first = self.publish(worker, [src,ref], 'This one observation qualifies the proposed wall-wide effect.')
        lead2 = self.bind(v6_goal.ensure_task(self.conn, self.run))
        self.assertIn(ref, self.call(lead2,'gx_task_context',{})['evidence_refs'])
        peer, reviewer = self.open(lead2, [src,ref], 2)
        self.call(lead2, 'gx_decide_revision', {'decision':draft()})
        own = self.paragraph(reviewer)
        request = dict(evidence_ref=ref, receipt_id=own['receipt_id'], quote_sha256_seen=found['quote_sha256'],
                       verdict='SUPPORTED', note='The passage directly identifies the first visit to the western transect.')
        with self.assertRaisesRegex(ScopeDenied, 'receipt'):
            self.call(reviewer, sources.REVIEW, {**request, 'receipt_id':read['receipt_id']})
        with self.assertRaisesRegex(SchemaInvalid, 'exact quote'):
            self.call(reviewer, sources.REVIEW, {**request, 'quote_sha256_seen':'0'*64})
        result = self.call(reviewer, sources.REVIEW, request, 'review')
        self.assertEqual(result, self.call(reviewer, sources.REVIEW, request, 'review'))
        contrary = self.call(reviewer, sources.REVIEW, {**request, 'verdict':'OVERSTATED', 'note':'The signed note gives no visit date.'})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM evidence_reviews').fetchone()[0], 2)
        rr = result['review_ref']
        before = self.cursor()
        self.assertIsNone(sources.claim_ground(self.conn,self.run,rr,'run',before))
        self.assertEqual(sources.published_reviews(self.conn,self.run,'run',before,claim['claim_revision_id']), [])
        final = self.publish(reviewer, [src,ref,rr,contrary['review_ref']], 'Two judgments share a paragraph and retain their reasons.')
        final_cursor = self.cursor()
        fact = sources.claim_ground(self.conn,self.run,rr,'run',final_cursor)
        self.assertEqual(fact['publication_event_id'], final['wake_event_id'])
        self.assertLess(first['wake_event_id'], fact['publication_event_id'])
        self.assertEqual((fact['record_nature'],fact['direction'],fact['verdict']), ('formal_evidence','opposes','SUPPORTED'))
        self.assertEqual((fact['extractor']['scope'],fact['reviewer']['scope']), (origin['scope'],peer['scope']))
        for field in ('task_id','lease_id','thread_id','context_ref'):
            self.assertNotEqual(fact['extractor'][field],fact['reviewer'][field])
        self.assertNotEqual(fact['extract_receipt_id'], fact['review_receipt_id'])
        public = sources.published_reviews(self.conn,self.run,'run',final_cursor,staged['claim_ref'])
        self.assertEqual([row['review_ref'] for row in public], [rr,contrary['review_ref']])
        self.assertIsNone(sources.claim_ground(self.conn,self.run,rr,'run',before))
        self.conn.close()
        self.conn = store.open_store(self.path)
        self.assertEqual(fact, sources.claim_ground(self.conn,self.run,rr,'run',final_cursor))
        disposition = sources.claim_ground(self.conn,self.run,contrary['review_ref'],'run',final_cursor)
        self.assertEqual((disposition['status'],disposition['verdict']), ('DISPUTED','OVERSTATED'))
        self.assertEqual(self.conn.execute('SELECT status FROM evidence WHERE evidence_id=?', (found['evidence_id'],)).fetchone()[0], 'DISPUTED')
        self.attach()
        next_lead = self.bind(v6_goal.ensure_task(self.conn,self.run))
        revised = next(c for c in v6_claims.current(self.conn,self.run) if c['hypothesis_id'] == claim['hypothesis_id'])
        self.assertGreater(revised['revision'],claim['revision'])
        _, subsequent = self.open(next_lead,[src],3,target_claim_refs=[revised['claim_revision_id']])
        follow = self.paragraph(subsequent)
        later = self.call(subsequent,sources.EXTRACT,self.interpretation(follow,target_claim_ref=revised['claim_revision_id']))
        self.assertTrue(later['accepted'])
