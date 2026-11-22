"""Pinned Codex notifications with streamed Responses text, real tool callbacks and rate-limit headers."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import time

ACCEPTANCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACCEPTANCE))
from acc1_protocol import MeterProbe, binary_facts, fixture
from native_probe import DYN_SPEC, INIT, find_binary, _proxy_preflight
from native_probe_peer import DYNAMIC, Handler, Peer, tool

TEXT_DELTA = 'item/agentMessage/delta'
USAGE = {'input_tokens': 30, 'input_tokens_details': {'cached_tokens': 11},
         'output_tokens': 5, 'output_tokens_details': {'reasoning_tokens': 2}, 'total_tokens': 35}


class StreamingResponses(Handler):
    def do_POST(self):
        if self.path != '/v1/responses':
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        row, index = self.server.record(body)
        ident = row['id']
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('x-codex-primary-used-percent', str(self.server.used_percent))
        self.send_header('x-codex-primary-window-minutes', '15')
        self.send_header('x-codex-primary-reset-at',str(self.server.reset_at))
        self.end_headers()
        def send(event):
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()
        try:
            send({'type':'response.created','response':{'id':ident}})
            if index == 1:
                item = tool(DYNAMIC, {}, ident)
            else:
                item = self.server.message(ident, 'One two three.')
                send({'type':'response.output_item.added','item':self.server.message(ident, '')})
                for chunk in ('One ', 'two ', 'three.'):
                    send({'type':'response.output_text.delta','delta':chunk})
            send({'type':'response.output_item.done','item':item})
            send({'type':'response.completed','response':{'id':ident,'usage':USAGE}})
        except (BrokenPipeError, ConnectionResetError):
            pass


class RatePeer(Peer):
    def __init__(self):
        super().__init__()
        self.RequestHandlerClass = StreamingResponses
        self.used_percent = 12
        self.reset_at = int(time.time()) + 60


class EventProbe(MeterProbe):
    def notification(self, frame):
        super().notification(frame)
        if frame.message['method'] == 'account/rateLimits/updated':
            self.log[-1]['rateLimits'] = frame.message['params']['rateLimits']


def native_reference(binary, disabled):
    from unittest.mock import patch
    with fixture(binary, peer_type=RatePeer, probe_type=EventProbe) as p:
        init = {**INIT,'capabilities':{'experimentalApi':True, 'optOutNotificationMethods':disabled}}
        # Only the acceptance client's initialization document changes between the paired runs.
        with patch('native_probe.INIT',init):
            p.connect()
        root = p.thread(experimentalRawEvents=True)
        turn = p.turn(root,'Call the local arithmetic check and report the result.')
        history = p.rpc('thread/read',{'threadId':root,'includeTurns':True})['thread']
        counts = Counter(r['method'] for r in p.log if r['direction']=='notification')
        limits = [r['rateLimits'] for r in p.log if 'rateLimits' in r]
        units = [r['tokenUsage'] for r in p.log if 'tokenUsage' in r]
        account_keys = sorted({k for r in p.log if 'rateLimits' in r for k in r if k in ('turn','thread')})
        return {'init':init['capabilities'],'root':root,'turn':turn,'notification_counts':dict(counts),
                'tool_callbacks':p.callbacks,'account_notification_scope_fields':account_keys,
                'rate_limit_snapshots':limits,'usage_snapshots':units,
                'history':[(t['id'],t['status']) for t in history['turns']],
                'model_calls':len(p.peer.rows)}


def validate_schema(additional=()):
    root = ACCEPTANCE.parent
    lock = json.loads((root/'release-assets/lock.json').read_text())['experimental_schema']
    archive = root/'release-assets'/lock['file']
    with archive.open('rb') as stream:
        assert hashlib.file_digest(stream,'sha256').hexdigest()==lock['sha256']
    with tarfile.open(archive) as tar:
        capabilities = tar.extractfile('./InitializeCapabilities.ts').read().decode()
        methods = tar.extractfile('./ServerNotification.ts').read().decode()
    assert 'optOutNotificationMethods' in capabilities
    for name in (TEXT_DELTA,'account/rateLimits/updated','thread/tokenUsage/updated','turn/completed',*additional):
        assert name in methods, name
    return lock['sha256']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    control=native_reference(binary,[])
    opted=native_reference(binary,[TEXT_DELTA])
    for obs in (control,opted):
        assert obs['history']==[(obs['turn'],'completed')]
        assert len(obs['tool_callbacks'])==1 and obs['tool_callbacks'][0]['thread']==obs['root']
        assert obs['usage_snapshots'][-1]['total']['totalTokens']==70
        assert obs['usage_snapshots'][-1]['total']['cachedInputTokens']==22
        assert obs['usage_snapshots'][-1]['total']['reasoningOutputTokens']==4
        assert obs['rate_limit_snapshots'][-1]['primary']['usedPercent']==12
        assert obs['notification_counts']['rawResponse/completed']==2
        assert obs['model_calls']==2 and not obs['account_notification_scope_fields']
    assert control['notification_counts'].get(TEXT_DELTA,0)>0 and opted['notification_counts'].get(TEXT_DELTA,0)==0
    report={'scope':'official stdio, deterministic loopback Responses; instrumented native reference client',
            **binary_facts(binary),'schema_archive_sha256':validate_schema(),
            'ordinary_initialize':control,'opted_out':opted,'result':'PASS'}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2)+'\n')
    print('PASS',control['notification_counts'][TEXT_DELTA],'deltas -> 0, two paid responses with cached input per sample')


if __name__=='__main__':
    main()
