"""Offline, versioned source reads and independent reviews for scoped Goal tasks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from adapters.scientific import published_documents

from .. import contracts, schema
from ..artifacts import ArtifactService
from ..domain import clock, ids
from ..errors import SchemaInvalid, ScopeDenied, VersionImmutable
from ..persistence import store
from . import v6_decisions

READ, EXTRACT, REVIEW = TOOL_NAMES = ('gx_read_source', 'gx_extract_evidence', 'gx_review_evidence')
EXTRACTION = 'v62_source_extraction'
REVIEW_RECORD = 'v62_evidence_review'
PARSER = 'gx.offline.paragraphs.2'
LEGACY_PARSER = 'gx.offline.paragraphs.1'
FORMATS = {'.txt': 'text', '.text': 'text', '.md': 'markdown', '.markdown': 'markdown',
           '.xml': 'jats', '.nxml': 'jats', '.jats': 'jats', '.json': 'json',
           '.pdf': 'pdf', '.htm': 'html', '.html': 'html'}
MEDIA = {'text':'text/plain', 'markdown':'text/markdown','jats':'application/xml',
         'pdf':'application/pdf', 'html':'text/html', 'json':'application/json'}
PAGE = 12
# Leave room for locators, escape sequences and the receipt in the native tool response.
SEGMENT_BYTES = 2400


def tool_specs():
    return [contracts.native_goal_tool_spec(name) for name in TOOL_NAMES]


def source_spec(name, *, searchable=False):
    spec = contracts.native_goal_tool_spec(name)
    if name != READ or not searchable:
        return spec
    schema = spec["inputSchema"]
    return {**spec, "description": spec["description"] +
            " For a long original, list matching paragraph locators using query (a literal word or phrase), "
            "then read a selected segment_id for its exact words and receipt.",
            "inputSchema": {**schema, "properties": {**schema["properties"],
                "query": {"type":"string","minLength":1,"maxLength":160}}}}


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _blocks(raw, fmt, *, metadata=None):
    if fmt == 'jats':
        root = published_documents.jats_article(raw)
        if metadata is not None:
            identifiers = published_documents.article_identifiers(root)
            if identifiers:
                metadata["publication_identifiers"] = sorted(identifiers)
        def name(node):
            return node.tag.rsplit('}', 1)[-1]
        blocks = []
        # Direct ancestry distinguishes an abstract from the body and prevents duplicate
        # extraction of nested inline markup. Locations keep the publisher's IDs.
        stack = [(root, '', '', '')]
        body_paragraph = False
        while stack:
            node, region, section, table = stack.pop()
            tag = name(node)
            if tag in ('front', 'abstract', 'body', 'back'):
                region = tag
            if tag == 'table-wrap':
                table = node.get('id') or 'table'
            if tag == 'sec':
                section = node.get('id') or section
            if tag in ('p', 'article-title', 'title', 'td', 'th', 'mixed-citation'):
                text = ''.join(node.itertext()).strip()
                if text:
                    locator = '/'.join(filter(None, (region, section, table, tag, node.get('id'))))
                    blocks.append((region or 'text', locator[:160], text))
                    body_paragraph |= region == 'body' and tag in ('p', 'td', 'th')
            else:
                children = []
                row = cell = 0
                for child in node:
                    tag_child = name(child)
                    position = table
                    if tag_child in ('thead', 'tbody', 'tfoot'):
                        position = '/'.join(filter(None, (position, tag_child)))
                    elif tag_child == 'tr':
                        row += 1
                        position = f'{position or "table"}/row[{row}]'
                    elif tag_child in ('td', 'th'):
                        cell += 1
                        position = f'{position or "table"}/cell[{cell}]'
                    children.append((child, region, section, position))
                stack.extend(reversed(children))
        regions = {kind for kind, _, _ in blocks}
        coverage = 'FULL_TEXT' if body_paragraph else 'ABSTRACT_ONLY' if 'abstract' in regions else 'PARTIAL_TEXT'
        return coverage, blocks
    try:
        lines = raw.decode('utf-8-sig').splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise SchemaInvalid('Offline text and Markdown sources must be UTF-8') from exc
    blocks = []
    start = 0
    for i, line in enumerate([*lines, '\n']):
        heading = fmt == 'markdown' and re.match(r'^ {0,3}#{1,6}\s', line)
        if line.strip() and not heading:
            continue
        if start < i:
            blocks.append(('paragraph', f'lines {start+1}-{i}', ''.join(lines[start:i]).rstrip('\r\n')))
        if heading:
            blocks.append(('heading', f'line {i+1}', line.rstrip('\r\n')))
        start = i + 1
    # For unstructured notes the whole file can be read, but its extent as a
    # publication is unknown. Explicitly abstract-only notes retain that limit.
    headings = [text.strip().strip('#').strip().strip(':').casefold() for kind, _, text in blocks
                if kind == 'heading' or text.strip().casefold() in ('abstract', 'abstract:', '摘要')]
    abstract_only = headings[-1:] in (['abstract'], ['摘要'])
    return 'ABSTRACT_ONLY' if abstract_only else 'PARTIAL_TEXT', blocks


def document_index(raw, fmt):
    if fmt == 'html':
        coverage, blocks, details = published_documents.html_blocks(raw)
    elif fmt == 'pdf':
        coverage, blocks, details = published_documents.pdf_blocks(raw)
    elif fmt == 'json':
        coverage, blocks, details = published_documents.json_blocks(raw)
    else:
        details = ({'scope': 'JATS article text; figures and supplementary files require separate reads.'}
                   if fmt == 'jats' else {})
        coverage, blocks = _blocks(raw, fmt, metadata=details)
    segments = []
    for paragraph, (kind, label, text) in enumerate(blocks, 1):
        start, part = 0, 1
        while start < len(text):
            # Bound the rendered UTF-8 JSON, including escape characters. Binary search
            # only the next native-sized window, even for a single very large paragraph.
            lo, hi = start + 1, min(len(text), start + SEGMENT_BYTES)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(ids.canonical_json(text[start:mid]).encode('utf-8')) - 2 <= SEGMENT_BYTES:
                    lo = mid
                else:
                    hi = mid - 1
            piece = text[start:lo]
            segment_id = f'p{paragraph}'
            location = label
            if start or lo < len(text):
                segment_id += f'.{part}'
                location = f'{label} characters [{start},{lo})'
            segments.append((segment_id, len(segments), kind, location, piece, _sha(piece), len(piece)))
            start, part = lo, part + 1
    if not segments and coverage != 'UNAVAILABLE':
        raise SchemaInvalid('The registered source contains no readable paragraphs')
    return coverage, segments, details


def _segments(raw, fmt):
    coverage, segments, _ = document_index(raw, fmt)
    return coverage, segments


def persist_index(conn, run_id, ref, digest, name, fmt, length, provenance, index):
    sid = 'gxs_' + ids.sha256_of_obj([run_id, ref, digest, PARSER])[:32]
    if conn.execute("SELECT 1 FROM sources WHERE source_id=?", (sid,)).fetchone():
        return sid
    coverage, segments, details = index
    if details:
        provenance = {**provenance, 'parsing': details}
    conn.execute("""INSERT INTO sources(source_id,namespace,status,kind,title,version,blob_artifact_ref,
        blob_sha256,bytes,media_type,parser,parser_version,text_coverage,segments_count,origin,provenance_json,
        created_at) VALUES(?,?,'PARSED',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, run_id, fmt, name, digest, ref, digest, length, MEDIA[fmt], PARSER, '2', coverage,
         len(segments), 'JOB_OUTPUT' if provenance.get('job_id') else 'INGEST',
         ids.canonical_json(provenance), clock.now_iso()))
    conn.executemany('INSERT INTO source_segments(source_id,segment_id,ordinal,kind,label,text,text_sha256,char_count) '
                     'VALUES(?,?,?,?,?,?,?,?)', ((sid, *segment) for segment in segments))
    return sid


