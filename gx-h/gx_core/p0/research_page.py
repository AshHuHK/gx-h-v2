"""A short reading of the accepted conclusion, original predictions and subsequent decisions."""
from collections import Counter
import re


def _excerpt(text, limit):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    part = text[:limit]
    sentences = list(re.finditer(r"[。！？]|[.!?](?=\s|$)", part))
    if sentences:
        return part[:sentences[-1].end()] + " …"
    return part.rstrip() + "…"


def _cell(text):
    return " ".join(str(text).split()).replace("|", r"\|")


def _observations(prediction, assessment, output_labels, materials, zh):
    observations = []
    metric = (prediction.get("method") or prediction.get("external_endpoint") or {}).get("metric_id")
    for item in assessment.get("results", []):
        label = output_labels.get(item["artifact_ref"]) or ", ".join(
            materials[sha] for sha in item.get("matched_data_sha256", []) if sha in materials)
        value = str(item["value"])
        if item.get("unit") and item["unit"] != "1":
            value += " " + item["unit"]
        counts = item.get("denominators") or {}
        if "blocks_missed_observed" in counts and "blocks_evaluable" in counts:
            n, d = counts["blocks_missed_observed"], counts["blocks_evaluable"]
            fraction = f"{n}/{d} " + ("段遗漏" if zh else "evaluable blocks missed")
            if metric == "blocks_missed_observed":
                value = fraction
            elif metric == "missed_present" and item["value"] in (0, 1):
                value = ("有遗漏" if item["value"] else "无遗漏") if zh else ("Present" if item["value"] else "Absent")
                value += f" ({fraction})"
            else:
                value += f" ({fraction})"
        elif "denominator" in counts:
            num = counts.get("numerator")
            den = counts["denominator"]
            value += f" ({num}/{den})" if num is not None else (f" (分母 {den})" if zh else f" (denominator {den})")
        observations.append(f"{label} {value}" if label else value)
    if observations:
        return _excerpt(", ".join(observations), 220)
    gaps = {g["kind"] for g in assessment.get("gaps", [])}
    if "out_of_scientific_scope" in gaps:
        return "登记范围与输出不匹配" if zh else "The output did not match the registered scope"
    if "unavailable_metric" in gaps:
        return "端点缺测" if zh else "Endpoint unavailable"
    return "无合格读数" if zh else "No qualifying reading"


