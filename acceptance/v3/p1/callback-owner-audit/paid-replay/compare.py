"""Verify the paired trials and retain conditional, time-block-resampled tail differences."""

from collections import defaultdict
import ast
import gzip
import json
import math
from pathlib import Path
import random
import statistics
import tarfile

from reconstruct import HERE, PERF, canonical, sha


def q95(values):
    x = sorted(values)
    return x[math.ceil(len(x) * 0.95) - 1]


def scale(values):
    x = sorted(values)
    return {
        "n": len(x),
        "p50": round(statistics.median(x), 4),
        "p95": round(q95(x), 4),
        "max": round(x[-1], 4),
    }


def bootstrap(rows):
    # Within each randomized time block retain all seven original cases and both builds together.
    grouped = defaultdict(dict)
    for r in rows:
        grouped[r["round"]].setdefault(r["case"], {})[r["arm"]] = r["gx_callback_ms"]
    pairs = [[(v["v21"], v["p1"]) for v in by_case.values()] for by_case in grouped.values()]
    assert all(len(x) == 7 for x in pairs)
    vals = [i for rr in pairs for i in rr]
    rng = random.Random(6043)
    boot_med = []
    boot_tail = []
    for _ in range(6000):
        draw = [i for rr in rng.choices(pairs, k=len(pairs)) for i in rr]
        boot_med.append(statistics.median(b - a for a, b in draw))
        boot_tail.append(q95(b for a, b in draw) - q95(a for a, b in draw))

    def interval(v):
        v.sort()
        return [round(v[149], 4), round(v[5849], 4)]

    return {
        "round_blocks": len(pairs),
        "each_contains_all_seven_cases": True,
        "resamples": 6000,
        "median_paired_difference_ms": round(statistics.median(b - a for a, b in vals), 4),
        "median_paired_difference_block_bootstrap_95pct_ms": interval(boot_med),
        "difference_in_marginal_p95_ms": round(
            q95(b for a, b in vals) - q95(a for a, b in vals), 4
        ),
        "difference_in_marginal_p95_block_bootstrap_95pct_ms": interval(boot_tail),
    }