def _source(conn, run_id, ref, *, verify=False, cursor=None):
    # Only original inputs admitted by the Goal owner participate. A supplied output,
    # report or arbitrary path cannot masquerade as a registered publication.
    material = conn.execute("""SELECT a.*,json_extract(f.value,'$.role') AS role,
        json_extract(f.value,'$.path') AS input_path FROM runs r JOIN json_each(r.inputs_json,'$.files') f
        JOIN artifacts a ON a.run_id=r.run_id AND a.artifact_ref=json_extract(f.value,'$.artifact_ref')
        AND a.sha256=json_extract(f.value,'$.sha256') WHERE r.run_id=? AND a.artifact_ref=? AND a.published=1""",
        (run_id, ref)).fetchone()
    if not material:
        # The caller already checked the exact frozen scope. A registered PI text
        # attachment supplies the original bytes and filename to the same parser.
        material = conn.execute('''SELECT a.*, json_extract(e.payload_json,'$.role') AS role,
            json_extract(e.payload_json,'$.name') AS input_path FROM artifacts a JOIN events e USING(run_id)
            WHERE a.run_id=? AND a.artifact_ref=? AND e.kind='pi.attachment.registered'
            AND a.artifact_ref=json_extract(e.payload_json,'$.artifact_ref')
            AND a.sha256=json_extract(e.payload_json,'$.sha256') AND a.published=1 LIMIT 1''',
            (run_id, ref)).fetchone()
    if not material:
        # Admission and scope are separate: the pinned Job assignment must have existed
        # when this reader's task was frozen, even if the raw output was already published.
        material = conn.execute("""SELECT a.*,json_extract(e.payload_json,'$.role') AS role,
            json_extract(e.payload_json,'$.name') AS input_path FROM events e
            JOIN artifacts a ON a.run_id=e.run_id AND a.artifact_ref=e.subject_id
            WHERE e.run_id=? AND e.subject_id=? AND e.kind='v621.material.assigned' AND e.actor='gx:job'
            AND a.origin='JOB_OUTPUT' AND a.published=1 AND a.job_id=json_extract(e.payload_json,'$.job_id')
            AND a.sha256=json_extract(e.payload_json,'$.sha256') AND (? IS NULL OR e.event_id<=?)
            ORDER BY e.event_id LIMIT 1""", (run_id, ref, cursor, cursor)).fetchone()
        if not material:
            raise ScopeDenied('Source must be an original, registered material of this Goal')
    path = Path(material['input_path'])
    fmt = FORMATS.get(path.suffix.lower())
    if not fmt:
        raise SchemaInvalid('This registered file has no paragraph or record reader')
    if fmt == 'json' and material['origin'] != 'JOB_OUTPUT':
        raise SchemaInvalid('JSON record reads require a registered database original')
    digest = material['sha256']
    sid = 'gxs_' + ids.sha256_of_obj([run_id, ref, digest, PARSER])[:32]
    old_sid = 'gxs_' + ids.sha256_of_obj([run_id, ref, digest, LEGACY_PARSER])[:32]
    row = conn.execute('SELECT * FROM sources WHERE source_id IN (?,?) ORDER BY created_at LIMIT 1',
                       (sid, old_sid)).fetchone()
    if material['origin'] == 'JOB_OUTPUT' and (not row or row['origin'] != 'JOB_OUTPUT'):
        raise ScopeDenied('This acquired output has no verified document registration')
    if not row or verify:
        raw = ArtifactService(conn).read_bytes(ref, expected_sha256=digest)
    if not row:
        provenance = {'role': material['role']}
        message_id = store.loads(material, 'provenance_json', {}).get('source_message_id')
        if message_id:
            provenance.update(source_message_id=message_id, source_kind='pi_material')
        sid = persist_index(conn, run_id, ref, digest, path.name, fmt, len(raw), provenance, document_index(raw, fmt))
        row = conn.execute('SELECT * FROM sources WHERE source_id=?', (sid,)).fetchone()
    if ((row['namespace'], row['blob_artifact_ref'], row['blob_sha256'], row['version']) != (run_id, ref, digest, digest)
            or row['status'] != 'PARSED' or row['retraction_status'] == 'RETRACTED'):
        raise VersionImmutable('The named version is no longer an available, parsed source')
    return row


