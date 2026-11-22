"""Replay only the paid pair's grid-tool sequence while preserving the fixed scientific judge product."""
import hashlib
import json

TOOL='gx_grid_measurement'


class PaidMeasurementSequence:
    def __init__(self, filename, peer):
        self.peer=peer
        original=json.loads(filename.read_bytes())
        self.actions={(v['lane'],v['role'],v['attempt']):v['measurements'] for v in original['ordered_worker_actions']}

    def _context(self,body,brief):
        artifacts,submitted=self.peer.tool_history(body['input']);task=brief['input']
        proposal=artifacts.read_json(task['proposal_ref']);paths=[p['id'] for p in proposal['paths']];lane=paths[0].split('-',1)[0]
        return artifacts,submitted,lane

    def substitute(self,body,request_id,brief,item,info):
        if item.get('name')!='gx_submit_product' and item['type']!='message':return item,info
        if not any(t.get('name')==TOOL for group in body.get('tools',[]) for t in (group.get('tools') or [group])):return item,info
        _,submitted,lane=self._context(body,brief)
        expected=self.actions[lane,brief['role'],brief['attempt']]
        requests={m['call_id']:json.loads(m['arguments']) for m in body['input'] if m.get('type')=='function_call' and m.get('name')==TOOL}
        observed=[]
        for m in body['input']:
            if m.get('type')!='function_call_output' or m.get('call_id') not in requests:continue
            reply=self.peer.output_json(m['output']);observed.append((requests[m['call_id']]['path_id'],not(bool(reply.get('error')))))
        if observed!=[(e['path_id'],e['success']) for e in expected[:len(observed)]]:
            raise ValueError('real plugin reply differs from the observed paid treatment sequence')
        phase='after_submit' if submitted else 'before_submit'
        if len(observed)==len(expected) or expected[len(observed)]['phase']!=phase:return item,info
        nxt=expected[len(observed)];detail={**info,'tool':TOOL,'replayed_phase':phase,'expected_success':nxt['success']}
        return {'type':'function_call','name':TOOL,'call_id':'call-'+request_id,'arguments':json.dumps({'path_id':nxt['path_id']})},detail

    def portable_product(self,body,brief,item):
        artifacts,_,lane=self._context(body,brief);task=brief['input'];mapping={task['proposal_ref']:'artifact:proposal'}
        if task.get('assessment_ref'):mapping[task['assessment_ref']]='artifact:accepted_assessment'
        for i,ref in enumerate(task['evidence_refs']):
            mapping[ref]=f'artifact:evidence:{i}'
            for route in artifacts.read_json(ref)['routes']:
                for field in ('input_ref','observations_ref','evaluation_ref','job_id','eval_id'):
                    if route.get(field):mapping[route[field]]=f"{field}:{route['path_id']}"
        def normalize(value):
            if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
            if isinstance(value,list):return [normalize(v) for v in value]
            if isinstance(value,str):return mapping.get(value,value)
            return value
        product=normalize(json.loads(item['arguments'])['product']);data=json.dumps(product,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        return {'lane':lane,'portable_product_sha256':hashlib.sha256(data).hexdigest()}
