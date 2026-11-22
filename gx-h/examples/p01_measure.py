"""Summarize a single opt-in GX_METRICS_FILE; intervals can overlap."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(path):
    counts, tools, errors, durations, waits, work = (Counter() for _ in range(6))
    usage = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            kind = row["kind"]
            counts[kind] += 1
            if "duration_ms" in row:
                durations[kind] += row["duration_ms"]
            if kind == "gx_tool_dispatch":
                tools[row["tool"]] += 1
                if row.get("error_code"):
                    errors[row["error_code"]] += 1
            elif kind == "project_wait":
                waits[row["wake_reason"]] += 1
            elif kind == "root_work_after_turn":
                work[row["work_status"]] += 1
            elif kind == "usage_report":
                usage.append(row)
    return {
        "event_counts": dict(counts), "gx_tool_calls_by_name": dict(tools),
        "tool_errors_by_code": dict(errors), "wait_wakes": dict(waits),
        "work_after_root_turn": dict(work), "interval_ms_by_kind": dict(durations),
        "raw_usage_reports": usage,
        "model_request_count": None, "model_wait_ms": None, "model_cost": None,
        "measurement_gaps": "App Server turns may contain multiple model requests. "
            "Provider wait and billing are not observed by this trace.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    print(json.dumps(summarize(parser.parse_args().trace), indent=2))