def _segment(conn, sid, segment_id):
    seg = conn.execute('SELECT * FROM source_segments WHERE source_id=? AND segment_id=?', (sid, segment_id)).fetchone()
    if not seg:
        raise SchemaInvalid('The source version has no such paragraph locator')
    if _sha(seg['text']) != seg['text_sha256'] or len(seg['text']) != seg['char_count']:
        raise VersionImmutable('The stored paragraph no longer matches its parsed digest')
    return seg


def _pin(src):
    provenance = store.loads(src, 'provenance_json', {})
    supplied = {k: provenance[k] for k in ('source_kind', 'source_message_id', 'external_source', 'job_id',
                                          'descriptor_ref', 'source_identity_status', 'parsing') if k in provenance}
    return dict(**supplied, run_id=src['namespace'], source_id=src['source_id'], source_ref=src['blob_artifact_ref'], source_sha256=src['blob_sha256'],
                source_version=src['version'], parser=src['parser'], text_coverage=src['text_coverage'])


def _read(conn, src, args, identity):
    sid = src['source_id']
    if 'query' in args:
        if 'segment_id' in args:
            raise SchemaInvalid('query finds candidate paragraphs; read a named segment separately')
        start = min(args.get('start', 0), src['segments_count'])
        sql = """SELECT segment_id,label,kind,char_count,ordinal FROM source_segments WHERE source_id=?
            AND ordinal>=? AND (instr(lower(label),lower(?))>0 OR instr(lower(text),lower(?))>0)
            ORDER BY ordinal LIMIT ?"""
        needle = args['query'].strip()
        rows = conn.execute(sql, (sid,start,needle,needle,PAGE+1)).fetchall()
        return {**_pin(src), 'query':needle, 'segments':[{k:r[k] for k in ('segment_id','label','kind','char_count','ordinal')}
            for r in rows[:PAGE]], 'segments_count':src['segments_count'],
            'next_start':rows[PAGE]['ordinal'] if len(rows)>PAGE else None}
    if 'segment_id' not in args:
        start = min(args.get('start', 0), src['segments_count'])
        rows = conn.execute('SELECT segment_id,label,kind,char_count FROM source_segments '
                            'WHERE source_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?', (sid, start, PAGE))
        next_start = start + PAGE
        return {**_pin(src), 'segments':[dict(r) for r in rows], 'segments_count':src['segments_count'],
                'next_start':next_start if next_start < src['segments_count'] else None}
    if 'start' in args:
        raise SchemaInvalid('start applies to a listing, while segment_id reads one original paragraph')
    seg = _segment(conn, sid, args['segment_id'])
    receipt = ids.new_id('receipt')
    conn.execute('INSERT INTO source_reads(receipt_id,source_id,segment_id,text_sha256,task_id,lease_id,principal,'
                 'delivered_at) VALUES(?,?,?,?,?,?,?,?)',
                 (receipt, sid, seg['segment_id'], seg['text_sha256'], identity['task_id'], identity['lease_id'],
                  identity['thread_id'], clock.now_iso()))
    return {**_pin(src), 'segment_id':seg['segment_id'], 'label':seg['label'], 'text':seg['text'],
            'text_sha256':seg['text_sha256'], 'receipt_id':receipt, 'scope':identity['scope'],
            'task_id':identity['task_id'], 'lease_id':identity['lease_id'], 'principal':identity['thread_id']}


def _receipt(conn, src, segment_id, receipt_id, identity):
    seg = _segment(conn, src['source_id'], segment_id)
    delivery = conn.execute('SELECT 1 FROM source_reads r JOIN v6_decision_tasks d USING(task_id) '
                            'WHERE r.receipt_id=? AND r.source_id=? AND r.segment_id=? AND r.text_sha256=? '
                            'AND r.task_id=? AND r.lease_id=? AND r.principal=? AND d.run_id=? AND d.scope=?',
                            (receipt_id, src['source_id'], segment_id, seg['text_sha256'], identity['task_id'],
                             identity['lease_id'], identity['thread_id'], identity['run_id'], identity['scope'])).fetchone()
    if not delivery:
        raise ScopeDenied('A read receipt from this task, principal and lease for this exact source paragraph is required')
    return seg


