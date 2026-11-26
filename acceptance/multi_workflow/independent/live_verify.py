"""Independently fit only registered selections with successful, published physical Job outputs."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from statistics import median
import sys

import Bio
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO), str(REPO / "gx-h")]
from acceptance.v623.study.independent_geometry import FIT_TOL, PAIRS, select


def require(ok, message):
    if not ok:
        raise ValueError(message)


def fit(fixed, moving, endpoint):
    """Same proper-SVD reference, calculating only the scalar the completed Job selected."""
    if len(fixed) < 3:
        return None
    a, b = moving - moving.mean(axis=0), fixed - fixed.mean(axis=0)
    u, singular, vt = np.linalg.svd(a.T @ b)
    sign = -1 if np.linalg.det(u @ vt) < 0 else 1
    if singular[0] == 0 or singular[1] + sign * singular[2] <= 1e-12 * singular[0]:
        return None
    rot = (u * np.array([1, 1, sign])) @ vt
    if endpoint == "angle_deg":
        return float(np.degrees(np.arccos(np.clip((np.trace(rot)-1)/2, -1, 1))))
    require(endpoint == "rmsd_A", "unknown endpoint")
    residual = a @ rot - b
    return float(np.sqrt(np.sum(residual**2) / len(a)))


def group_median(values):
    return None if None in values else median(values)


def change(a, b):
    return {"before": a, "after": b, "delta": None if a is None or b is None else b-a}


def label(pairs, med, e, z):
    values = [r["delta"] for r in pairs.values()]
    if None in values:
        return "unmeasured"
    large = [v for v in values if abs(v) >= e]
    if not large:
        return "quiet"
    if abs(med) >= e:
        return "opposed" if any(v*med < 0 for v in large) else "represented"
    return "near_zero" if abs(med) < z else "attenuated"


def numeric(value, reported, tolerance, path, discrepancies):
    if value is None or reported is None:
        gap = None
        if value != reported:
            discrepancies.append({"field": path, "reference": value, "reported": reported})
    else:
        gap = abs(value - reported)
        if not math.isfinite(gap) or gap > tolerance:
            discrepancies.append({"field": path, "reference": value, "reported": reported,
                                  "absolute_difference": gap, "tolerance": tolerance})
    return gap


def verify_signed(raw, params, summary, details):
    entry, endpoint = summary["entry"], params["endpoint"]
    for doc, schema in [(summary,"gx.signed-summary/1"), (details,"gx.signed-blocks/1")]:
        require(doc["schema"] == schema and doc["method"] == "deposited-block-signed@1", "signed output schema")
        require(doc["entry"] == entry and doc["input_sha256"] == sha256(raw).hexdigest(), "signed input mismatch")
        require(all(doc[k] == v for k,v in params.items()), "signed output differs from Job parameters")
        require(doc["unit"] == {"angle_deg":"deg","rmsd_A":"Å"}[endpoint], "incorrect fit unit")
    meta, keys, xyz = select(raw, entry, summary["input_sha256"], params["mask"])
    positions, n = meta["positions"], len(meta["positions"])
    union = len(meta["union_positions"])
    require((summary["matched_keys_sha256"], summary["selected_residues"], summary["observed_union_residues"])
            == (meta["matched_keys_sha256"], n, union), "matched coordinate selection differs")
    require(summary["chains"] == details["chains"] == list("ABC") and summary["model"] == summary["label_entity"] == "1",
            "unexpected chain, model or entity")
    ix = np.asarray([r[0] for r in keys])
    def fits(keep):
        return {p:fit(xyz[p[0]][keep], xyz[p[1]][keep], endpoint) for p in PAIRS}
    base = fits(slice(None))
    official = {r["block_index"]:r for r in details["blocks"]}
    upper = max([0, *meta["declared_positions"], *meta["union_positions"]])
    require(len(official) == len(details["blocks"]) and sorted(official) == list(range((upper+19)//20)), "block universe")
    e, z, k = (params[i] for i in ("effect_minimum","zero_limit","minimum_evaluable"))
    rows, disagreements, counts = [], [], Counter()
    max_diff = {"fit":0.0, "delta":0.0}
    for i, observed in sorted(official.items()):
        lo, hi = 20*i+1, 20*i+20
        omit = [p for p in positions if lo <= p <= hi]
        keep_n = n-len(omit)
        eligible = n >= 60 and union > 0 and 5*n >= 3*union and len(omit)>=10 and keep_n >= 60 and 5*keep_n>=3*n
        require([lo,hi] == observed["interval"] and omit==observed["omitted_positions"]
                and keep_n == observed["retained_count"] and eligible == observed["eligible"], "block selection differs")
        row = {"index": i, "interval": [lo,hi], "matched_in_interval":len(omit), "retained":keep_n}
        name = "ineligible"
        if eligible:
            end = fits((ix<lo)|(ix>hi))
            pairs = {p:change(base[p],end[p]) for p in PAIRS}
            mid = change(group_median(list(base.values())),group_median(list(end.values())))
            name = label(pairs, mid["delta"], e, z)
            for tag, ref in {**pairs, "median":mid}.items():
                value = observed["median"] if tag=="median" else observed["pairs"][tag]
                for f, val in ref.items():
                    kind = "delta" if f=="delta" else "fit"
                    gap = numeric(val, value[f], FIT_TOL[endpoint]*(2 if kind=="delta" else 1),
                                  f"{i}/{tag}/{f}", disagreements)
                    if gap is not None:
                        max_diff[kind] = max(gap,max_diff[kind])
            row.update(pairs=pairs, median=mid)
            row["effect_pairs"]=[p for p in PAIRS if pairs[p]["delta"] is not None and abs(pairs[p]["delta"])>=e]
        row["classification"] = name
        if name != observed["classification"]:
            disagreements.append({"field":f"{i}/classification","reference":name,"reported":observed["classification"]})
        counts[name] += 1
        rows.append(row)
    measured = len(rows)-counts["ineligible"]-counts["unmeasured"]
    missed = sum(counts[i] for i in ("opposed","near_zero","attenuated"))
    scalar = None
    reason = None
    if measured < k:
        reason = "too_few_evaluable_blocks"
    elif missed:
        scalar = 1
    elif counts["unmeasured"]:
        reason = "unmeasured_eligible_blocks"
    else:
        scalar = 0
    denominator = {"blocks_total":len(rows),"blocks_eligible":len(rows)-counts["ineligible"],
                   "blocks_evaluable":measured, "blocks_unmeasured":counts["unmeasured"],
                   "blocks_missed_observed":missed}
    for field,value in {**denominator,"missed_present":scalar,"unavailable_reason":reason}.items():
        if summary[field] != value or type(summary[field]) is not type(value):
            disagreements.append({"field":field,"reference":value,"reported":summary[field]})
    return {"entry":entry,"parameters":params,"unit":summary["unit"],"input_sha256":summary["input_sha256"],
            "matched_keys_sha256":meta["matched_keys_sha256"],"matched_positions":n,"union_positions":union,
            "missed_present":scalar,"unavailable_reason":reason,**denominator,"class_counts":dict(counts),
            "fit_tolerance":FIT_TOL[endpoint],"delta_tolerance":2*FIT_TOL[endpoint],
            "max_abs_differences":max_diff,"discrepancies":disagreements,"blocks":rows}


def read_artifact(db, state, rid, item):
    row = db.execute("SELECT namespace,rel_path,sha256,bytes,published FROM artifacts WHERE run_id=? AND artifact_ref=?",
                     (rid,item["artifact_ref"])).fetchone()
    require(row and row["published"] and row["sha256"] == item["sha256"], "artifact publication or digest differs")
    data = (state/"artifacts"/row["namespace"]/row["rel_path"]).read_bytes()
    require(sha256(data).hexdigest()==item["sha256"] and len(data)==item["bytes"], "artifact bytes differ")
    return data


def closing_readout(job, metric_id):
    """Resolve native metric IDs to the independently reconstructed numerical quantity and tolerance."""
    if not job or "evidence" not in job:
        return None
    method, ev = job["method"], job["evidence"]
    if method == "deposited-block-signed@1" and metric_id in ev:
        return ev[metric_id], 0.0
    if method == "deposited-chain-geometry@2" and metric_id == "coverage":
        return ev["coverage_fraction"], 0.0
    if method == "deposited-block-influence@1":
        if metric_id == "coverage":
            return ev["matched_positions"] / ev["union_positions"], 0.0
        if metric_id == "eligible_blocks":
            return len(ev["eligible_blocks"]), 0.0
        for endpoint, tol in FIT_TOL.items():
            if metric_id == "max_"+endpoint:
                peak = ev["largest_absolute_changes"][endpoint]
                return (peak["delta"] if peak else None), 2*tol
    if method == "deposited-block-reading@1":
        for endpoint, tol in FIT_TOL.items():
            for kind, value in ev["recomputed"][endpoint].items():
                if metric_id == kind+"_"+endpoint:
                    return value, tol if kind in ("after","baseline") else 2*tol
    return None


def report(record):
    run = record["run"]
    lines = ["# 独立坐标复核", "", f"Run `{run['run_id']}`，Goal 状态 `{run['goal_status']}`。"
             "只复核了此 Run 中已成功完成并发布的物理 Job。", "",
             "独立路径从原始 mmCIF 的模型 1、实体 1 和标签链 A/B/C 读取坐标，按已执行 Job 的原子选择进行正旋转 SVD 拟合。"
             "省略区段后先对三条链对分别重拟合，随后取各阶段的中位数。角度或 RMSD 的带符号变化均为删后减删前。", ""]
    for j in record["jobs"]:
        ev = j.get("evidence", {})
        lines.extend([f"## {ev.get('entry','')} · {j['method']} · `{j['job_id']}`", ""])
        if j["method"] == "deposited-chain-geometry@2":
            lines.extend([f"全链 `{ev['mask']}` 选择覆盖 {ev['selected_residues']}/{ev['observed_union_residues']} 个观测位置。"
                          "角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。", "",
                          "| 链对 | 旋角（°） | RMSD（Å） |", "|---|---:|---:|"])
            for pair, v in ev["pairs"].items():
                values = " | ".join("缺测" if v[f] is None else f"{v[f]:.6f}" for f in ("angle_deg","rmsd_A"))
                lines.append(f"| {pair} | {values} |")
            lines.append("")
            continue
        if j["method"] == "deposited-block-influence@1":
            lines.extend([f"原件 `{ev['entry']}`，`{ev['mask']}`，合格区段 {len(ev['eligible_blocks'])} 个。"
                          "所有基线、逐区段和分散对照拟合均已独立重算。", ""])
            for ep, peak in ev["largest_absolute_changes"].items():
                if peak:
                    i=peak["index"]
                    lines.append(f"- `{ep}` 的最大链对变化为 {peak['delta']:.6f} {ev['units'][ep]}，链对 "
                                 f"{peak['pair']}，标签区段 {20*i+1}–{20*i+20}。")
            lines.append("")
            continue
        if j["method"] != "deposited-block-signed@1":
            lines.extend(["所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。", ""])
            continue
        pars = ev["parameters"]
        observed = ev["missed_present"]
        lines.extend([f"原子选择 `{pars['mask']}`，指标 `{pars['endpoint']}`（{ev['unit']}）。"
                      f"阈值 e={pars['effect_minimum']}、z={pars['zero_limit']}，最少可评价区段 k={pars['minimum_evaluable']}。"
                      f"合格 {ev['blocks_eligible']}/{ev['blocks_total']} 段，可评价 {ev['blocks_evaluable']} 段，"
                      f"其中 {ev['blocks_missed_observed']} 段属遗漏。独立计算 missed_present={observed if observed is not None else 'null'}。",
                      "", f"全部逐段重算的最大绝对差为单次拟合 {ev['max_abs_differences']['fit']:.3g} {ev['unit']}，"
                      f"变化量 {ev['max_abs_differences']['delta']:.3g} {ev['unit']}。"
                      f"对应容差为 {ev['fit_tolerance']} 和 {ev['delta_tolerance']} {ev['unit']}。"
                      f"分类和数值存在 {len(ev['discrepancies'])} 项超差或不一致。", ""])
        measured = [b for b in ev["blocks"] if b["classification"] not in ("unmeasured","ineligible")]
        chosen = [b for b in measured if b["classification"] in ("near_zero","attenuated","opposed")]
        if not chosen and measured:
            chosen = [max(measured,key=lambda r:max(abs(v["delta"]) for v in r["pairs"].values()))]
        if chosen:
            lines.extend(["| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |",
                          "|---|---:|---:|---:|---:|---|"])
            for b in chosen:
                vals = [b["pairs"][pair]["delta"] for pair in PAIRS] + [b["median"]["delta"]]
                lines.append(f"| {b['interval'][0]}–{b['interval'][1]} | " + " | ".join(f"{v:+.6f}" for v in vals)
                             + f" | {b['classification']} |")
            lines.append("")
        if ev["unavailable_reason"]:
            lines.extend([f"缺测原因 `{ev['unavailable_reason']}`。", ""])
    if "closed" in record:
        c = record["closed"]
        matches = record["matched_masks"]
        if matches["signed_jobs"]:
            lines.extend(["## 同一区段的原子选择复核", "",
                          f"逐个已执行 signed Job 计，共有 {matches['flagged_blocks']} 个遗漏区段读数。"
                          f"其中 {matches['measured_matches']} 个可在同一原件、同一区段、同一指标上与另一原子选择匹配，"
                          f"{matches['no_evaluable_counterpart']} 个匹配区段缺测，"
                          f"{matches['no_counterpart_job']} 个缺少另一原子选择的对应 Job。", ""])
            if matches["rows"]:
                lines.extend(["| 原件 · 区段 | 指标 | 原子选择 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 该 Job 签名分类 |",
                              "|---|---|---|---:|---:|---:|---:|---|"])
                seen = set()
                for row in matches["rows"]:
                    for obs in [row["source"], *row["counterparts"]]:
                        key = (row["entry"], row["index"], row["endpoint"], obs["job_id"])
                        if key in seen or "pair_signed_deltas" not in obs:
                            continue
                        seen.add(key)
                        deltas = [obs["pair_signed_deltas"].get(p) for p in PAIRS] + [obs["median_signed_delta"]]
                        cells = " | ".join("缺测" if d is None else f"{d:+.6f}" for d in deltas)
                        lines.append(f"| {row['entry']} · {row['interval'][0]}–{row['interval'][1]} | {row['endpoint']} | "
                                     f"{obs['mask']} | {cells} | {obs.get('classification','仅有原变化量')} |")
                lines.extend(["", "表中每行对应一个实际 Job。各 Job 的阈值、分母及对应关系见 JSON 的 matched_masks。", ""])
        lines.extend(["## 正式结题", "", f"事件 {c['committed_event']} 采纳决定，事件 {c['closed_event']} 关闭 Goal。"
                      f"该决定显式引用 {len(c['citations'])} 项已交付指标，"
                      f"{sum(v.get('matches') is True for v in c['citations'])} 项与独立复算相符。"
                      "原 Job、原值、单位及复算差值保留在 JSON。", ""])
    lines.extend(["[完整参数、输出摘要与逐区段读数](results.json)"])
    return "\n".join(lines)+"\n"


def verify_run(state, out, run_id=None):
    from acceptance.multi_workflow.independent.supplemental_live import verify_block, verify_geometry
    from acceptance.multi_workflow.independent.matched_masks import matched_comparisons
    state, out = state.resolve(), out.resolve()
    db = sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)
    db.row_factory = sqlite3.Row
    slot = db.execute("""SELECT w.run_id,r.status,g.status AS goal_status FROM p0_workspace_runs w
                      JOIN runs r USING(run_id) JOIN v6_goals g USING(run_id)
                      WHERE ? IS NULL OR w.run_id=?""", (run_id, run_id)).fetchall()
    require(len(slot) == 1, "Select one existing workspace Goal Run with --run-id")
    run, = slot
    rid = run["run_id"]
    reference_files = (Path(__file__), HERE/"supplemental_live.py", HERE/"matched_masks.py",
                       REPO/"acceptance/v623/study/independent_geometry.py",
                       REPO/"acceptance/v623/study/independent_compare.py",
                       REPO/"acceptance/v6/reviewer/independent_geometry.py")
    hashes = {str(f.relative_to(REPO)):sha256(f.read_bytes()).hexdigest() for f in reference_files}
    prior_path = out/"results.json"
    prior = json.loads(prior_path.read_bytes()) if prior_path.is_file() else {}
    require(not prior or prior.get("run",{}).get("run_id")==rid,
            "Output directory contains evidence from a different Run")
    warm = ({i["job_id"]:i for i in prior.get("jobs",[])} if prior.get("run",{}).get("run_id")==rid and
            prior.get("reference_code_sha256")==hashes else {})
    jobs = db.execute("""SELECT j.job_id,j.capability_id,j.capability_version,m.admission_json,m.completion_json,
                    (SELECT min(e.event_id) FROM events e WHERE e.run_id=j.run_id AND e.subject_id=j.job_id
                     AND e.kind='job.transition' AND e.new_state='SUCCEEDED') AS completed_event
                    FROM jobs j JOIN p0_execution_manifests m USING(job_id) WHERE j.run_id=? AND j.status='SUCCEEDED'
                    AND json_extract(m.completion_json,'$.status')='SUCCEEDED' ORDER BY completed_event,j.job_id""",
                    (rid,)).fetchall()
    if not jobs and run["goal_status"]!="CLOSED":
        db.close()
        print("Waiting for completed CPU jobs.")
        return
    items, projections = [], []
    for j in jobs:
        a, c = json.loads(j["admission_json"]), json.loads(j["completion_json"])
        method = f"{j['capability_id']}@{j['capability_version']}"
        require(j["completed_event"] is not None and c["exit_code"]==0 and c["started_at"] and c["finished_at"],
                "missing physical Job completion")
        job = {"job_id":j["job_id"],"method":method,"branch":a["owner"].get("branch_id"),
               "completed_event":j["completed_event"],"finished_utc":c["finished_at"]}
        params = a["parameters"]
        job["parameters"] = params
        outputs = {o["output_name"]:o for o in c["outputs"]}
        job["outputs"]={key:{f:o[f] for f in ("artifact_ref","sha256","bytes")} for key,o in outputs.items()}
        if method=="deposited-block-reading@1":
            projections.append((job,a,outputs))
            continue
        if method not in ("deposited-block-signed@1","deposited-block-influence@1","deposited-chain-geometry@2"):
            items.append({**job,"verification":"outside coordinate-method scope"})
            continue
        original, = a["inputs"]
        require(original["role"] == "structure", "direct structure missing")
        job["original"] = {key:original[key] for key in ("artifact_ref","sha256","bytes")}
        # Complete and verified named publication is required before opening or fitting the input.
        docs = {k:json.loads(read_artifact(db,state,rid,v)) for k,v in outputs.items()}
        require(all(doc["input_sha256"] == original["sha256"] and doc["mask"] == params["mask"]
                    for doc in docs.values()), "output source or mask differs from sealed Job")
        raw = read_artifact(db,state,rid,original)
        cached = warm.get(job["job_id"], {})
        if cached and all(cached.get(k) == value for k,value in job.items()) and "evidence" in cached:
            answer = cached["evidence"]
        elif method=="deposited-block-signed@1":
            answer = verify_signed(raw, params, docs["signed-summary.json"], docs["signed-blocks.json"])
        elif method=="deposited-block-influence@1":
            answer = verify_block(raw, docs["block-influence.json"])
        else:
            answer = verify_geometry(raw, docs["geometry.json"])
        items.append({**job,"evidence":answer})
    tables={t["outputs"]["block-influence.json"]["artifact_ref"]:t for t in items if t["method"]=="deposited-block-influence@1"}
    for job,a,outputs in projections:
        pin, = a["inputs"]
        require(pin["role"]=="table" and pin["artifact_ref"] in tables, "cell needs a completed same-Run table")
        parent=tables[pin["artifact_ref"]]
        cell=json.loads(read_artifact(db,state,rid,outputs["cell.json"]))
        require(parent["outputs"]["block-influence.json"]["sha256"]==cell["input_sha256"]==pin["sha256"], "wrong cell source")
        require(cell["material_sha256"]==parent["original"]["sha256"], "wrong cell material")
        index, pair = a["parameters"]["block_index"], a["parameters"]["pair"]
        require((cell["block_index"],cell["pair"])==(index,pair), "cell scope differs")
        # Validate each projected value against its already checked original table row.
        tab=json.loads(read_artifact(db,state,rid,pin))
        block=next((r for r in tab["blocks"] if r["index"]==index), {})
        pair_row=block.get("pairs",{}).get(pair,{})
        independent = parent["evidence"]
        independently_omitted = next((b for b in independent["blocks"] if b["index"]==index), {})
        values, recomputed = {}, {}
        for ep in FIT_TOL:
            readings={"baseline":tab["baseline"][pair][ep],"after":pair_row.get(ep),"delta":pair_row.get("delta_"+ep),
                "median_delta":block.get("median",{}).get(ep,{}).get("delta"),
                "control_max":pair_row.get(ep+"_assessment",{}).get("control_max")}
            for f,v in readings.items(): require(v==cell[f+"_"+ep], "cell projection differs")
            values[ep]=readings
            mapped = independently_omitted.get("metrics",{}).get(ep,{})
            p = mapped.get("pairs",{}).get(pair,{})
            recomputed[ep] = {"baseline":independent["baseline"][pair][ep], "after":p.get("after"),
                              "delta":p.get("absolute_change"), "median_delta":mapped.get("median",{}).get("absolute_change"),
                              "control_max":p.get("control_max")}
        items.append({**job,"evidence":{"entry":cell["entry"],"mask":cell["mask"],"pair":pair,"block_index":index,
                    "source_table_job":parent["job_id"],"values":values,"recomputed":recomputed}})
    out.mkdir(parents=True, exist_ok=True)
    record={"run":dict(run),"reader":"Bio.PDB.MMCIF2Dict "+Bio.__version__,"solver":"NumPy SVD "+np.__version__,"reference_code_sha256":hashes,"jobs":items}
    if run["goal_status"]=="CLOSED":
        from gx_core.p0.research_report import completed_close, closing_decision
        record["matched_masks"]=matched_comparisons(items)
        close=completed_close(db,rid)
        record["closed"]={k:close[k] for k in ("decision_ref","goal_version","committed_event","closed_event","closed_utc")}
        final = closing_decision(db,rid,close)
        checked = {j["job_id"]:j for j in items}
        citations = []
        for item in final["used_endpoints"]:
            m = item["reading"]
            cite = {k:item[k] for k in ("metric_ref","event_id","source_ref")}
            cite.update({k:m.get(k) for k in ("job_id","artifact_ref","metric_id","unit","value","display","scope") if k in m})
            observation = closing_readout(checked.get(m.get("job_id")), m["metric_id"])
            if observation is not None:
                value, tolerance = observation
                errors = []
                gap = numeric(value, m.get("value"), tolerance, m["metric_id"], errors)
                cite.update(recomputed=value, absolute_difference=gap, tolerance=tolerance, matches=not errors)
            citations.append(cite)
        record["closed"]["citations"]=citations
    db.close()
    tmp=out/"results.tmp"
    tmp.write_text(json.dumps(record,ensure_ascii=False,allow_nan=False,indent=2)+"\n")
    tmp.replace(out/"results.json")
    (out/"READINGS.md").write_text(report(record))
    verified = sum("evidence" in item for item in items)
    print(f"{rid} {run['goal_status']}: independently checked {verified}/{len(items)} completed Jobs")


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="GX state directory containing gx.db and artifacts/")
    parser.add_argument("--output", type=Path, required=True, help="directory for results.json and READINGS.md")
    parser.add_argument("--run-id", help="select the scientific Run when the database has multiple Goals")
    args = parser.parse_args()
    verify_run(args.state, args.output, args.run_id)
