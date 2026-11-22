"""Local scripted Responses partner for a complementary domain control.

It only sees the actual Responses request and replies to the official Codex
tool calls. All coordinate processing and validation run in the public GX CLI.
"""
import json

from e2e_responses import brief_from, message_texts, output_json


class NeedTool(Exception):
    def __init__(self, name, args):
        self.name, self.arguments = name, args


def peer_action(body, ident, *, finish_if_stable=False):
    offered = {entry.get('name') for group in body.get('tools', [])
               for entry in (group.get('tools') or [group])}

    def message(text, info):
        return ({'type': 'message', 'id': 'm-' + ident, 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': text}]}, info)

    if 'gx_submit_product' not in offered:
        return message('Received the bounded structural-coordinate study.', {'actor': 'main'})
    brief = brief_from(body)
    task = brief['input']
    info = {'task_id': brief['task_id'], 'output_kind': task['output_kind'], 'round': task['round']}
    calls = {item['call_id']: item for item in body['input'] if item.get('type') == 'function_call'}
    replies = {}
    for item in body['input']:
        if item.get('type') != 'function_call_output' or item.get('call_id') not in calls:
            continue
        call = calls[item['call_id']]
        name, args = call['name'], json.loads(call['arguments'])
        reply = output_json(item['output'])
        if isinstance(reply, dict) and reply.get('error'):
            raise ValueError(f'{name} rejected: {reply["error"]}')
        if name == 'gx_submit_product':
            return message('Submitted the scientific product to GX.', info)
        replies[name, json.dumps(args, sort_keys=True)] = reply

    def get(name, **args):
        key = (name, json.dumps(args, sort_keys=True))
        if key not in replies:
            raise NeedTool(name, args)
        return replies[key]

    def read(ref):
        return get('gx_read_artifact', ref=ref)

    try:
        stage = task['output_kind']
        if stage == 'geometry_plan':
            quality = read(task['quality_summary_ref'])
            guide = quality.get('source_guide')
            if guide:
                locator = next(loc for loc in guide['locators'] if 'ATOM ' in loc['excerpt'])
                page = get('gx_read_artifact', ref=guide['structure_ref'], offset=locator['offset'])
                if not page['content'].startswith(locator['excerpt']) or page['sha256'] != guide['sha256']:
                    raise ValueError('The source locator does not lead to the original coordinate excerpt')
            # Prospectively accept exploratory subsets with >=40 positions and 25% observed-union coverage.
            # Both constraints are written into the plan before any geometry observations.
            criteria = dict(task['criteria_template'])
            criteria['coverage'] = {**criteria['coverage'], 'min_residues': 40, 'min_fraction': 0.25}
            product = {
                'source_ref': task['structure_ref'], 'protocols': ['common_backbone', 'common_ca'],
                'rationale': f'The coordinate inventory covers {quality["mapping"]["observed_union_residues"]} '
                    'observed-union positions. First compare two common-atom definitions. Retain at least 40 distinct '
                    'positions and 25% overlap for each exploratory fit. This prevents a tiny local patch dominating '
                    'the comparison. Keep the stated angle and RMSD bounds for all selections and require numerical '
                    'reproduction at the stricter frozen tolerances.',
                'hypotheses': [
                    {'id': 'stable', 'expected': 'stable',
                     'prediction': 'All three pairs stay within both prospective tolerances across measured masks.'},
                    {'id': 'sensitive', 'expected': 'sensitive',
                     'prediction': 'At least one fitted pair exceeds an angle or RMSD tolerance across measured masks.'}],
                'criteria': criteria}
        else:
            evidence, plan = read(task['evidence_ref']), read(task['proposal_ref'])
            status = evidence['comparison']['status']
            if stage == 'geometry_assessment':
                checks = {}
                for route in evidence['routes']:
                    if route.get('observations_ref'):
                        read(route['observations_ref'])
                        reproduced = get('gx_recalculate_geometry', protocol=route['protocol'])
                        if reproduced.get('waiting'):
                            job_id = reproduced['job_id']
                            parked = get('gx_wait_job', job_id=job_id)
                            awakened = any(f'GX job {job_id} is now ' in text for item in body['input']
                                           if item.get('role') == 'user' for text in message_texts(item))
                            if parked.get('waiting') and not awakened:
                                return message('Parked on the assigned independent fit Job.', info)
                            raise NeedTool('gx_recalculate_geometry', {'protocol': route['protocol']})
                        if reproduced['status'] != 'matches':
                            raise ValueError('independent and primary fits disagree')
                        checks[route['protocol']] = reproduced['check_ref']
                comparisons = evidence['comparison']['comparisons']
                reason = (f'{len(comparisons)} comparisons were retained. ' + evidence['comparison']['reason'])
                if comparisons:
                    reason += (f' Maximum angle difference {max(c["angle_delta_deg"] for c in comparisons):.5f} degrees, '
                               f'maximum fitted RMSD difference {max(c["rmsd_delta_angstrom"] for c in comparisons):.5f} Å.')
                guide = read(task['quality_summary_ref']).get('source_guide')
                id_field = 'hypothesis_id' if guide and plan.get('previous_decision_ref') else 'id'
                limitations = ('The pairs share atoms and use one deposited model. Disorder and missing density limit inference.'
                               if not guide or id_field == 'hypothesis_id' else [
                                   'The three pair fits share atom coordinates.',
                                   'A single experimental crystal cannot determine the solution-state geometry.'])
                product = {'proposal_ref': task['proposal_ref'], 'evidence_ref': task['evidence_ref'],
                           'checks': checks, 'status': status,
                           'findings': {h[id_field]: {'supported': None if status == 'insufficient'
                                                 else h['expected'] == status, 'reason': reason} for h in plan['hypotheses']},
                           'limitations': limitations}
            elif stage == 'geometry_decision':
                assessed = read(task['assessment_ref'])
                observed = {r['protocol'] for r in evidence['routes']}
                next_protocols = []
                if not finish_if_stable and assessed['status'] == 'stable' and 'ordered_backbone' in task['available_protocols'] and (
                        observed == {'common_backbone', 'common_ca'}):
                    next_protocols = ['ordered_backbone']
                reason = (f'The independent assessment found {assessed["status"]} for {sorted(observed)}. '
                          + ('Test whether high-order residues concentrated in a smaller subset change that comparison.'
                             if next_protocols else 'The measured masks and pairwise deltas delimit the supported scope.'))
                product = {'proposal_ref': task['proposal_ref'], 'evidence_ref': task['evidence_ref'],
                           'assessment_ref': task['assessment_ref'], 'reason': reason,
                           'counterevidence': evidence['comparison']['reason'],
                           'uncertainty': 'Correlated pairs and unobserved atoms preclude extrapolation to solution geometry.',
                           'next_protocols': next_protocols,
                           'stop_reason': (None if next_protocols else
                               'The two common-atom selections answer the defined question for atoms recovered in all chains. '
                               'The narrower ordered subset remains untested.'
                               if finish_if_stable and assessed['status'] == 'stable' and 'ordered_backbone' in task['available_protocols']
                               else 'No further funded, informative atom selection remains for this comparison.')}
            else:
                raise ValueError(f'Unexpected workflow output kind {stage}')
        product['intent_responses'] = [{'item_id': k, 'version': item['version'],
                                        'reason': 'The project-provided coordinate source is used within the assigned scope.'}
                                       for k, item in task.get('intent_items', {}).items()]
        name, args = 'gx_submit_product', {'product': product}
    except NeedTool as call:
        name, args = call.name, call.arguments
    return ({'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': 'call-' + ident},
            {**info, 'tool': name})