def _save(conn, kind, doc, identity):
    service = ArtifactService(conn)
    digest = ids.sha256_of_obj(doc)
    result = service.publish_bytes(namespace=identity['run_id'] + '/source-evidence', rel_path=digest + '.json',
        kind=kind, data=ids.canonical_json(doc), expected_sha256=digest, media_type='application/json',
        run_id=identity['run_id'], task_id=identity['task_id'], actor=identity['thread_id'], origin='BACKEND')
    service.discard_on_rollback(result)
    event_kind = 'v62.source.reviewed' if kind == REVIEW_RECORD else 'v62.source.extracted'
    store.append_event(conn, run_id=identity['run_id'], kind=event_kind, actor=identity['thread_id'],
        subject_kind='artifact', subject_id=result.artifact_ref, payload={
            'scope':identity['scope'], 'artifact_ref':result.artifact_ref, 'sha256':digest,
            'target_claim_ref':doc.get('target_claim_ref'),
            'evidence_id':doc['evidence_id'], 'review_id':doc.get('review_id'),
            **({'review_turn_id': identity['turn_id']} if kind == REVIEW_RECORD else {})},
        evidence_level='EXECUTION_OBSERVED')
    return result.artifact_ref


def _nature(src, *, reviewed):
    provenance = store.loads(src, 'provenance_json', {})
    if provenance.get('source_kind') == 'pi_material':
        return 'supplied_information'
    role = (provenance.get('role') or '').lower()
    if role in ('fixture', 'test_fixture', 'synthetic_fixture') or role.endswith('_fixture'):
        return 'test_fixture'
    if role in ('engineering_failure', 'execution_failure'):
        return 'engineering_failure'
    if role in ('model_prediction', 'prediction'):
        return 'model_prediction'
    if role in ('module_suggestion', 'module_recommendation'):
        return 'module_suggestion'
    return 'formal_evidence' if reviewed else 'module_suggestion'


def _target(conn, target, identity, cursor, permitted):
    from . import v6_claims
    if not target:
        return None
    record = v6_claims.read_revision(conn, identity['run_id'], target)
    if (not record or not {target, record['claim_revision_id']}.intersection(permitted) or not conn.execute(
            "SELECT 1 FROM events WHERE run_id=? AND kind IN ('v6.claim.adopted','v62.claim.reassessed') "
            "AND subject_kind='hypothesis' AND subject_id=? AND event_id<=? "
            "AND json_extract(payload_json,'$.claim_revision_id')=?", (identity['run_id'], record['hypothesis_id'],
             cursor, record['claim_revision_id'])).fetchone()):
        raise ScopeDenied('The target must be an explicitly assigned, published claim revision in this frozen scope')
    return {k: record[k] for k in ('claim_revision_id', 'statement', 'scope')}


