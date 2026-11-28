"""Compare both installed discharge methods against an independent calculation on the original USGS units."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def verify(release):
    checked = {}
    for case in ('a', 'b'):
        directory = HERE / case
        original = directory / 'usgs.json'
        data = directory / 'discharge.json'
        reference = json.loads((directory / 'reference.json').read_bytes())
        if reference['input'] != hashlib.sha256(original.read_bytes()).hexdigest():
            raise ValueError('Changed archived USGS input')
        expected = {'left_L': reference['volumes_L'][0], 'right_L': reference['volumes_L'][1],
                    'difference_low_L': reference['lower_L'], 'difference_high_L': reference['upper_L']}
        checks = []
        with tempfile.TemporaryDirectory() as root:
            for method, output, fields in [('marginal', 'marginal.json', ('left_L','right_L')),
                                           ('concurrent', 'paired.json', tuple(expected))]:
                script = release/'gx-h/plugins/installed'/('org.gx.hydro-' + method)/'1.0.0/worker.py'
                subprocess.run([sys.executable, '-I', '-B', str(script), '--record', str(data), '--out', root], check=True)
                observed = json.loads((Path(root)/output).read_bytes(), parse_float=Decimal)
                if observed['input_sha256'] != hashlib.sha256(data.read_bytes()).hexdigest():
                    raise ValueError('Method did not identify this input')
                for key in fields:
                    actual, target = Decimal(observed[key]), Decimal(expected[key])
                    difference = abs(actual - target)
                    if difference > max(Decimal(reference['rounding_absolute_tolerance_L']),
                                        Decimal(reference['rounding_relative_tolerance'])*abs(target)):
                        raise ValueError(f'{case} {method} {key} differs: {difference}')
                    checks.append({'method':method, 'output':key, 'observed':str(actual),
                                   'expected':str(target), 'abs_error_L':str(difference)})
            if observed['common_minutes'] != 1440 * len(reference['shared_dates']):
                raise ValueError('Paired time windows disagree')
        checked[case] = {'checks':checks, 'raw_sha256':reference['input'], 'common_minutes':str(observed['common_minutes'])}
    return {'scope':'PORTABLE_CPU_PREFLIGHT; independent raw-unit check, no model or GX Job', 'cases':checked}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.release.resolve()), indent=2))
