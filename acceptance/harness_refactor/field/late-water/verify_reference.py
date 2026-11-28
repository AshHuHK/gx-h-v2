"""Independently recompute the reference from archived NWIS bytes, offline."""
import argparse
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
RAW = HERE.parent / 'hydro/a/usgs.json'
ARCHIVE_SHA256 = '03c1879879b8c2fdff492ae438cc0c3f3f968d186f30b6c30db533b44a7bd336'
SITES = [('first-station.json', '01646500'), ('later-station.json', '01651000')]
DATES = [f'2023-04-{d:02d}T00:00:00.000' for d in (1, 2, 3)]


def check(condition, detail):
    if not condition:
        raise ValueError(detail)


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def percent_display(pct):
    return format(Decimal(pct.numerator) / Decimal(pct.denominator), '.6f')


def build_reference():
    data = RAW.read_bytes()
    check(sha256(data).hexdigest() == ARCHIVE_SHA256, 'archived NWIS byte digest changed')
    source = json.loads(data)['value']
    series = source['timeSeries']
    check(len(series) == len(SITES), 'unexpected station count')
    query = source['queryInfo']['queryURL']
    pins = json.loads((HERE / 'preregistered.json').read_bytes())['main_inputs']
    stations = {}
    percent = {}
    signed_change = {}
    for index, (filename, site) in enumerate(SITES):
        attachment_bytes = (HERE / filename).read_bytes()
        check(sha256(attachment_bytes).hexdigest() == pins[filename], f'{filename}: changed bytes')
        attachment = json.loads(attachment_bytes)
        original = series[index]
        pointer = f'/value/timeSeries/{index}'
        check(attachment['archive_sha256'] == ARCHIVE_SHA256 and
              attachment['archive_query'] == query and attachment['json_pointer'] == pointer,
              f'{filename}: wrong source reference')
        check(canon(attachment['timeSeries']) == canon(original), f'{filename}: changed timeSeries')
        info = original['sourceInfo']
        check(info['siteCode'][0]['value'] == site and info['siteCode'][0]['agencyCode'] == 'USGS',
              f'{filename}: different station')
        var = original['variable']
        unit = var['unit']['unitCode']
        check(var['variableCode'][0]['value'] == '00060' and unit == 'ft3/s' and
              any(o['name'] == 'Statistic' and o['value'] == 'Mean' and o['optionCode'] == '00003'
                  for o in var['options']['option']), f'{filename}: different variable or statistic')
        only_block, = original['values']
        observations = only_block['value']
        check([r['dateTime'] for r in observations] == DATES, f'{filename}: incomplete dates')
        check(all(r['qualifiers'] == ['A'] for r in observations), f'{filename}: qualifier changed')
        qualifier, = [q for q in only_block['qualifier'] if q['qualifierCode'] == 'A']
        flow = [Decimal(r['value']) for r in observations]
        check(all(x > 0 for x in flow), f'{filename}: unexpected missing or nonpositive flow')
        signed_change[site] = flow[-1] - flow[0]
        percent[site] = -100 * Fraction(signed_change[site]) / Fraction(flow[0])
        stations[site] = {
            'station_name': info['siteName'], 'unit': unit, 'parameter_code': '00060',
            'statistic_code': '00003', 'attachment': filename, 'attachment_sha256': pins[filename],
            'raw_json_pointer': pointer, 'canonical_timeSeries_sha256': sha256(canon(original)).hexdigest(),
            'observations_from_archive': observations, 'qualifier_A_definition': qualifier['qualifierDescription'],
            'daily_changes_ft3_s': [str(b - a) for a, b in zip(flow, flow[1:])],
            'signed_change_ft3_s': str(signed_change[site]),
            'drop_percent_exact': str(percent[site]), 'drop_percent': percent_display(percent[site]),
        }
    ranked = sorted(stations, key=percent.__getitem__)
    check(percent[ranked[0]] != percent[ranked[1]], 'unexpected relative tie')
    gap = percent[ranked[1]] - percent[ranked[0]]
    return {
        'archive': '../hydro/a/usgs.json', 'archive_sha256': ARCHIVE_SHA256, 'archived_query': query,
        'reference_design': 'Only the pinned raw archive supplies measurements. Canonical JSON comparison '
                            'checks the entire timeSeries subtree of each separately serialized attachment.',
        'statistic': 'Percent decrease = 100 × (first daily mean − last daily mean) / first daily mean; '
                     'three daily means span two day-to-day intervals.',
        'stations': stations,
        'larger_proportional_decrease_site': ranked[-1],
        'larger_absolute_decrease_site': min(signed_change, key=signed_change.__getitem__),
        'difference_percentage_points_exact': str(gap), 'difference_percentage_points': percent_display(gap),
        'limitations': [
            'Both stations have approved daily means. The qualifier supplies no numerical uncertainty interval.',
            'Three daily aggregates do not determine subdaily trajectories or event timing.',
            'These descriptive changes do not establish runoff causes, statistical significance or a general recession law.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--print', dest='emit', action='store_true', help='print the recomputed reference')
    args = parser.parse_args()
    reference = build_reference()
    if args.emit:
        print(json.dumps(reference, ensure_ascii=False, indent=2))
    else:
        check(json.loads((HERE / 'reference.json').read_bytes()) == reference, 'reference.json differs from raw data')
        print('PASS: archive digest, both attachment digests, full source subtrees and exact numerical reference')


if __name__ == '__main__':
    main()
