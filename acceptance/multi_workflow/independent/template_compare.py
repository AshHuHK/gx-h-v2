"""Check coordinate-model dependence between two already measured, independently verified depositions."""
import argparse
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys

from Bio.PDB.MMCIF2Dict import MMCIF2Dict

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "gx-h")]
from acceptance.multi_workflow.independent.live_verify import FIT_TOL, PAIRS, read_artifact, require
from acceptance.multi_workflow.independent.template_geometry import compare_template_geometry
from gx_core.p0.research_evidence import _claims

SIGNED = "deposited-block-signed@1"
WHOLE = "deposited-chain-geometry@2"
METADATA = (
    "_entry.id", "_exptl.method", "_refine.ls_d_res_high", "_refine.pdbx_starting_model",
    "_pdbx_initial_refinement_model.accession_code", "_em_3d_fitting.ref_protocol",
    "_em_3d_fitting.ref_space", "_em_3d_fitting_list.pdb_entry_id",
    "_em_3d_fitting_list.accession_code", "_em_3d_fitting_list.pdb_chain_id",
    "_em_3d_fitting_list.chain_id", "_em_3d_reconstruction.resolution",
    "_em_3d_reconstruction.resolution_method", "_pdbx_audit_revision_history.revision_date",
    "_pdbx_audit_revision_history.major_revision", "_pdbx_audit_revision_history.minor_revision")


