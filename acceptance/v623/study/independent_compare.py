"""Compare a named GX block-influence product against a fresh fit of the registered original."""
import argparse
from hashlib import sha256
import json
import math
from pathlib import Path

if __package__:
    from . import independent_geometry as ref
else:
    import independent_geometry as ref


def compare(expected, actual):
    """Match every index before comparing numbers. Incomplete tables and false zeros fail closed."""
    errors = []
    maxima = {}
    checked = {}
    boundaries = []
    exceptions = []
    peaks = {e: None for e in ref.LIMITS}
    near_control = []

    def equal(where, truth, seen):
        if ref.digest(seen) != ref.digest(truth):
            errors.append({'at': where, 'expected': truth, 'observed': seen})

    def number(where, truth, seen, endpoint, delta=False):
        kind = ('delta_' if delta else 'fit_') + endpoint
        checked[kind] = checked.get(kind, 0) + 1
        if truth is None:
            equal(where, None, seen)
        elif type(seen) not in (int, float) or not math.isfinite(seen):
            errors.append({'at': where, 'expected': truth, 'observed': str(seen)})
        else:
            difference = abs(truth - seen)
            maxima[kind] = max(maxima.get(kind, 0.), difference)
            if difference > ref.FIT_TOL[endpoint] * (2 if delta else 1):
                errors.append({'at': where, 'expected': truth, 'observed': seen, 'error': difference})

    def same_fields(a, b, keys, where):
        for key in keys:
            if key not in b:
                errors.append({'at': f'{where}.{key}', 'error': 'missing field'})
            else:
                equal(f'{where}.{key}', a[key], b[key])

    def pairs(a, b, where, phase):
        equal(where + '.pair_ids', sorted(ref.PAIRS), sorted(b))
        for pair in ref.PAIRS:
            for endpoint in ref.LIMITS:
                for delta in ([False] if phase == 'base' else [False, True]):
                    name = ('delta_' if delta else '') + endpoint
                    path = f'{where}.{pair}.{name}'
                    if name not in b.get(pair, {}):
                        errors.append({'at': path, 'error': 'missing endpoint'})
                    else:
                        number(path, a[pair][name], b[pair][name], endpoint, delta)

    equal('schema', 'gx.block-influence/1', actual.get('schema'))
    equal('method', 'deposited-block-influence@1', actual.get('method'))
    same_fields(expected, actual, ('entry', 'input_sha256', 'mask', 'model', 'label_entity', 'chains',
                                   'positions', 'matched_keys_sha256', 'union_positions'), 'source')
    same_fields(expected, actual, ('matched_atoms', 'declared_positions', 'matched_residues'), 'mapping')
    equal('observed_chains', expected['observed_by_chain'], actual.get('observed_chains'))
    equal('selected_residues', len(expected['positions']), actual.get('selected_residues'))
    equal('observed_union_residues', len(expected['union_positions']), actual.get('observed_union_residues'))
    pairs(expected['baseline'], actual.get('baseline', {}), 'baseline', 'base')
    observed = actual.get('blocks', [])
    equal('block_indexes', [b['index'] for b in expected['blocks']], [b.get('index') for b in observed])
    by_id = {b.get('index'): b for b in observed}
    for block in expected['blocks']:
        i = block['index']
        if i not in by_id:
            continue
        delivered = by_id[i]
        tag = f'block[{i}]'
        same_fields(block, delivered, ('positions', 'positions_sha256', 'interval', 'reasons', 'eligible', 'retained_count'), tag)
        if not block['eligible']:
            if delivered.get('controls') or delivered.get('pairs'):
                errors.append({'at': tag, 'error': 'ineligible block has numerical rows'})
            continue
        same_fields(block, delivered, ('retained_keys_sha256',), tag)
        pairs(block['pairs'], delivered.get('pairs', {}), tag, 'drop')
        cs = delivered.get('controls', [])
        equal(tag + '.control_keys', [c['k'] for c in block['controls']], [c.get('k') for c in cs])
        sampled = {c.get('k'): c for c in cs}
        for c in block['controls']:
            k = c['k']
            if k not in sampled:
                continue
            target = sampled[k]
            site = tag + f'.control[{k}]'
            same_fields(c, target, ('positions', 'positions_sha256'), site)
            pairs(c['pairs'], target.get('pairs', {}), site, 'controls')
        for endpoint, threshold in ref.LIMITS.items():
            median = block['median'][endpoint]
            dm = delivered.get('median', {}).get(endpoint, {})
            if 'delta' not in dm or 'concealed' not in dm:
                errors.append({'at': tag + '.median.' + endpoint, 'error': 'missing median output'})
                continue
            number(tag + '.median.' + endpoint, median['delta'], dm['delta'], endpoint, True)
            for direction in ('before', 'after'):
                number(tag + '.median.' + direction, median[direction], dm.get(direction), endpoint)
            raw_pair_values = {p: delivered.get('pairs', {}).get(p, {}).get('delta_' + endpoint) for p in ref.PAIRS}
            declared_ex = [p for p in ref.PAIRS if dm['delta'] is not None and raw_pair_values[p] is not None
                           and raw_pair_values[p] > threshold]
            equal(tag + '.median.' + endpoint + '.exceptions', declared_ex, dm.get('exceptions'))
            concealed = None if dm['delta'] is None else dm['delta'] <= threshold and bool(declared_ex)
            equal(tag + '.median.' + endpoint + '.concealed_deduced', concealed, dm['concealed'])
            edge = median['delta'] is not None and abs(median['delta'] - threshold) <= 2 * ref.FIT_TOL[endpoint]
            if edge:
                boundaries.append({'index': i, 'pair': 'median', 'endpoint': endpoint, 'delta': median['delta'],
                                   'margin': median['delta'] - threshold})
            for pair in ref.PAIRS:
                row = block['pairs'][pair]
                value = row['delta_' + endpoint]
                assessment = delivered.get('pairs', {}).get(pair, {}).get(endpoint + '_assessment', {})
                where = f'{tag}.{pair}.{endpoint}_assessment'
                independent = row[endpoint + '_interpretation']
                number(where + '.margin', independent['threshold_margin'], assessment.get('margin'), endpoint, True)
                number(where + '.control_max', independent['control_max'], assessment.get('control_max'), endpoint, True)
                reported_delta = raw_pair_values[pair]
                reported_controls = [ctrl.get('pairs', {}).get(pair, {}).get('delta_' + endpoint) for ctrl in cs]
                sufficient = len(reported_controls) == 20 and None not in reported_controls
                equal(where + '.over_limit', None if reported_delta is None else reported_delta > threshold,
                      assessment.get('over_limit'))
                equal(where + '.borderline', None if reported_delta is None else
                      abs(reported_delta - threshold) <= 2 * ref.FIT_TOL[endpoint], assessment.get('borderline'))
                equal(where + '.exceeds_dispersed_range', None if reported_delta is None or not sufficient else
                      reported_delta > threshold and reported_delta > max(reported_controls),
                      assessment.get('exceeds_dispersed_range'))
                if value is None:
                    continue
                finding = row[endpoint + '_interpretation']
                obs = {'index': i, 'pair': pair, 'endpoint': endpoint, 'delta': value,
                       'margin': finding['threshold_margin'], 'boundary': finding['boundary'],
                       'median_conceals': median['concealed'],
                       'above_all_20_and_over_tolerance': finding['above_all_20_and_over_tolerance']}
                if not peaks[endpoint] or value > peaks[endpoint]['delta']:
                    peaks[endpoint] = obs
                if finding['over_tolerance']:
                    exceptions.append(obs)
                if finding['boundary']:
                    boundaries.append(obs)
                    edge = True
                controls = [c['pairs'][pair]['delta_' + endpoint] for c in block['controls']]
                if len(controls) == 20 and None not in controls and value > threshold:
                    diff = value - max(controls)
                    if abs(diff) <= 4 * ref.FIT_TOL[endpoint]:
                        near_control.append(dict(index=i, pair=pair, endpoint=endpoint, margin_vs_max_control=diff))
            if median['concealed'] != dm['concealed'] and not edge:
                equal(tag + '.median.' + endpoint + '.concealed', median['concealed'], dm['concealed'])
    active = [b for b in expected['blocks'] if b['eligible']]
    equal('eligible_blocks', len(active), actual.get('eligible_blocks'))
    scope = {}
    for ep in ref.LIMITS:
        missing = [{'index': b['index'], 'pair': pair} for b in active for pair in ref.PAIRS
                   if b['pairs'][pair]['delta_' + ep] is None]
        exceedances = sum(row['endpoint'] == ep for row in exceptions)
        result = ('insufficient_blocks' if len(active) < 3 else
                  'present_chain_pairs_and_blocks' if exceedances else
                  'incomplete_fits' if missing else 'within_tolerance_on_all_eligible_blocks')
        declared = actual.get('summary', {}).get(ep, {})
        usable = [b for b in observed if b.get('eligible') and
                  b.get('median', {}).get(ep, {}).get('delta') is not None]
        values = [(b['index'], p, row['delta_' + ep]) for b in observed for p,row in b.get('pairs',{}).items()
                  if row.get('delta_' + ep) is not None]
        seen_ex = [{'block_index': i, 'pair': p, 'delta': v} for i, p, v in values if v > ref.LIMITS[ep]]
        best = min(values, key=lambda t: (-t[2], t[0], t[1]), default=(None, None, None))
        equal(ep + '.summary.winning_pair', list(best[:2]), [declared.get('block_index'), declared.get('pair')])
        number(ep + '.summary.max', None if peaks[ep] is None else peaks[ep]['delta'], declared.get('max'), ep, True)
        equal(ep + '.summary.exceptions', seen_ex, declared.get('exceptions'))
        equal(ep + '.summary.concealed', [b['index'] for b in usable if b['median'][ep]['concealed']],
              declared.get('concealed_blocks'))
        equal(ep + '.summary.usable', len(usable), declared.get('usable_blocks'))
        equal(ep + '.summary.eligible', len(active), declared.get('eligible_blocks'))
        equal(ep + '.summary.global', len(usable) >= 3, declared.get('global_assessable'))
        equal(ep + '.summary.within', not bool(seen_ex) if len(usable) >= 3 and len(usable) == len(active) else None,
              declared.get('all_within_limit'))
        scope[ep] = {'eligible_block_count': len(active), 'unavailable': missing,
                     'over_tolerance_count': exceedances, 'recommendation': result}
    return {'status': 'FAIL' if errors else 'PASS', 'endpoint_scope': scope, 'errors': errors, 'checked': checked, 'max_abs_errors': maxima,
            'input_sha256': expected['input_sha256'], 'entry': expected['entry'], 'mask': expected['mask'],
            'fit_tolerances': ref.FIT_TOL, 'delta_tolerances': {k: 2*v for k, v in ref.FIT_TOL.items()},
            'source_parser': expected['parser'], 'independent_solver': expected['solver'],
            'eligible_blocks': [b['index'] for b in expected['blocks'] if b['eligible']],
             'ineligible_blocks': {str(b['index']): b['reasons'] for b in expected['blocks'] if not b['eligible']},
            'matched_atoms': expected['matched_atoms'], 'matched_positions': len(expected['positions']),
            'positions_sha256': expected['positions_sha256'], 'matched_keys_sha256': expected['matched_keys_sha256'],
            'union_positions': len(expected['union_positions']), 'revisions': expected['revisions'],
            'exceptions': exceptions, 'threshold_boundaries': boundaries, 'control_boundaries': near_control,
            'largest_absolute_changes': peaks}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--structure', type=Path, required=True)
    cli.add_argument('--entry', required=True)
    cli.add_argument('--input-sha256', required=True)
    cli.add_argument('--mask', required=True, choices=ref.MASKS)
    cli.add_argument('--product', type=Path, required=True)
    cli.add_argument('--product-sha256', required=True)
    cli.add_argument('--artifact-ref')
    cli.add_argument('--job-id')
    cli.add_argument('--output', type=Path, required=True)
    opts = cli.parse_args()
    expected = ref.reference(opts.structure.read_bytes(), opts.entry, opts.input_sha256, opts.mask)
    raw = opts.product.read_bytes()
    if sha256(raw).hexdigest() != opts.product_sha256:
        raise ValueError('Product artifact differs from the supplied digest')
    report = compare(expected, json.loads(raw))
    report.update(product_sha256=opts.product_sha256, artifact_ref=opts.artifact_ref, job_id=opts.job_id)
    with opts.output.open('x') as out:
        json.dump(report, out, allow_nan=False, indent=2)
        out.write('\n')
    return int(report['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
