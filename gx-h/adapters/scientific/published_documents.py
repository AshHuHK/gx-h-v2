"""Offline readers for publisher documents and accessioned, immutable database records."""
from datetime import datetime
from html.parser import HTMLParser
from io import BytesIO
import json
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from gx_core.errors import SchemaInvalid


def jats_article(raw):
    if b'<!ENTITY' in raw.replace(b'\0', b'').upper():
        raise SchemaInvalid('Offline JATS sources cannot include entity declarations')
    try:
        article = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError, LookupError) as exc:
        raise SchemaInvalid('The registered JATS document is not readable XML') from exc
    if article.tag.rsplit('}', 1)[-1] != 'article':
        raise SchemaInvalid('Offline XML must be a JATS article')
    return article


def _article_ids(article):
    # References and sub-articles frequently identify *different* publications.
    front = article.find('{*}front/{*}article-meta')
    found = set()
    if front is None:
        return found
    for field in front.findall('{*}article-id'):
        kind = field.get('pub-id-type', '').lower()
        value = ''.join(field.itertext()).strip().casefold()
        if kind in ('pmc', 'pmcid') and re.fullmatch(r'(?:pmc)?[0-9]+', value):
            found.add('pmc' + value.removeprefix('pmc'))
        elif kind in ('doi', 'arxiv', 'pmid'):
            found.add(value)
    return found


def identity(meta, raw, fmt, digest):
    """Verify the bytes of this named publication, preserving provider revision vs byte snapshot."""
    if not isinstance(meta, dict):
        raise SchemaInvalid('An external source requires a typed acquisition descriptor')
    fields = ('kind', 'accession', 'version', 'url', 'retrieved_at')
    if any(not isinstance(meta.get(k), str) or not meta[k].strip() for k in fields):
        raise SchemaInvalid('External source requires kind, accession, version, retrieval time and URL')
    source = {k: meta[k] for k in fields}
    endpoint = urlsplit(source['url'])
    if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password:
        raise SchemaInvalid('An acquisition must identify its public HTTPS retrieval URL')
    try:
        date = datetime.fromisoformat(source['retrieved_at'].replace('Z', '+00:00'))
        if date.utcoffset() is None:
            raise ValueError()
    except ValueError as exc:
        raise SchemaInvalid('An acquisition time requires an ISO date and time zone') from exc
    pin = 'sha256:' + digest
    accession = source['accession']
    kind = source['kind']
    if kind == 'article' and fmt == 'jats':
        article = jats_article(raw)
        if accession.casefold() not in _article_ids(article):
            raise SchemaInvalid('The publisher article metadata does not identify the requested accession')
        version = article.find('{*}front/{*}article-meta/{*}article-version')
        revision = ''.join(version.itertext()).strip() if version is not None else None
        if source['version'] not in (pin, revision):
            raise SchemaInvalid('The article revision is absent or differs; use its SHA-256 snapshot version')
        status = 'matched_accession'
    elif kind == 'database_record' and fmt == 'json':
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SchemaInvalid('Expected a JSON database entry') from exc
        if not isinstance(record, dict) or record.get('primaryAccession') != accession:
            raise SchemaInvalid('The database record does not identify the requested primary accession')
        audit = record.get('entryAudit')
        revision = audit.get('entryVersion') if isinstance(audit, dict) else None
        if type(revision) not in (int, str) or not str(revision).strip() or source['version'] != str(revision):
            raise SchemaInvalid('The requested entry revision differs from the deposited record')
        status = 'matched_accession'
    elif kind == 'article' and fmt in ('pdf', 'html', 'text', 'markdown'):
        # These formats lack a uniform publisher ID. Keep the retrieval claim distinct
        # from an accession verified inside JATS front matter.
        if source['version'] != pin:
            raise SchemaInvalid('This document requires a byte-pinned snapshot version')
        status = 'snapshot_only'
    else:
        raise SchemaInvalid('The external source kind and original file format are incompatible')
    return {'schema': 'gx.source_identity/1', 'artifact_sha256': digest, 'status': status,
            'external_source': source}


