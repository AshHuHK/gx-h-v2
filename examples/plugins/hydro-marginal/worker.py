"""Read station-specific recorded totals. Keep the observation supports separate."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path


def number(value):
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite():
        raise ValueError("Expected a finite JSON number")
    return Decimal(value)


def measure(raw):
    data = json.loads(raw, parse_float=Decimal)
    left, right = data["channels"]
    lo, hi = map(number, data["window_minutes"])
    if not left or not right or left == right or lo >= hi:
        raise ValueError("Expected two different channels and a positive time window")
    rows = {}
    if data["schema"] == "gx.hydrometry-summary/1":
        for channel in (left, right):
            entry = data["summaries"][channel]
            rows[channel] = (number(entry["observed_minutes"]), number(entry["volume_L"]))
    elif data["schema"] == "gx.hydrometry/1":
        if any(row["channel"] not in (left, right) for row in data["intervals"]):
            raise ValueError("Unexpected channel")
        for channel in (left, right):
            minutes = volume = Decimal(0)
            last = lo
            for row in sorted((r for r in data["intervals"] if r["channel"] == channel), key=lambda r: number(r["start"])):
                start, end = number(row["start"]), number(row["end"])
                if not last <= start < end <= hi or type(row["valid"]) is not bool:
                    raise ValueError("Out-of-window, overlapping or unclassified interval")
                last = end
                if row["valid"]:
                    rate = number(row["q_L_s"])
                    if rate < 0:
                        raise ValueError("Expected a nonnegative discharge")
                    minutes += end-start
                    volume += 60*(end-start)*rate
            rows[channel] = (minutes, volume)
    else:
        raise ValueError("Needs a hydrometry/1 interval or hydrometry-summary/1 record")
    if any(not 0 <= m <= hi-lo or v < 0 or (not m and v) for m,v in rows.values()):
        raise ValueError("Marginal coverage or volume is inconsistent")
    return {"schema":"gx.hydrometry-marginals/1","trial":data["trial"],"channels":[left,right],
            "input_sha256":hashlib.sha256(raw).hexdigest(),"left_L":float(rows[left][1]),"right_L":float(rows[right][1]),
            "left_minutes":float(rows[left][0]),"right_minutes":float(rows[right][0]),
            "scope":"Each station has its own support. Marginal coverage alone does not give simultaneous duration, volumes, or the sign of a paired difference."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    (args.out / "marginal.json").write_text(json.dumps(measure(args.record.read_bytes()), allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
