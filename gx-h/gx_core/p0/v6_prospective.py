"""Release only the computations exposing a selected prospective endpoint."""
import json

from ..domain import ids
from ..errors import HoldActive, SchemaInvalid
from . import v623_chronology
from .v62_outputs import PREDICTION_TARGET_NATURE


def validate_opening(spec, catalog):
    if 'prospective_opening' not in spec:
        return
    opening = spec['prospective_opening']
    if not isinstance(opening, dict) or set(opening) - {'capabilities', 'material_roles', 'metric_ids'}:
        raise SchemaInvalid('prospective_opening selects capabilities, optional material_roles and metric_ids')
    granted_caps = {item['capability_version']: item for item in catalog}
    choices = opening.get('capabilities')
    if not _distinct_names(choices) or not set(choices) <= set(granted_caps):
        raise SchemaInvalid('The prospective opening must select one or more granted capability versions')
    metrics = opening.get('metric_ids')
    if metrics is not None and not _distinct_names(metrics):
        raise SchemaInvalid('prospective_opening.metric_ids must contain distinct named metrics')
    available_metrics = set()
    for capability in choices:
        eligible = {metric for output in granted_caps[capability]['outputs']
                    if output.get('nature') in PREDICTION_TARGET_NATURE.values() and output.get('source_roles')
                    for metric in output.get('metric_specs', {})}
        if not eligible or metrics is not None and not eligible.intersection(metrics):
            raise SchemaInvalid(f'{capability} has no matching registered numeric metric bound to original inputs')
        available_metrics.update(eligible)
    if metrics and not set(metrics) <= available_metrics:
        raise SchemaInvalid('Opening metric_ids must identify metrics declared by the selected capabilities')
    roles = opening.get('material_roles')
    available = {f.get('role', 'input') for f in spec.get('input_files', [])}
    if roles is not None and (not _distinct_names(roles) or not set(roles) <= available):
        raise SchemaInvalid('prospective_opening.material_roles must select granted original inputs')
    if not spec.get('input_files'):
        raise SchemaInvalid('A prospective opening needs original Goal materials')


def _distinct_names(values):
    return (isinstance(values, list) and bool(values) and all(isinstance(v, str) and v.strip() for v in values)
            and len(values) == len(set(values)))