def json_blocks(raw):
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SchemaInvalid('The registered data record must be JSON') from exc
    if not isinstance(record, (list, dict)):
        raise SchemaInvalid('Database source must be a JSON object or array of rows')
    blocks = []
    def emit(pointer, value):
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        blocks.append(('database_field', pointer, text))
    for key, value in (record.items() if isinstance(record, dict) else enumerate(record)):
        pointer = '/' + str(key).replace('~', '~0').replace('/', '~1')
        if isinstance(value, list) and value:
            for idx, item in enumerate(value):
                emit(pointer + '/' + str(idx), item)
        else:
            emit(pointer, value)
    return 'OFFICIAL_DOC_ONLY', blocks, {'locator_format': 'JSON Pointer', 'rendering': 'JSON field values'}


def pdf_blocks(raw):
    if not raw.lstrip().startswith(b'%PDF-'):
        raise SchemaInvalid('The registered PDF is missing its file signature')
    try:
        import pypdf
        from pypdf.errors import PyPdfError
    except ImportError as exc:
        raise SchemaInvalid('Install the pinned PDF reader from release-assets/python/requirements.lock') from exc
    blocks = []
    extracted = []
    unreadable = []
    try:
        doc = pypdf.PdfReader(BytesIO(raw))
        if doc.is_encrypted:
            raise SchemaInvalid('The registered PDF is encrypted')
        total = len(doc.pages)
        labels = doc.page_labels
        for number, page in enumerate(doc.pages, 1):
            try:
                text = (page.extract_text() or '').strip()
            except (PyPdfError, ValueError, NotImplementedError) as exc:
                unreadable.append({'page': number, 'error': type(exc).__name__})
                continue
            if text:
                extracted.append(number)
                label = labels[number - 1]
                locator = f'page {number}'
                if label != str(number):
                    locator += f' [{label}]'
                blocks.append(('page_text', locator, text))
    except (PyPdfError, ValueError, RecursionError) as exc:
        raise SchemaInvalid('The registered PDF could not be parsed') from exc
    return ('PARTIAL_TEXT' if extracted else 'UNAVAILABLE'), blocks, {
        'extractor': 'pypdf ' + pypdf.__version__, 'page_count': total, 'pages_with_text': extracted,
        'page_errors': unreadable,
        'scope': 'Embedded text only. Figures, reading order, table layout and image-only pages require inspection.'}


class _HTMLText(HTMLParser):
    containers = {'article', 'main', 'section', 'div', 'table'}
    blocks = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'td', 'th', 'li', 'figcaption'}
    void = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    ignored = {'head', 'script', 'style', 'template', 'nav', 'footer', 'aside', 'noscript'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.pieces = []
        self.active = None
        self.results = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag in self.void:
            if tag == 'br' and self.active:
                self.pieces.append('\n')
            return
        # Common publisher HTML omits optional closing tags for successive cells/paragraphs.
        if self.stack and tag in {'p', 'td', 'th', 'li'} and self.stack[-1][0] == tag:
            self.handle_endtag(tag)
        labels = [fields.get(k) or '' for k in ('id', 'class', 'role', 'aria-label')]
        parent = self.stack[-1] if self.stack else ('', '', '', False)
        hidden = parent[3] or tag in self.ignored or 'hidden' in fields or fields.get('aria-hidden') == 'true'
        region = parent[1]
        if tag in ('main','article'):
            region = 'body'
        if tag in self.containers and any('abstract' in x.lower().split() for x in labels):
            region = 'abstract'
        section = parent[2]
        if (tag in self.containers or tag in self.blocks) and fields.get('id'):
            section = fields['id']
        self.stack.append((tag, region, section, hidden))
        if not hidden and tag in self.blocks and self.active is None:
            self.active = (len(self.stack), region or 'text', section, tag)
            self.pieces = []

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                if self.active and self.active[0] > i:
                    _, region, section, block = self.active
                    text = ''.join(self.pieces).strip()
                    if text:
                        locator = '/'.join(filter(None, (region, section, f'{block} [{len(self.results)+1}]')))
                        self.results.append((region, locator, text))
                    self.active = None
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.active and not self.stack[-1][3]:
            self.pieces.append(data)


def html_blocks(raw):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise SchemaInvalid('Registered publisher HTML must use UTF-8') from exc
    parser = _HTMLText()
    parser.feed(text)
    parser.close()
    if parser.stack:
        parser.handle_endtag(parser.stack[0][0])
    kinds = {kind for kind, _, _ in parser.results}
    coverage = 'ABSTRACT_ONLY' if kinds == {'abstract'} else 'PARTIAL_TEXT' if kinds else 'UNAVAILABLE'
    return coverage, parser.results, {'scope': 'Visible paragraphs, headings and table cells; scripts and figures excluded.'}