def main():
    metrics = ["gx_callback_ms", "gx_writer_ms", "outside_writer_ms", "cpu_ms", "gx_writer_lock_ms"]
    paths = {
        "local_code_1": HERE,
        "local_code_2": HERE / "repeat",
        "shared_code_and_state": HERE / "shared-code-and-state",
    }
    hist = json.loads((PERF / "paid-submit-timing" / "historical-samples.json").read_bytes())
    hist_by_id = {s["call_id"]: s for s in hist["samples"]}
    result = {
        "series": {},
        "cases": {},
        "original_sample_count": len(hist["samples"]),
        "historical_markers_sha256": sha(
            (PERF / "paid-submit-timing" / "historical-samples.json").read_bytes()
        ),
    }
    images = {}
    envs = {}
    expected_cases = None
    with tarfile.open(HERE / "measured-sources.tar.gz", "r:gz") as bundle:
        measured = {
            n: bundle.extractfile(n).read()
            for n in ("reconstruct.py", "replay.py", "shared_checkout.py")
        }
    source_custody = {}
    for n, original in measured.items():
        published = (HERE / n).read_bytes()
        tree = ast.dump(ast.parse(original), include_attributes=False)
        assert tree == ast.dump(ast.parse(published), include_attributes=False)
        source_custody[n] = {
            "exact_measured_sha256": sha(original),
            "readable_source_sha256": sha(published),
            "identical_ast_sha256": sha(tree.encode()),
        }
    result["source_custody"] = {
        "original_bundle_sha256": sha((HERE / "measured-sources.tar.gz").read_bytes()),
        "files": source_custody,
    }
    for name, where in paths.items():
        doc = json.loads((where / "results.json").read_bytes())
        meta = doc["metadata"]
        rows = list(map(json.loads, gzip.open(where / "samples.jsonl.gz", "rt")))
        src = doc["fixtures"]
        cases = set(src)
        images[name] = {n: f["snapshot_db_sha256"] for n, f in src.items()}
        assert meta["disposable_removed"] and meta["filesystem"] == "hcufs"
        assert all(sha(measured[f]) == h for f, h in meta["diagnostic_sha256"].items())
        assert meta["parent_run_py_sha256"] == sha((HERE.parent / "run.py").read_bytes())
        assert all(sha((where / f).read_bytes()) == h for f, h in doc["raw_sha256"].items())
        assert meta["input_samples_sha256"] == sha(
            (PERF / "callback-samples.jsonl.gz").read_bytes()
        )
        if name == "shared_code_and_state":
            assert meta["source_export_removed"] and meta["shared_checkout_script_sha256"] == sha(
                measured["shared_checkout.py"]
            )
        envelopes = list(map(json.loads, gzip.open(where / "official-envelopes.jsonl.gz", "rt")))
        envs[name] = sha(canonical(envelopes))
        assert {r["case"] for r in envelopes} == cases
        if expected_cases:
            assert cases == expected_cases
        expected_cases = cases
        for f in envelopes:
            c = f["case"]
            fx = src[c]
            s = hist_by_id[fx["sample"]["call_id"]]
            assert (
                fx["original_product_sha256"]
                == s["product_sha256"]
                == sha(canonical(f["request"]["params"]["arguments"]["product"]))
            )
            assert (
                fx["rebound_owner"]["lease_id"] == s["lease_id"]
                and s["outcome"] == "ACCEPTED"
                and len(fx["science_routes"]) == 2
            )
            assert s["product_bytes"] == fx["sample"]["product_bytes"]
            if c not in result["cases"]:
                result["cases"][c] = {
                    "paid_callback_ms": s["total_ms"],
                    "paid_writer_ms": fx["sample"]["writer_ms"],
                    "paid_product_bytes": s["product_bytes"],
                    "paid_phases_ms": s["phases"],
                    "custody": fx["official"],
                    "product_sha256": fx["original_product_sha256"],
                    "logical_input_artifact_set_sha256": fx["manifest_sha256"],
                    "replays": {},
                }
            own = [r for r in rows if r["case"] == c]
            assert len(own) == 66
            for r in own:
                assert (
                    r["request_sha256"] == fx["request_sha256"]
                    and r["result"] == "ACCEPTED"
                    and r["writes"] == 17
                )
            for traced, n in [(False, 24), (True, 8)]:
                for arm in ("v21", "p1"):
                    group = [
                        r
                        for r in own
                        if r["arm"] == arm and r["traced"] == traced and not r["warmup"]
                    ]
                    assert (
                        len(group) == n
                        and sum(r["arm_order_in_pair"] == 0 for r in group) == n // 2
                    )
                    if traced:
                        assert all(
                            len(r["trace"]["raw_fsyncs"]) == 13
                            and len([x for x in r["trace"]["raw_fsyncs"] if x[1] == "file"]) == 2
                            for r in group
                        )
            result["cases"][c]["replays"][name] = {
                arm: doc["measurements"][c][arm]["gx_callback_ms"] for arm in ("v21", "p1")
            }
        bare = [r for r in rows if not r["warmup"] and not r["traced"]]
        probes = [r for r in rows if not r["warmup"] and r["traced"]]
        armstats = {}
        for arm in ("v21", "p1"):
            b = [r for r in bare if r["arm"] == arm]
            t = [r for r in probes if r["arm"] == arm]
            summary = {k: scale(r[k] for r in b) for k in metrics}
            summary["bare_above_100ms"] = sum(r["gx_callback_ms"] > 100 for r in b)
            summary["traced"] = {
                k: scale(r["trace"][k] for r in t) for k in t[0]["trace"] if k != "raw_fsyncs"
            }
            armstats[arm] = summary
        result["series"][name] = {
            "start_utc": meta["start"],
            "finish_utc": meta["finish"],
            "builds": armstats,
            "paired_inference": bootstrap(bare),
            "n_including_warmup": len(rows),
            "sqlite_sync_modes": sorted({r["synchronous"] for r in rows}),
        }
    assert (
        len(set(envs.values())) == 1 and images["local_code_2"] == images["shared_code_and_state"]
    )
    result["official_envelope_set_sha256"] = next(iter(envs.values()))
    result["physical_images_agree_repeats"] = {
        k: images["local_code_2"][k] == images["shared_code_and_state"][k] for k in expected_cases
    }
    result["first_series_free_list_layout_differences"] = sorted(
        k for k in images["local_code_1"] if images["local_code_1"][k] != images["local_code_2"][k]
    )
    (HERE / "comparison.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print({k: v["paired_inference"] for k, v in result["series"].items()})


if __name__ == "__main__":
    main()