def load_measured(state, snapshot, entry):
    record = json.loads(snapshot.read_bytes())
    rid = record["run"]["run_id"]
    jobs = [j for j in record["jobs"] if j["method"] in (SIGNED, WHOLE)
            and j.get("evidence", {}).get("entry") == entry]
    require(jobs, f"No independently checked, completed Job for {entry}")
    db = sqlite3.connect((state.resolve()/"gx.db").as_uri()+"?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    raw = None
    for job in jobs:
        if job["method"] == SIGNED:
            require(job["evidence"].get("discrepancies")==[], "Discrepant signed Job cannot support a cross-material comparison")
        sealed = db.execute("""SELECT j.status,j.capability_id,j.capability_version,m.admission_json,m.completion_json
                             FROM jobs j JOIN p0_execution_manifests m USING(job_id)
                             WHERE j.run_id=? AND j.job_id=?""", (rid, job["job_id"])).fetchone()
        require(sealed and sealed["status"] == "SUCCEEDED", "Missing original successful Job")
        admission, completion = json.loads(sealed["admission_json"]), json.loads(sealed["completion_json"])
        require(completion["status"] == "SUCCEEDED" and completion["exit_code"] == 0, "Missing sealed completion")
        require(job["method"] == sealed["capability_id"]+"@"+sealed["capability_version"]
                and admission["parameters"] == job["parameters"], "Method or selection in snapshot differs")
        pin, = admission["inputs"]
        require(pin["role"] == "structure" and pin["sha256"] == job["original"]["sha256"], "Wrong original")
        for item in completion["outputs"]:
            require(job["outputs"][item["output_name"]]["sha256"] == item["sha256"], "Published output mismatch")
            output = json.loads(read_artifact(db,state,rid,item))
            require(output["entry"] == entry and output["input_sha256"] == pin["sha256"]
                    and output["mask"] == job["parameters"]["mask"], "Published selection mismatch")
            if item["output_name"] == "signed-summary.json":
                require(all(output[k] == job["evidence"][k] for k in
                            ("missed_present","blocks_evaluable","blocks_missed_observed","unavailable_reason")),
                        "Independent summary differs from its published signed Job")
            if item["output_name"] == "signed-blocks.json":
                original = {r["block_index"]:r["classification"] for r in output["blocks"]}
                independent = {r["index"]:r["classification"] for r in job["evidence"]["blocks"]}
                require(original == independent, "Independent classes differ from their published signed Job")
        data = read_artifact(db,state,rid,pin)
        require(raw is None or raw == data, "Multiple original revisions for one material")
        raw = data
    digest = sha256(raw).hexdigest()
    selected = []
    for claim in _claims(db,rid)[0]:
        reg = claim["registration"]
        method = reg["prediction"].get("method", {})
        if method.get("capability") != SIGNED:
            continue
        last = claim["assessments"][-1]
        for item in last.get("material_coverage",[]):
            if item.get("sha256") == digest:
                selected.append({"claim_id":claim["hypothesis_id"], "method":method["capability"],
                                 "parameters":method["parameters"], "metric":method["metric_id"],
                                 **{k:item.get(k) for k in ("registration_event_id","endpoint_first_available_event_id",
                                      "qualification","known_at_registration")}})
    db.close()
    cif = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    public = {"entry":entry, "run_id":rid, "sha256":digest,
              "metadata":{k:cif[k] for k in METADATA if k in cif}, "GX_registered_scope":selected,
              "completed_jobs":[{k:j[k] for k in ("job_id","method","parameters","completed_event","outputs")} for j in jobs]}
    return public, raw, jobs


def signed_comparison(left, right):
    endpoint = left["parameters"]["endpoint"]
    by_index = {b["index"]:b for b in left["evidence"]["blocks"]}
    pairs, medians, changed = [], [], []
    same_eligibility = True
    effect = left["parameters"]["effect_minimum"]
    def transition(block):
        pairs = block.get("pairs",{})
        pair_deltas = {p:v["delta"] for p,v in pairs.items()}
        medians = {}
        for stage in ("before","after"):
            midpoint = block.get("median",{}).get(stage)
            medians[stage] = [p for p,v in pairs.items() if midpoint is not None and v.get(stage) == midpoint]
        return {"classification":block["classification"],"pair_signed_deltas":pair_deltas,
                "abs_pair_delta_minus_effect":{p:None if v is None else abs(v)-effect for p,v in pair_deltas.items()},
                "median_pairs_by_stage":medians,"median_signed_delta":block.get("median",{}).get("delta")}
    for after in right["evidence"]["blocks"]:
        before = by_index.get(after["index"])
        if before is None or after["interval"] != before["interval"]:
            same_eligibility = False
            continue
        same_eligibility &= (after["matched_in_interval"], after["retained"]) == (before["matched_in_interval"], before["retained"])
        if before["classification"] != after["classification"]:
            changed.append({"interval":after["interval"],"template":before["classification"],"target":after["classification"],
                            "template_details":transition(before),"target_details":transition(after)})
        if "pairs" not in before or "pairs" not in after:
            continue
        for name in (*PAIRS, "median"):
            t = (before["median"] if name == "median" else before["pairs"][name])["delta"]
            u = (after["median"] if name == "median" else after["pairs"][name])["delta"]
            if t is None or u is None:
                continue
            (medians if name == "median" else pairs).append({"interval":after["interval"],"pair":name,
                                   "template":t,"target":u,"absolute_difference":abs(t-u)})
    same_eligibility &= len(by_index) == len(right["evidence"]["blocks"])
    tol = 2*FIT_TOL[endpoint]
    def summary(values):
        return {"compared":len(values), "exceed_tolerance":sum(r["absolute_difference"]>tol for r in values),
                "largest":max(values,key=lambda r:r["absolute_difference"],default=None)}
    return {"template_job":left["job_id"],"target_job":right["job_id"],"parameters":left["parameters"],
            "nominal_labels":"A:A, B:B, C:C", "unit":left["evidence"]["unit"],"delta_tolerance":tol,
            "same_block_counts_and_retained_counts":same_eligibility,
            "template_evaluable_blocks":left["evidence"]["blocks_evaluable"], "target_evaluable_blocks":right["evidence"]["blocks_evaluable"],
            "template_missed_blocks":left["evidence"]["blocks_missed_observed"], "target_missed_blocks":right["evidence"]["blocks_missed_observed"],
            "template_class_counts":left["evidence"]["class_counts"],"target_class_counts":right["evidence"]["class_counts"],
            "template_missed_present":left["evidence"]["missed_present"],"target_missed_present":right["evidence"]["missed_present"],
            "changed_classifications":changed,"pair_deltas":summary(pairs),"median_deltas":summary(medians)}


def render(result):
    template, target = result["template"], result["target"]
    meta = target["metadata"]
    def field(key):
        return ", ".join(meta.get(key, ["未记录"]))
    lines = [f"# {target['entry']} 与 {template['entry']} 的模板关系", "",
             f"{target['entry']} 的沉积元数据记录拟合模型为 {field('_em_3d_fitting_list.pdb_entry_id')}，"
             f"拟合类型为 {field('_em_3d_fitting.ref_protocol')}，拟合空间为 {field('_em_3d_fitting.ref_space')}，"
             f"EM 重建的报告分辨率为 {field('_em_3d_reconstruction.resolution')} Å。"
             f"拟合表的模板链和目标链字段分别为 {field('_em_3d_fitting_list.pdb_chain_id')} 和 "
             f"{field('_em_3d_fitting_list.chain_id')}。下表使用这两次 Run 封存的具体 mmCIF 修订版本。", "",
             "| 两边已测原子选择 | 每链共有原子 | 三链总点数 | 最优链映射 | 共同刚体 RMSD（Å） | 最大残差（Å） | 十进制坐标严格全等 |",
             "|---|---:|---:|---|---:|---:|---|"]
    for mask,v in result["coordinate_comparison"]["masks"].items():
        if v["status"] != "measured":
            lines.append(f"| {mask} | 0 | 0 | — | — | — | 无法判定 |")
            continue
        exact = "是" if v["exact_full_mask"] else "否"
        lines.append(f"| {mask} | {v['keys']['intersection']['atoms_per_chain']} | {v['matched_points_all_chains']} | "
                     f"{v['best_mapping']} | {v['best_rmsd_A']:.8f} | {v['best_max_residual_A']:.8f} | {exact} |")
    lines.extend(["", "每种链映射对三条链的全部共有原子拟合一次旋转和平移。刚体残差使用 Å。"
                  "严格全等另以原文件十进制坐标的精确点积和手性判断。"
                  "JSON 列出六种链映射、原子键交集、坐标打印精度及舍入界。", "",
                  "| 两边已执行的签名 | e / z / k | 可比链对变化数 | 最大链对变化差 | 最大中位变化差 | 原有变化容差 | 分类改变区段数 | 两边遗漏数/可评价段数 | 两边 missed_present |",
                  "|---|---|---:|---:|---:|---:|---:|---|---|"])
    for obs in result["measured_signed_endpoints"]:
        p = obs["parameters"]
        def largest(which):
            row = obs[which]["largest"]
            return "缺测" if row is None else f"{row['absolute_difference']:.7g} {obs['unit']}"
        lines.append(f"| {p['mask']} · {p['endpoint']} | {p['effect_minimum']} / {p['zero_limit']} / {p['minimum_evaluable']} | "
                     f"{obs['pair_deltas']['compared']} | {largest('pair_deltas')} | {largest('median_deltas')} | "
                     f"{obs['delta_tolerance']} {obs['unit']} | {len(obs['changed_classifications'])} | "
                     f"{obs['template_missed_blocks']}/{obs['template_evaluable_blocks']} → "
                     f"{obs['target_missed_blocks']}/{obs['target_evaluable_blocks']} | "
                     f"{obs['template_missed_present']} / {obs['target_missed_present']} |")
    changes = [(s,c) for s in result["measured_signed_endpoints"] for c in s["changed_classifications"]]
    if changes:
        lines.extend(["", "## 跨沉积实际改变的区段分类", "",
                      "原签名按照全精度读数与效应阈值比较。两次测量的差值落在拟合复核容差内，仍可能跨过分类阈值。", ""])
        for obs, c in changes:
            p = obs["parameters"]
            lines.extend([f"### {c['interval'][0]}–{c['interval'][1]} · {p['mask']} · {p['endpoint']}", "",
                          f"效应阈值 e={p['effect_minimum']} {obs['unit']}，z={p['zero_limit']} {obs['unit']}，"
                          f"k={p['minimum_evaluable']}。原 Job 分别为 `{obs['template_job']}` 与 `{obs['target_job']}`。", "",
                          "| 沉积 | 链对 | 带符号变化 | 变化绝对值减效应阈值 | 区段判定 |",
                          "|---|---|---:|---:|---|"])
            for side in ("template","target"):
                d = c[side+"_details"]
                for pair, v in d["pair_signed_deltas"].items():
                    if v is not None:
                        lines.append(f"| {result[side]['entry']} | {pair} | {v:+.17g} | "
                                     f"{d['abs_pair_delta_minus_effect'][pair]:+.12g} | {d['classification']} |")
            lines.append("")
            for side in ("template","target"):
                d = c[side+"_details"]
                chains = d["median_pairs_by_stage"]
                lines.append(f"- {result[side]['entry']} 的删前中位链对为 {', '.join(chains['before'])}，"
                             f"删后为 {', '.join(chains['after'])}，中位变化为 {d['median_signed_delta']} {obs['unit']}。")
            lines.append("")
    lines.extend(["", f"## {target['entry']} 在 GX 中首次可得的证据", "",
                  "| 已采用的签名 | 登记事件 | 同一原件的 GX 首次可得事件 | 登记时已知 |",
                  "|---|---:|---:|---|"])
    for reg in target["GX_registered_scope"]:
        p = reg["parameters"]
        lines.append(f"| {p['mask']} · {p['endpoint']} | {reg['registration_event_id']} | "
                     f"{reg['endpoint_first_available_event_id']} | {reg['known_at_registration']} |")
    lines.extend(["", f"这些事件属于 Run `{target['run_id']}` 封存的原件 SHA-256 `{target['sha256']}`。"
                  f"比较模板的已测原件 SHA-256 为 `{template['sha256']}`。"
                  "GX 时序按各自原件审定。科学上的独立性还取决于沉积记载的模板和实际坐标关系。", ""])
    masks = result["coordinate_comparison"]["masks"].values()
    measured = [v for v in masks if v["status"]=="measured"]
    if (template["entry"] in meta.get("_em_3d_fitting_list.pdb_entry_id", [])
            and measured and all(v["identical_key_sets"] and v["best_max_residual_A"] <= v["two_input_rounding_bound_A"]
                                 for v in measured)):
        lines.extend([f"{target['entry']} 明确使用 {template['entry']} 作为拟合模板，"
                      "实测掩码的最优整组三链刚体残差均在坐标打印舍入界内。"
                      "这组比较提供模板依赖坐标上的复测。独立结构重复的证据需另计。", ""])
    return "\n".join(lines)


def main(args):
    a,raw_a,jobs_a=load_measured(args.template_state,args.template_evidence,args.template_entry)
    b,raw_b,jobs_b=load_measured(args.target_state,args.target_evidence,args.target_entry)
    masks=sorted({j["parameters"]["mask"] for j in jobs_a} & {j["parameters"]["mask"] for j in jobs_b})
    geometry=compare_template_geometry(raw_a,raw_b,a["entry"],b["entry"],masks)
    endpoints=[signed_comparison(i,j) for i in jobs_a if i["method"]==SIGNED for j in jobs_b
               if j["method"]==SIGNED and i["parameters"] == j["parameters"]]
    out={"schema":"gx.independent-template-comparison/1","template":a,"target":b,"coordinate_comparison":geometry,
         "measured_signed_endpoints":endpoints,
         "comparison_code_sha256":{p.name:sha256(p.read_bytes()).hexdigest() for p in
                                   (Path(__file__),Path(__file__).with_name("template_geometry.py"))},
         "scope":"Only observed combinations in both Runs. Delta comparisons follow deposited chain labels. GX first availability refers to the corresponding immutable target SHA in that Run."}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(out,indent=2,ensure_ascii=False,allow_nan=False)+"\n")
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(render(out))


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    for side in ("template","target"):
        cli.add_argument("--"+side+"-state",required=True,type=Path)
        cli.add_argument("--"+side+"-evidence",required=True,type=Path)
        cli.add_argument("--"+side+"-entry",required=True)
    cli.add_argument("--output",required=True,type=Path)
    cli.add_argument("--report",type=Path,help="optional human-readable Markdown report")
    main(cli.parse_args())
