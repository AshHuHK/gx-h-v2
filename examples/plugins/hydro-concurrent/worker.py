"""Compare two channels on their jointly observed, non-overlapping time intervals."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path


def number(value):
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite():
        raise ValueError("Expected a finite JSON number")
    return Decimal(value)


def prepare(data):
    if data.get("schema") != "gx.hydrometry/1":
        raise ValueError("Original time-resolved hydrometry/1 intervals are required")
    channels = data["channels"]
    if len(channels) != 2 or len(set(channels)) != 2 or not all(isinstance(c, str) and c for c in channels):
        raise ValueError("Expected two different ordered channels")
    lo, hi = map(number, data["window_minutes"])
    if hi <= lo:
        raise ValueError("The trial window must have positive duration")
    by_channel = {c: [] for c in channels}
    for row in data["intervals"]:
        a, b = number(row["start"]), number(row["end"])
        if row["channel"] not in by_channel or not lo <= a < b <= hi or type(row["valid"]) is not bool:
            raise ValueError("Invalid channel, interval, or quality flag")
        rate = error = None
        if row["valid"]:
            rate, error = number(row["q_L_s"]), number(row["err_L_s"])
            if rate < 0 or error < 0:
                raise ValueError("Rate and absolute uncertainty must be nonnegative")
        by_channel[row["channel"]].append((a, b, rate, error))
    accepted = []
    for channel in channels:
        rows = sorted(by_channel[channel], key=lambda row: row[:2])
        if any(row[0] < prev[1] for prev, row in zip(rows, rows[1:])):
            raise ValueError("Overlapping intervals on the same channel are ambiguous")
        accepted.append([row for row in rows if row[2] is not None])
    return channels, hi - lo, accepted


def measure(raw):
    data = json.loads(raw, parse_float=Decimal)
    channels, window_minutes, (left, right) = prepare(data)
    i = j = 0
    duration = west = east = lower = upper = Decimal(0)
    intervals = []
    while i < len(left) and j < len(right):
        a, b, lrate, lerr = left[i]
        c, d, rrate, rerr = right[j]
        begin, end = max(a, c), min(b, d)
        if end > begin:
            minutes = end - begin
            seconds = 60 * minutes
            duration += minutes
            west += seconds * lrate
            east += seconds * rrate
            lower += seconds * (max(Decimal(0), lrate - lerr) - (rrate + rerr))
            upper += seconds * (lrate + lerr - max(Decimal(0), rrate - rerr))
            intervals.append({"start_min":float(begin), "end_min":float(end)})
        if b <= d:
            i += 1
        if d <= b:
            j += 1
    return {
        "schema":"gx.hydrometry-paired/1", "trial":data["trial"], "channels":channels,
        "input_sha256":hashlib.sha256(raw).hexdigest(),
        "common_minutes":float(duration), "outside_common_minutes":float(window_minutes - duration),
        "left_L":float(west), "right_L":float(east), "difference_L":float(west-east),
        "difference_low_L":float(lower), "difference_high_L":float(upper),
        "left_mean_L_s":float(west/(60*duration)) if duration else None,
        "right_mean_L_s":float(east/(60*duration)) if duration else None,
        "support":"positive_duration" if duration else "no_shared_observations",
        "intervals":intervals,
        "scope":"Volumes integrate over valid simultaneous intervals only. An empty support has zero duration and integrated volume; the mean and ordering there are unknown.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = measure(args.record.read_bytes())
    (args.out / "paired.json").write_text(json.dumps(result, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