def _extract(conn, src, args, identity, cursor, permitted_targets):
    seg = _receipt(conn, src, args['segment_id'], args['receipt_id'], identity)
    grain = args.get('claim_grain', 'FINDING')
    if ((src['text_coverage'] == 'ABSTRACT_ONLY' or seg['kind'] == 'abstract') and (grain in ('METHOD_DETAIL', 'NUMERIC_VALUE') or
            args.get('method') or args.get('data'))):
        raise SchemaInvalid('Abstract-only coverage cannot substantiate a numerical value or a method detail')
    if re.fullmatch(r'\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*%?\s*', args['confidence_basis'], re.I):
        raise SchemaInvalid('Confidence basis must state scientific grounds for this interpretation')
    quote = args['quote']
    text = seg['text']
    if ('char_start' in args) != ('char_end' in args):
        raise SchemaInvalid('A quoted span supplies both character offsets or omits both')
    start = args.get('char_start', text.find(quote))
    end = args.get('char_end', start + len(quote))
    matched = 0 <= start < end <= len(text) and text[start:end] == quote
    elsewhere = []
    if not matched:
        elsewhere = [r[0] for r in conn.execute('SELECT segment_id FROM source_segments WHERE source_id=? '
                     'AND segment_id<>? AND instr(text,?)>0 ORDER BY ordinal LIMIT ?',
                     (src['source_id'], seg['segment_id'], quote, PAGE))]
    anchor = 'VERBATIM' if matched else 'ELSEWHERE' if elsewhere else 'NOT_ANCHORED'
    status = 'LOCATOR_VERIFIED' if matched else 'LOCATOR_UNRESOLVED'
    target = _target(conn, args.get('target_claim_ref'), identity, cursor, permitted_targets)
    evid = ids.new_id('evidence')
    locator = {'segment_id':seg['segment_id'], 'label':seg['label'], 'text_sha256':seg['text_sha256'],
               'char_start':start if matched or 'char_start' in args else None,
               'char_end':end if matched or 'char_end' in args else None, 'found_in':elsewhere}
    doc = {**_pin(src), **locator, 'schema':'gx.v62.source_extraction.1', 'evidence_id':evid,
           'source_class':'source_extraction', 'record_nature':_nature(src, reviewed=False), 'status':status,
           'anchor_status':anchor, 'direction':args['direction'], 'claim':args['claim'], 'claim_grain':grain,
           'evidence_type':args.get('evidence_type', 'REVIEW'), 'applicability':args['applicability'],
           'limitations':args.get('limitations'), 'target_claim':target,
           'target_claim_ref':target['claim_revision_id'] if target else None, 'method':args.get('method'),
           'data':args.get('data'), 'confidence_basis':args['confidence_basis'], 'quote':quote, 'quote_sha256':_sha(quote),
           'extract_receipt_id':args['receipt_id'], 'extractor':identity}
    ref = _save(conn, EXTRACTION, doc, identity)
    now = clock.now_iso()
    conn.execute('''INSERT INTO evidence(evidence_id,namespace,run_id,status,claim,claim_grain,evidence_type,source_id,
        source_locator_json,quote,quote_sha256,anchor_status,anchored_segment_id,conditions,method,data,limitations,
        review_status,confidence_basis,evidence_level,origin,extracted_task_id,extracted_lease_id,extracted_principal,
        receipt_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (evid, identity['scope'], identity['run_id'], status, doc['claim'], grain, doc['evidence_type'], src['source_id'],
         ids.canonical_json(locator), quote, doc['quote_sha256'], anchor, seg['segment_id'] if matched else None,
         doc['applicability'], doc['method'], doc['data'], doc['limitations'], status, doc['confidence_basis'],
         'SOURCE_VERIFIED' if matched else 'CLIENT_REPORTED', 'GOAL_OFFLINE_SOURCE', identity['task_id'],
         identity['lease_id'], identity['thread_id'], args['receipt_id'], now, now))
    return {'evidence_id':evid, 'artifact_ref':ref, 'evidence_ref':ref, 'accepted':matched, 'status':status,
            'record_nature':doc['record_nature'], 'anchor_status':anchor, 'quote_sha256':doc['quote_sha256'],
            'found_in':elsewhere, 'scope':identity['scope']}


def receipt_turns(conn, run_id, thread, turn):
    """Only acknowledged GX resumptions inherit source receipts on the same native thread."""
    rows = conn.execute("""
        WITH RECURSIVE lineage(turn_id) AS (
            SELECT ?
            UNION
            SELECT prior.turn_id FROM lineage
            JOIN p0_root_deliveries child ON child.run_id=? AND child.thread_id=? AND child.turn_id=lineage.turn_id
            JOIN p0_root_deliveries prior ON prior.run_id=child.run_id AND prior.thread_id=child.thread_id
                AND prior.turn_id=substr(child.event_id,8) AND prior.rowid<child.rowid
            WHERE child.method='turn/start' AND prior.method='turn/start'
                AND child.state='acknowledged' AND prior.state='acknowledged' AND substr(child.event_id,1,7)='resume:'
        ) SELECT turn_id FROM lineage""", (turn,run_id,thread))
    return {row[0] for row in rows}


def decision_citations(conn, task, lease, thread, turn, candidates):
    """Return source records actually delivered by this assignment's native evidence tools."""
    if not candidates:
        return set()
    turns = receipt_turns(conn, task["run_id"], thread, turn)
    rows = conn.execute("""SELECT a.artifact_ref FROM p0_root_calls c
        JOIN p0_v6_native_calls n USING(thread_id,call_id,turn_id)
        JOIN p0_roots r ON r.thread_id=c.thread_id
        JOIN artifacts a ON a.artifact_ref=json_extract(c.result_json,'$.artifact_ref')
        WHERE c.thread_id=? AND c.turn_id IN (SELECT value FROM json_each(?)) AND r.run_id=? AND r.task_id=? AND r.lease_id=?
        AND a.run_id=r.run_id AND a.task_id=r.task_id AND a.published=1
        AND json_extract(c.result_json,'$.accepted')=1
        AND json_extract(c.result_json,'$.scope')=?
        AND ((json_extract(n.request_json,'$.tool')=? AND a.kind=?
              AND a.artifact_ref=json_extract(c.result_json,'$.evidence_ref'))
          OR (json_extract(n.request_json,'$.tool')=? AND a.kind=?
              AND a.artifact_ref=json_extract(c.result_json,'$.review_ref')))
        AND a.artifact_ref IN (SELECT value FROM json_each(?))""",
        (thread, ids.canonical_json(sorted(turns)), task["run_id"], task["task_id"], lease, task["scope"],
         EXTRACT, EXTRACTION, REVIEW, REVIEW_RECORD, ids.canonical_json(sorted(candidates))))
    from . import v62_catalog
    owned = {row[0] for row in rows}
    return owned | v62_catalog._returned_method_citations(conn, task, lease, thread, turns, candidates - owned)


def _owned(conn, run_id, ref, *, cursor=None):
    return conn.execute("""SELECT a.kind,a.sha256,e.event_id,e.payload_json FROM events e JOIN artifacts a
        ON a.run_id=e.run_id AND a.artifact_ref=e.subject_id AND a.published=1
        WHERE e.run_id=? AND e.subject_kind='artifact' AND e.subject_id=? AND
        ((e.kind='v62.source.extracted' AND a.kind=?) OR (e.kind='v62.source.reviewed' AND a.kind=?))
        AND (? IS NULL OR e.event_id<=?)""", (run_id, ref, EXTRACTION, REVIEW_RECORD, cursor, cursor)).fetchone()


def _document(conn, ref, owned):
    doc = ArtifactService(conn).read_json(ref)
    ledger = store.loads(owned, 'payload_json')
    if owned['sha256'] != ledger['sha256'] or doc['evidence_id'] != ledger['evidence_id']:
        raise VersionImmutable('Source-evidence artifact differs from its original Goal receipt')
    return doc


def _publication(conn, run_id, ref, owned, cursor):
    if store.loads(owned, 'payload_json')['scope'] == 'run':
        return owned['event_id']
    # A report's acceptance and its release can occur at different cursors. Only exact
    # source refs actually published in a released report are shared with the Lead.
    row = conn.execute("""SELECT min(w.event_id) FROM v62_module_wakes w JOIN events e USING(event_id)
        JOIN json_each(e.payload_json,'$.report_refs') delivered JOIN v62_module_reports r
        ON r.run_id=w.run_id AND r.report_ref=delivered.value JOIN json_each(r.receipt_json,'$.source_refs') cited
        WHERE w.run_id=? AND cited.value=? AND w.event_id<=?""", (run_id, ref, cursor)).fetchone()
    return row[0]


def _scope_grants(conn, run_id, scope, ref, cursor):
    if scope == 'run':
        return True
    if scope.startswith('module:'):
        row = conn.execute("SELECT module_instance_id,grant_json,run_id FROM v62_modules WHERE run_id=? AND scope=?",
                           (run_id, scope)).fetchone()
        if row is None:
            return False
        if ref in store.loads(row, 'grant_json')['input_refs']:
            return True
        from . import v621_assignment
        return any(m["artifact_ref"] == ref for m in v621_assignment.for_module(conn, row, cursor=cursor))
    return conn.execute("""SELECT 1 FROM (
        SELECT d.visible_refs_json FROM v6_decision_tasks d JOIN research_contexts c USING(context_ref)
        WHERE d.run_id=? AND d.scope=? AND c.event_cursor<=? ORDER BY c.event_cursor DESC,d.rowid DESC LIMIT 1
        ) latest JOIN json_each(latest.visible_refs_json) i WHERE i.value=?""", (run_id, scope, cursor, ref)).fetchone() is not None


def _independent(conn, author, reviewer):
    from ..governance import reviews
    if any(author[k] == reviewer[k] for k in ('task_id', 'lease_id', 'thread_id', 'context_ref')):
        raise ScopeDenied('Independent review needs a different task, lease, principal and frozen context')
    proof = reviews.explain_independence(conn, author_task_id=author['task_id'], author_lease_id=author['lease_id'],
        author_principal=author['thread_id'], reviewer_task_id=reviewer['task_id'], reviewer_lease_id=reviewer['lease_id'])
    if proof['level'] == reviews.CLIENT_REPORTED_ONLY:
        # Goal Roots bind only official, non-forked threads to separate leases. Older
        # governance adapters do not project that P0 host receipt into worker_observations.
        # Require both exact original bindings and acknowledged native turn deliveries.
        for who in (author, reviewer):
            row = conn.execute("""SELECT 1 FROM p0_roots r JOIN leases l USING(lease_id,task_id)
                JOIN p0_root_deliveries d ON d.run_id=r.run_id AND d.thread_id=r.thread_id
                WHERE r.run_id=? AND r.task_id=? AND r.lease_id=? AND r.thread_id=? AND r.thread_id=l.principal
                AND d.turn_id=? AND d.method='turn/start' AND d.state='acknowledged'""",
                tuple(who[k] for k in ('run_id','task_id','lease_id','thread_id','turn_id'))).fetchone()
            if not row:
                raise ScopeDenied('Independent review requires the two original native worker bindings')
        proof['level'] = reviews.FRESH_CONTEXT_SAME_HOST
        proof['why'] = 'Distinct native Goal Roots, leased tasks and acknowledged turns in independent frozen contexts'
    if proof['level'] not in contracts.sufficient_independence():
        raise ScopeDenied('Source review did not establish an independent worker: ' + proof['why'])
    return proof


def _review(conn, src, extract, args, identity):
    if extract['status'] != 'LOCATOR_VERIFIED':
        raise SchemaInvalid('A source extraction with an unresolved quote cannot become reviewed evidence')
    seg = _receipt(conn, src, extract['segment_id'], args['receipt_id'], identity)
    start, end, quote = extract['char_start'], extract['char_end'], extract['quote']
    if (seg['text_sha256'] != extract['text_sha256'] or seg['text'][start:end] != quote
            or _sha(quote) != args['quote_sha256_seen'] or args['quote_sha256_seen'] != extract['quote_sha256']):
        raise SchemaInvalid('The reviewer must identify the exact quote delivered in the original source paragraph')
    proof = _independent(conn, extract['extractor'], identity)
    review_id = ids.new_id('review')
    verdict = args['verdict']
    status = 'REVIEWED' if verdict == 'SUPPORTED' else 'DISPUTED'
    doc = {**extract, 'schema':'gx.v62.evidence_review.1', 'source_class':'reviewed_source_evidence',
           'record_nature':_nature(src, reviewed=True), 'evidence_ref':args['evidence_ref'], 'review_id':review_id,
           'status':status, 'verdict':verdict, 'note':args['note'], 'review_receipt_id':args['receipt_id'],
           'reviewer':identity, 'independence':proof}
    ref = _save(conn, REVIEW_RECORD, doc, identity)
    obs = proof['reviewer'].get('observation_ids') or [None]
    now = clock.now_iso()
    conn.execute('''INSERT INTO evidence_reviews(review_id,evidence_id,task_id,lease_id,principal,worker_observation_id,
        independence_level,verdict,receipt_id,quote_sha256_seen,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (review_id, extract['evidence_id'], identity['task_id'], identity['lease_id'], identity['thread_id'], obs[-1],
         proof['level'], verdict, args['receipt_id'], args['quote_sha256_seen'], args['note'], now))
    # Keep each judgment immutable. The compatibility status cannot silently cancel a dispute.
    conn.execute("UPDATE evidence SET status=CASE WHEN status='DISPUTED' THEN status ELSE ? END, "
                 "review_status=CASE WHEN status='DISPUTED' THEN status ELSE ? END, updated_at=? WHERE evidence_id=?",
                 (status, status, now, extract['evidence_id']))
    return {'review_ref':ref, 'artifact_ref':ref, 'evidence_ref':args['evidence_ref'], 'review_id':review_id,
            'accepted':True, 'status':status, 'verdict':verdict, 'direction':doc['direction'],
            'record_nature':doc['record_nature'], 'scope':identity['scope']}


def call_tool(conn, tool, payload, *, run_id, task_id, lease_id, thread_id, turn_id, call_id):
    """Act only for the live, frozen Goal assignment. The transport owns callback identity/replay."""
    from ..errors import OutcomeUnknown
    if tool not in TOOL_NAMES:
        raise SchemaInvalid('Unknown source-evidence tool')
    from . import harness_policy, research_handoff
    searchable = isinstance(payload, dict) and 'query' in payload and research_handoff.active(conn, run_id)
    errors = schema.validate(payload, source_spec(tool, searchable=searchable)['inputSchema'])
    if errors:
        raise SchemaInvalid(', '.join(errors[:4]))
    if any(isinstance(v, str) and not v.strip() or type(v) is int and v < 0 for v in payload.values()):
        raise SchemaInvalid('Source text and references must be nonempty, and indices nonnegative')
    native = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, thread_id=thread_id, turn_id=turn_id)
    with store.transaction(conn, transaction_owner='GoalSourceEvidence', operation='source_tool', run_id=run_id):
        root, task, _, _ = v6_decisions.authority(conn, **native)
        if tool not in json.loads(root['tool_names_json'] or '[]'):
            raise ScopeDenied('This scientific assignment has no grant for the requested source tool')
        scope = task['scope']
        packet = store.loads(task, 'snapshot_json')
        visible = set(json.loads(task['visible_refs_json']))
        permitted_targets = visible
        module = None
        if scope.startswith('module:'):
            # authority already checked the active module, goal and material pins inside this writer.
            module = conn.execute('SELECT module_instance_id,grant_json FROM v62_modules WHERE run_id=? AND scope=?',
                                  (run_id, scope)).fetchone()
            grant = json.loads(module['grant_json'])
            if tool not in grant['tools'] or tool not in packet['allowed_tools']:
                raise ScopeDenied('This tool is outside the active module grant')
            visible &= {m['artifact_ref'] for m in packet['materials']}
            permitted_targets = set(grant.get('target_claim_refs', ()))
        elif (scope != 'run' or tool not in packet.get('source_tools', ())
              or (packet.get('research_effort') != 'deep' and not harness_policy.enabled(packet))):
            raise ScopeDenied('This decision has no delegated source evidence tool')
        extract = None
        if tool == REVIEW:
            eref = payload['evidence_ref']
            owned = _owned(conn, run_id, eref)
            if not owned or owned['kind'] != EXTRACTION:
                raise ScopeDenied('The referenced extraction is unavailable in this scientific scope')
            origin_scope = store.loads(owned, 'payload_json')['scope']
            if origin_scope != scope and (eref not in visible or not _publication(conn, run_id, eref, owned, task['event_cursor'])):
                raise ScopeDenied('Cross-scope review requires an explicitly delegated, released source extraction')
            extract = _document(conn, eref, owned)
            sref = extract['source_ref']
        else:
            sref = payload['source_ref']
        if sref not in visible:
            raise ScopeDenied('The original source is outside the frozen inputs of this assignment')
        identity = dict(**native, call_id=call_id, scope=scope, context_ref=task['context_ref'])
        # Repeated delivery in a later turn of this same lease can use the same receipt.
        # A new lease always receives its own copy and cannot inherit another worker's read.
        operation = ids.sha256_of_obj([run_id, scope, task_id, lease_id, thread_id, tool, payload])
        key = 'v62.source:' + operation
        idem = store.begin_idempotent(conn, key, 'v62.source', {'tool':tool, 'arguments':payload})
        if idem.replay:
            return idem.response
        if not idem.fresh:
            raise OutcomeUnknown('The original scoped evidence operation is awaiting reconciliation')
        src = _source(conn, run_id, sref, cursor=task['event_cursor'], verify=tool == READ and 'segment_id' in payload)
        if tool == READ:
            answer = _read(conn, src, payload, identity)
        elif tool == EXTRACT:
            answer = _extract(conn, src, payload, identity, task['event_cursor'], permitted_targets)
        else:
            answer = _review(conn, src, extract, payload, identity)
        if module:
            sources = [sref] if tool != READ or 'receipt_id' in answer else []
            if extract:
                sources.append(payload['evidence_ref'])
            artifacts = [answer['artifact_ref']] if 'artifact_ref' in answer else []
            assets = ArtifactService(conn)
            pins = {ref: assets.describe(ref)['sha256'] for ref in sources + artifacts}
            receipt = {**identity, 'module_instance_id':module['module_instance_id'], 'operation_key':operation,
                       'tool':tool, 'arguments_sha256':ids.sha256_of_obj(payload), 'artifact_refs':artifacts,
                       'source_refs':sources, 'source_sha256':src['blob_sha256'], 'artifact_sha256':pins,
                       'result_sha256':ids.sha256_of_obj(answer)}
            if 'receipt_id' in answer:
                receipt['receipt_id'] = answer['receipt_id']
                receipt['segment_id'] = answer['segment_id']
            store.append_event(conn, run_id=run_id, kind='v62.module.tool_completed', actor=thread_id,
                subject_kind='module', subject_id=module['module_instance_id'], payload=receipt,
                evidence_level='EXECUTION_OBSERVED')
        store.complete_idempotent(conn, key, answer)
        return answer