def page(result):
    zh = bool(re.search(r"[\u3400-\u9fff]", result["goal"]))
    def tr(en, cn):
        return cn if zh else en

    final = result["final_decision"]["rendered"]
    close = result["closure"]
    labels = {o["artifact_ref"]:o.get("summary", {}).get("entry") for j in result["jobs"] for o in j["outputs"]}
    materials = {}
    for i, item in enumerate(result["materials"], 1):
        source = next((p["source"] for p in item.get("provenance", []) if p.get("source")), None)
        materials[item.get("sha256")] = source or item.get("name") or tr(f"Material {i}", f"材料 {i}")
    titles = {"supported":tr("Supported", "支持"), "opposed":tr("Contradicted", "反对"),
              "mixed":tr("Mixed evidence", "证据不一致"), "undetermined":tr("Undetermined", "未定")}
    lines = [tr("# Research findings", "# 研究结论"), ""]
    question = next((s.strip() for s in result["goal"].splitlines() if s.strip() and not s.lstrip().startswith("#")), "")
    if question:
        lines.extend(["> " + _excerpt(question, 160 if zh else 220), ""])
    for text in dict.fromkeys([final["rationale"], final["reason"]]):
        if text:
            lines.extend([_excerpt(text, 1000 if zh else 1600), ""])
    predictions = result["predictions"]
    if predictions:
        lines.extend([tr("## Original predictions", "## 原始预测"), "",
                      tr("| Prediction | Verdict | Observed |", "| 登记预测 | 原判决 | 实测 |"), "|---|---|---|"])
        # When many hypotheses compete for space, keep counterexamples and unresolved predictions visible.
        priority = {"opposed":0, "mixed":1, "undetermined":2, "supported":3}
        selected = sorted(predictions, key=lambda p: priority.get(p["assessments"][-1].get("direction"), 2))[:8]
        for p in sorted(selected, key=lambda item: item["registration"]["event_id"]):
            assessment = p["assessments"][-1]
            verdict = titles.get(assessment.get("direction"), tr("Undetermined", "未定"))
            if p["classification"] in ("historical_replay", "contains_retrospective_evidence"):
                verdict += tr(" (includes prior evidence)", "（含登记前证据）")
            criterion = p["registration"]["event_id"]
            observation = _observations(p["prediction"], assessment, labels, materials, zh)
            if any(revision["prediction"] != p["prediction"] for revision in p.get("revisions", [])):
                verdict += tr(" · later rule revised", " · 后续判据有修订")
            lines.append(f"| {criterion} · {_cell(_excerpt(p['statement'], 220))} | {verdict} | {_cell(observation)} |")
        if len(predictions) > len(selected):
            lines.extend(["", tr(f"The full record lists all {len(predictions)} predictions.",
                                 f"完整记录列出全部 {len(predictions)} 项预测。")])
        revisions = sum(len(p.get("revisions", [])) for p in predictions)
        if revisions:
            lines.extend(["", tr(f"Later registration versions and their assessments are retained ({revisions}).",
                                 f"完整记录保留 {revisions} 个后续登记版本及逐次评估。")])
        lines.append("")
    else:
        lines.extend([tr("No registered prediction was recorded.", "本题未登记预测。"), ""])

    actions = result["post_read_actions"]
    if actions:
        lines.extend([tr("## Decisions after reading results", "## 读到结果后的决定"), ""])
        # Actions with a commissioned measurement or a change of route have priority over an interim wait.
        chosen = sorted(actions, key=lambda a:not bool(a["operations"]))[:4]
        for action in sorted(chosen, key=lambda a:a["event_id"]):
            reports = ", ".join(map(str, action["report_events"]))
            sources = tr(f"cited reports {reports}", f"引用报告 {reports}") if reports else tr("used readings", "引用量测")
            effect = action["objectives"][0] if action["objectives"] else action["rendered_rationale"]
            n = sum(bool(j["job_id"]) for j in action["jobs"])
            commissioned = tr(f", {n} Jobs launched", f"，启动 {n} 项作业") if n else ""
            lines.append(f"- {action['event_id']} ({sources}{commissioned}) · {_excerpt(effect, 155 if zh else 220)}")
        lines.append("")

    if final["unresolved"]:
        lines.extend([tr("## Scope and open questions", "## 适用范围与未决问题"), ""])
        for item in list(dict.fromkeys(final["unresolved"]))[:5]:
            lines.append("- " + _excerpt(item, 210))
        lines.append("")

    rejected = result["rejected_decisions"]
    if rejected:
        reasons = Counter(r["reason"] for r in rejected)
        known = {"comparison_action_ambiguous":tr("ambiguous comparison", "比较行动指向不明确"),
                 "endpoint_ambiguous":tr("ambiguous endpoint", "端点指向不明确"),
                 "endpoint_use_unspecified":tr("endpoint use unspecified", "未说明端点用途"),
                 "SCOPE_DENIED":tr("outside the assigned scope", "超出任务范围"),
                 "STALE_REVISION":tr("outdated revision", "修订版本已过时"),
                 "SCHEMA_INVALID":tr("invalid submission fields", "提交字段不符合要求")}
        reasons_text = tr(", ", "、").join(f"{known.get(k, k.replace('_', ' ').lower())} ×{n}" for k,n in reasons.most_common(3))
        lines.extend([tr(f"{len(rejected)} decision drafts were rejected. Frequent reasons included {reasons_text}.",
                         f"{len(rejected)} 份决策草稿被拒。常见原因包括{reasons_text}。"), ""])
    event, closed = close["committed_event"], close["closed_event"]
    lines.append(tr(f"Decision {event} closed this study at event {closed}.", f"结题决定 {event} 已在事件 {closed} 生效。")
                 + " " + tr("[Full measurements, decision history and sources](result.json)",
                            "[完整量测、修订历史与来源](result.json)"))
    fallback = tr("Unresolved question (reference in result.json)", "未能还原的问题（索引见 result.json）")
    return re.sub(r"gap:[0-9a-f]{64}", lambda m:result["final_decision"].get("gap_questions", {}).get(m[0], fallback),
                  "\n".join(lines)) + "\n"
