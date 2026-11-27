"""Read immutable publication versions and quote only the views actually extracted."""
import hashlib
from io import BytesIO
from pathlib import Path
import json
import unittest

from adapters.scientific import published_documents as documents
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v62_sources as reader


def metadata(kind, accession, version):
    return dict(kind=kind, accession=accession, version=version, retrieved_at='2026-11-27T00:00:00Z',
                url='https://archive.example/publication')


class DocumentReaders(unittest.TestCase):
    def test_jats_version_and_identity_exclude_cited_and_nested_articles(self):
        raw = b'''<article><front><article-meta><article-id pub-id-type="pmc">123</article-id>
        <article-version>2</article-version></article-meta></front><body><sec id="s1">
        <table-wrap id="t1"><table><tr><td>0.89</td></tr></table></table-wrap></sec></body>
        <sub-article><front><article-meta><article-id pub-id-type="pmcid">PMC456</article-id>
        </article-meta></front></sub-article></article>'''
        digest = hashlib.sha256(raw).hexdigest()
        meta = metadata('article', 'PMC123', '2')
        for version in ('2', 'sha256:' + digest):
            self.assertEqual(documents.identity(dict(meta, version=version), raw, 'jats', digest)['status'],
                             'matched_accession')
        for invalid in (dict(meta, accession='PMC456'), dict(meta, version='3'), dict(meta, retrieved_at='unknown')):
            with self.assertRaises(SchemaInvalid):
                documents.identity(invalid, raw, 'jats', digest)
        self.assertEqual(reader._segments(raw, 'jats')[1][0][3:5], ('body/s1/t1/row[1]/cell[1]/td', '0.89'))

    def test_database_verifies_accession_and_release_and_preserves_rows(self):
        record = {'primaryAccession':'P12345', 'entryAudit':{'entryVersion':4},
                  'notes/value':[{'site':'archive X','value':None}, {'site':'archive Y','value':0}]}
        raw = json.dumps(record).encode()
        digest = hashlib.sha256(raw).hexdigest()
        meta = metadata('database_record', 'P12345', '4')
        identity = documents.identity(meta, raw, 'json', digest)
        self.assertEqual(identity['external_source'], meta)
        for invalid in (dict(meta, accession='P12346'), dict(meta, version='3')):
            with self.assertRaises(SchemaInvalid):
                documents.identity(invalid, raw, 'json', digest)
        coverage, segments, details = reader.document_index(raw, 'json')
        self.assertEqual(coverage, 'OFFICIAL_DOC_ONLY')
        self.assertEqual(details['locator_format'], 'JSON Pointer')
        self.assertEqual([(s[3], json.loads(s[4])) for s in segments[-2:]],
                         [('/notes~1value/0', record['notes/value'][0]), ('/notes~1value/1', record['notes/value'][1])])

    def test_html_body_abstract_table_and_non_visible_text(self):
        raw = b'''<html><head><title>Metadata</title></head><article><section id="abstract"><p>abstract only</p>
        </section><section id="results"><p>Source <b>value</b>.</p><script>alert()</script>
        <p hidden>bad</p><table id="measured"><tr><td>value</td><td>14</td></tr></table></section></article></html>'''
        coverage, segments, _ = reader.document_index(raw, 'html')
        self.assertEqual(coverage, 'PARTIAL_TEXT')
        self.assertEqual([s[4] for s in segments], ['abstract only', 'Source value.', 'value','14'])
        self.assertEqual(segments[0][2], 'abstract')
        self.assertIn('measured/td', segments[-1][3])
        self.assertEqual(reader.document_index(b'<article><section id="abstract"><p>Overview.</p></section></article>',
                                              'html')[0], 'ABSTRACT_ONLY')

    def test_pdf_reports_original_page_numbers_and_readable_coverage(self):
        import pypdf
        raw = (Path(__file__).parent / 'fixtures/labelled-source.pdf').read_bytes()
        coverage, segments, details = reader.document_index(raw, 'pdf')
        self.assertEqual((coverage, details['page_count'], details['pages_with_text']), ('PARTIAL_TEXT', 2, [2]))
        self.assertEqual(segments[0][3:5], ('page 2 [S-8]', 'Published observation on page two.'))
        with pypdf.PdfWriter() as doc:
            doc.add_blank_page(width=100, height=100)
            buf = BytesIO()
            doc.write(buf)
        coverage, segments, details = reader.document_index(buf.getvalue(), 'pdf')
        self.assertEqual((coverage, segments, details['pages_with_text']), ('UNAVAILABLE', [], []))


class AcquiredVisibility(unittest.TestCase):
    def test_assignment_event_is_required_at_the_readers_frozen_cursor(self):
        from gx_core.artifacts import ArtifactService
        from gx_core.errors import ScopeDenied
        from gx_core.p0 import v621_assignment
        from gx_core.persistence import store
        from tests.test_v62_sources import SourceGoal

        fixture = SourceGoal()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        conn, run = fixture.conn, fixture.run
        raw = b'{"site":"archive X","measurement":12}'
        digest = hashlib.sha256(raw).hexdigest()
        job = 'archive-job-from-scoped-test'
        original = ArtifactService(conn).publish_bytes(namespace=run, run_id=run, job_id=job,
            rel_path='jobs/archive/record.json', origin='JOB_OUTPUT', kind='database_record',
            media_type='application/json', data=raw)
        ref = original.artifact_ref
        provenance = dict(role='database', job_id=job, source_kind='job_acquired')
        reader.persist_index(conn, run, ref, digest, 'P12345.json', 'json', len(raw), provenance,
                             reader.document_index(raw, 'json'))
        before = fixture.cursor()
        with self.assertRaises(ScopeDenied):
            reader._source(conn, run, ref)
        assigned = store.append_event(conn, run_id=run, kind=v621_assignment.ASSIGNED, actor='gx:job',
            subject_kind='artifact', subject_id=ref,
            payload=dict(job_id=job, sha256=digest, role='database', name='P12345.json', goal_version=1))
        with self.assertRaises(ScopeDenied):
            reader._source(conn, run, ref, cursor=before)
        source = reader._source(conn, run, ref, cursor=assigned, verify=True)
        self.assertEqual(reader._pin(source)['job_id'], job)
        self.assertEqual(reader._segment(conn, source['source_id'], 'p2')['label'], '/measurement')
        # The original Lead lease predates the Job and the assignment.
        with self.assertRaises(ScopeDenied):
            fixture.call(fixture.lead, reader.READ, {'source_ref':ref, 'segment_id':'p2'})
