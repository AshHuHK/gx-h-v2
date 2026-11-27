"""Public engineering fixture for mixed scientific outputs in a single detached Job."""
import json
from pathlib import Path
import sys


def main(input_path, output_dir):
    readings = json.loads(Path(input_path).read_bytes())["readings"]
    output_dir = Path(output_dir)
    analysis = {"schema": "mean/1", "value": sum(readings) / len(readings)}
    prediction = {"schema": "linear-extrapolation/1", "value": 2 * readings[-1] - readings[-2]}
    (output_dir / "mean.json").write_text(json.dumps(analysis))
    (output_dir / "next.json").write_text(json.dumps(prediction))


if __name__ == "__main__":
    main(*sys.argv[1:])