def claim_ground(conn, run_id, ref, decision_scope, event_cursor):
    """Resolve a source record as of a frozen cursor, including the original and both signed reads."""
    owned = _owned(conn, run_id, ref, cursor=event_cursor)
    if not owned:
        return None
    ledger = store.loads(owned, 'payload_json')
    publication = _publication(conn, run_id, ref, owned, event_cursor)
    if ledger['scope'] != decision_scope and (not publication or not _scope_grants(conn, run_id, decision_scope, ref, event_cursor)):
        return None
    doc = _document(conn, ref, owned)
    source = conn.execute('SELECT * FROM sources WHERE source_id=? AND namespace=? AND version=? AND blob_sha256=? '
                          'AND blob_artifact_ref=?', (doc['source_id'], run_id, doc['source_version'], doc['source_sha256'],
                          doc['source_ref'])).fetchone()
    if not source or source['status'] != 'PARSED' or source['retraction_status'] == 'RETRACTED':
        raise VersionImmutable('The registered evidence source was withdrawn or changed')
    seg = _receipt(conn, source, doc['segment_id'], doc['extract_receipt_id'], doc['extractor'])
    if seg['text_sha256'] != doc['text_sha256'] or _sha(doc['quote']) != doc['quote_sha256']:
        raise VersionImmutable('The immutable evidence locator does not match the registered paragraph')
    if doc['anchor_status'] == 'VERBATIM' and seg['text'][doc['char_start']:doc['char_end']] != doc['quote']:
        raise VersionImmutable('The recorded quotation differs from the registered source span')
    if owned['kind'] == REVIEW_RECORD:
        _receipt(conn, source, doc['segment_id'], doc['review_receipt_id'], doc['reviewer'])
        review = conn.execute('SELECT 1 FROM evidence_reviews WHERE review_id=? AND evidence_id=? AND receipt_id=? '
            'AND verdict=? AND quote_sha256_seen=? AND independence_level=?', (doc['review_id'], doc['evidence_id'],
            doc['review_receipt_id'], doc['verdict'], doc['quote_sha256'], doc['independence']['level'])).fetchone()
        if not review or doc['independence']['level'] not in contracts.sufficient_independence():
            raise VersionImmutable('The evidence review differs from its independent signed judgment')
    return {**doc, 'ref':ref, 'artifact_ref':ref, 'kind':owned['kind'], 'sha256':owned['sha256'],
            'event_id':owned['event_id'], 'publication_event_id':publication, 'scope':ledger['scope'],
            'evidence_ref':doc.get('evidence_ref', ref), 'review_ref':ref if owned['kind'] == REVIEW_RECORD else None}