def _opening(conn, run_id):
    row = conn.execute("SELECT json_extract(inputs_json,'$.inputs.p0_spec.prospective_opening'), "
                       "json_extract(inputs_json,'$.files') FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if not row or not row[0]:
        return None, set()
    opening = json.loads(row[0])
    roles = opening.get('material_roles')
    materials = {f['artifact_ref'] for f in json.loads(row[1] or '[]') if f.get('role') != 'workflow_spec'
                 and (roles is None or f.get('role', 'input') in roles)}
    return opening, materials


def _opened(conn, run_id, opening, materials):
    rows = conn.execute("""SELECT r.document_json FROM hypotheses h JOIN hypothesis_revisions r USING(hypothesis_id)
        JOIN v6_decision_tasks t ON t.context_ref=json_extract(r.document_json,'$.context_ref') AND t.run_id=h.run_id
        JOIN v6_goals g ON g.run_id=h.run_id AND g.goal_version=t.goal_version
        WHERE h.run_id=? AND t.scope='run' AND json_type(r.document_json,'$.prediction.method')='object'""", (run_id,))
    for (raw,) in rows:
        document = json.loads(raw)
        maturity = document.get('maturity', {})
        registration = maturity.get('registration') or {}
        method = (registration.get('method_pin') or {}).get('measurement_spec', {})
        if (method.get('capability') not in opening['capabilities'] or not valid_endpoint(method)
                or opening.get('metric_ids') and method['metric_id'] not in opening['metric_ids']
                or registration.get('posthoc_revision_of')):
            continue
        selected = {pin['artifact_ref'] for pin in maturity.get('material_coverage', [])
                    if not pin['known_at_registration'] and pin['qualification'] in ('unmeasured', 'prospective')}
        if 'scope_feedback' in document:
            selected.intersection_update(document['scope_feedback']['matching_refs'])
        if v623_chronology.materials_overlap(conn, selected, materials, registration['method_pin']):
            return True
    return False


def opening_pending(conn, run_id):
    opening, materials = _opening(conn, run_id)
    return bool(opening and not _opened(conn, run_id, opening, materials))


def valid_endpoint(method):
    return bool(method.get('input_role') and method.get('metric_id') and not method.get('missing_parameters'))


def match_parameters(expected, actual, names):
    return all(k in expected and k in actual and ids.canonical_json(expected[k]) == ids.canonical_json(actual[k])
               for k in names)


def _method_roles(conn, opening, producer):
    metrics = opening.get('metric_ids')
    for cap in opening['capabilities']:
        name, version = cap.rsplit('@', 1)
        row = conn.execute('SELECT outputs_json FROM capabilities WHERE capability_id=? AND version=?', (name, version)).fetchone()
        if not row:
            continue
        for output in json.loads(row[0]):
            if not output.get('metric_specs') or metrics and not set(metrics).intersection(output['metric_specs']):
                continue
            if cap == producer:
                yield from output.get('source_roles', ())
            for table in output.get('sufficient_outputs', ()):
                if table['capability'] == producer:
                    yield table['input_role']


def _active_drafts(conn, run_id):
    rows = conn.execute("""SELECT e.payload_json FROM events e JOIN v6_decision_tasks d
        ON d.task_id=json_extract(e.payload_json,'$.task_id') AND e.run_id=d.run_id
        JOIN v6_goals g ON g.run_id=d.run_id AND g.goal_version=d.goal_version
        JOIN tasks t ON t.task_id=d.task_id AND t.lease_id=json_extract(e.payload_json,'$.lease_id')
        WHERE e.run_id=? AND e.kind='v6.claim.staged' AND t.status IN ('RUNNING','CLAIMED')
        AND json_type(e.payload_json,'$.prediction_method.measurement_spec')='object'""", (run_id,))
    for (raw,) in rows:
        draft = json.loads(raw)
        method = draft['prediction_method']['measurement_spec']
        if valid_endpoint(method):
            yield method, {data['artifact_ref'] for data in draft['prediction_data']}


def require_registration(conn, run_id, capability, parameters, read_inputs):
    """Called at actual Job admission and cached-result release, inside their existing transactions.

    read_inputs is lazy so other work need not reread its input manifest or a claim artifact.
    A sufficient table may expose several settings; the method declares only its shared axes.
    """
    opening, materials = _opening(conn, run_id)
    inputs = None
    if opening:
        roles = set(_method_roles(conn, opening, capability))
        if roles:
            inputs = read_inputs()
            if (v623_chronology.materials_overlap(conn, {inputs[r] for r in roles if r in inputs}, materials)
                    and not _opened(conn, run_id, opening, materials)):
                raise HoldActive('Waiting for the Lead to adopt a prospective prediction for an opening method and material',
                                 details={'reason': 'prospective_opening'})
    for method, materials in _active_drafts(conn, run_id):
        roles = []
        if (method['capability'] == capability and
                ids.canonical_json(method['parameters']) == ids.canonical_json(parameters)):
            roles.append(method['input_role'])
        for table in method.get('sufficient_outputs', ()):
            if table['capability'] == capability and match_parameters(method['parameters'], parameters, table['match_parameters']):
                roles.append(table['input_role'])
        if roles:
            if inputs is None:
                inputs = read_inputs()
            if v623_chronology.materials_overlap(conn, {inputs[r] for r in roles if r in inputs}, materials,
                                                {'measurement_spec': method, 'manifest_sha256': method['manifest_sha256']}):
                raise HoldActive('Waiting for the decision on a staged prediction before producing its endpoint',
                                 details={'reason': 'prospective_prediction'})