def report_quotation(conn, run_id, scope, finding, reads):
    """Recognize exact source text already delivered to this module and cited in this finding."""
    refs = set(finding['source_refs'])
    text = finding['text']
    extracted = refs.intersection(a for read in reads if read['tool'] in (EXTRACT, REVIEW)
                                 for a in read.get('artifact_refs', ()))
    for op in reads:
        if op['tool'] != READ or not op.get('receipt_id') or not refs.intersection(op['source_refs']):
            continue
        original = op['source_refs'][0]
        src = _source(conn, run_id, original)
        seg = _receipt(conn, src, op['segment_id'], op['receipt_id'], op)
        base = dict(schema='gx.source_quotation/1', **_pin(src), segment_id=seg['segment_id'],
                    text_sha256=seg['text_sha256'], receipt_id=op['receipt_id'], record_nature=_nature(src, reviewed=False))
        if text == seg['text']:
            return dict(**base, quote_sha256=seg['text_sha256'], source_span=[0,len(text)], finding_span=[0,len(text)])
        for ref in sorted(extracted):
            owned = _owned(conn, run_id, ref)
            if not owned:
                continue
            doc = _document(conn, ref, owned)
            if (doc['anchor_status'] != 'VERBATIM' or doc['source_ref'] != original
                    or doc['extract_receipt_id'] != op['receipt_id'] or doc['text_sha256'] != seg['text_sha256']):
                continue
            start, end, quote = doc['char_start'], doc['char_end'], doc['quote']
            if seg['text'][start:end] != quote or not quote or _sha(quote) != doc['quote_sha256']:
                continue
            idx = text.find(quote)
            if idx < 0:
                continue
            # An inline numeric quote needs explicit attribution. A different literal in its surrounding
            # interpretation remains subject to the usual metric citation requirement.
            if text != quote and not (re.search(
                    r'(?:^|[.!?]\s+)(?:the\s+)?(?:[\w-]+\s+){0,4}'
                    r'(?:note|source|paper|study|record|article|report)\s+(?:reports?|states?|quotes?)\s+["“]?$',
                    text[:idx], flags=re.I) and re.match(r'["”]?(?:[.,;:!?]|$)',text[idx+len(quote):])):
                continue
            return dict(**base, evidence_ref=ref, evidence_sha256=owned['sha256'], quote_sha256=doc['quote_sha256'],
                        source_span=[start,end], finding_span=[idx,idx+len(quote)])
    return None


def _published_matches(conn, run_id, scope, cursor, locator, value):
    refs = conn.execute("SELECT subject_id FROM events WHERE run_id=? AND kind='v62.source.reviewed' AND event_id<=? "
        "AND json_extract(payload_json,?)=? ORDER BY event_id", (run_id, cursor, locator, value))
    published = []
    for row in refs:
        fact = claim_ground(conn, run_id, row['subject_id'], scope, cursor)
        if fact and fact['publication_event_id'] is not None:
            published.append(fact)
    return published


def published_reviews(conn, run_id, scope, cursor, target_claim_ref):
    """Return judgments of the exact targeted claim revision released through this cursor."""
    from .v6_claims import read_revision
    prior = read_revision(conn, run_id, target_claim_ref)
    return _published_matches(conn, run_id, scope, cursor, '$.target_claim_ref', prior['claim_revision_id']) if prior else []


def related_reviews(conn, run_id, scope, cursor, evidence_ref):
    """Preserve later published disagreements about the same original extraction."""
    owned = _owned(conn, run_id, evidence_ref, cursor=cursor)
    if not owned or owned['kind'] != EXTRACTION:
        return []
    evid = store.loads(owned, 'payload_json')['evidence_id']
    return _published_matches(conn, run_id, scope, cursor, '$.evidence_id', evid)
