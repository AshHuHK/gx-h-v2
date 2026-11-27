"""A short reading of the accepted conclusion, original predictions and subsequent decisions."""
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


_SENTENCE = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+")
_RECOMMEND = re.compile(r"须|应当|应单列|应分|优先|分别报告|建议|recommend|should|report separately", re.I)
_CONTRAST = re.compile(r"反例|反驳|推翻|衰减|仅在|无法|反向|contradict|counterexample|only when|opposite", re.I)
_MEASURE = re.compile(r"\d+\.\d+|\d+/\d+|\d+\s*(?:Å|°|deg|%)")
_NARROWED = re.compile(r"依预登记收窄|撤回|推翻|counterexample|we (?:narrowed|withdrew|rejected)", re.I)
_ACTION = re.compile(r"因此|故在|据此|这使|重新安排|另行登记|补读|追加|改用|转而|停止|收窄|委派|"
                     r"therefore|we (?:will|chose|selected|stopped|narrowed)|follow.up", re.I)


def _finding(text, limit):
    """Keep a contiguous passage from one paragraph of the Lead's conclusion."""
    if len(text) <= limit:
        return text.strip()
    passages = []
    for paragraph in re.split(r"\n\s*\n", text):
        sentences = [s.strip() for s in _SENTENCE.split(paragraph) if s.strip()]
        for i, sentence in enumerate(sentences):
            start = i
            while start > max(0, i-2) and len(" ".join(sentences[start-1:i+1])) <= limit:
                start -= 1
            passage = re.sub(r"(?<=[。！？]) +", "", " ".join(sentences[start:i+1]))
            advice = bool(_RECOMMEND.search(sentence))
            measured = bool(_MEASURE.search(passage))
            revised = bool(_NARROWED.search(sentence)) and start < i
            passages.append(((revised, advice and measured, advice, bool(_CONTRAST.search(sentence)) and measured, measured), passage))
    return _excerpt(max(passages, key=lambda p:p[0])[1], limit) if passages else ""


def _target(item, zh):
    p = item["prediction"]
    method = p.get("method") or p.get("external_endpoint") or {}
    metric = method.get("metric_id")
    parameters = method.get("parameters", {})
    if metric in ("missed_present", "blocks_missed_observed") and p.get("operator") == "eq" and parameters.get("endpoint"):
        mask = {"common_ca":"Cα", "common_backbone":("共同主链" if zh else "common backbone")}
        endpoint = {"rmsd_A":"RMSD", "angle_deg":("拟合角" if zh else "fit angle")}
        target = p["threshold"]
        if metric == "missed_present" and target in (0, 1):
            rule = (("有遗漏" if target else "无遗漏") if zh else ("a miss" if target else "no misses"))
        else:
            rule = f"{target} " + ("段遗漏" if zh else "blocks missed")
        atom_selection = parameters.get("mask", "")
        return " ".join(filter(None, [mask.get(atom_selection, atom_selection), endpoint.get(parameters["endpoint"], parameters["endpoint"]), rule]))
    statement = item["statement"]
    if metric in ("pair_delta_A", "median_delta_A", "pair_delta_deg", "median_delta_deg", "n_contact_residue_pairs") and "block_index" in parameters:
        interval = re.search(r"[A-Z][a-z]{2}\d+[–—-][A-Z][a-z]{2}\d+", statement)
        window = interval[0] if interval else ("窗索引 " if zh else "block index ") + str(parameters["block_index"])
        if metric.startswith("median_"):
            pair = ""
        else:
            labels = set(re.findall(r"(?<![A-Za-z0-9])([A-Z0-9]/[A-Z0-9])(?![A-Za-z0-9])", statement))
            pair = next(iter(labels)) if len(labels) == 1 else ("链对 " if zh else "pair ") + str(parameters.get("pair", ""))
        if metric == "n_contact_residue_pairs":
            quantity = "接触残基对数" if zh else "contacting residue pairs"
        else:
            mask = {"common_ca":"Cα","common_backbone":("共同主链" if zh else "common backbone")}.get(parameters.get("mask"), "")
            endpoint = ("角度" if zh else "angle") if metric.endswith("_deg") else "RMSD"
            if metric.startswith("median_"):
                endpoint = (f"(链对 {endpoint} 中位数)" if zh else f"(median pair {endpoint})")
            quantity = f"{mask} Δ{endpoint}".strip()
        statement = f"{window} · " + " ".join(filter(None, (pair, quantity)))
    else:
        statement = _excerpt(statement, 50 if zh else 105)
    op = {"eq":"=", "ge":"≥", "gt":">", "le":"≤", "lt":"<"}.get(p.get("operator"), p.get("operator"))
    if op is not None and "threshold" in p:
        unit = method.get("unit")
        rule = f"{op} {p['threshold']}" + (f" {unit}" if unit and unit != "1" else "")
        statement += f" ({rule})"
    return statement


def _action_rank(action, metric_methods):
    jobs = any(j["job_id"] for j in action["jobs"])
    ops = {op["op"] for op in action["operations"]}
    changed = jobs or bool(action.get("claim_revisions")) or bool(ops.intersection(("stop_branch","cancel_node","open_branch")))
    enacted = "ENACTED" in action.get("report_statuses", {}).values()
    methods = {metric_methods[m] for m in action["metric_refs"] if m in metric_methods}
    return (changed, len(methods) > 1, enacted, jobs, bool(methods), action["event_id"])


def _action_text(action, limit):
    sentences = [s.strip() for s in _SENTENCE.split(action["rendered_rationale"]) if s.strip()]
    if action["objectives"] and any(j["job_id"] for j in action["jobs"]):
        commissioned = re.compile(r"另行登记|补读|各测一次|we commissioned", re.I)
        choice = next((s for s in sentences if commissioned.search(s) and len(s) <= limit and
                       not re.search(r"若|如果|\bif\b", s, re.I)), None)
        if choice:
            return choice
        question = _SENTENCE.split(action["objectives"][0])[0].strip()
        if len(question) > limit and "是否" in question and "，" in question:
            head, ask = question.split("是否", 1)
            clause = ask.split("，", 1)[0]
            if clause and len(head + clause) + 3 <= limit:
                return head + "是否" + clause.rstrip("？?") + "？"
        return _excerpt(question, limit)
    choice = next((s for s in reversed(sentences) if _ACTION.search(s) and len(s) <= limit), None)
    return choice or _excerpt(next(iter(action["objectives"]), action["rendered_rationale"]), limit)


def _block_thresholds(predictions, zh):
    rules = []
    for item in predictions:
        method = item["prediction"].get("method") or {}
        p = method.get("parameters", {})
        if method.get("metric_id") not in ("missed_present", "blocks_missed_observed"):
            continue
        if all(k in p for k in ("endpoint","effect_minimum","zero_limit","minimum_evaluable")):
            rules.append((p["endpoint"],p["effect_minimum"],p["zero_limit"],p["minimum_evaluable"]))
    parts = []
    for endpoint,e,z,k in dict.fromkeys(rules):
        if endpoint not in ("angle_deg","rmsd_A"):
            continue
        unit = "°" if endpoint == "angle_deg" else " Å"
        name = ("角度" if zh else "angle") if endpoint == "angle_deg" else "RMSD"
        parts.append(f"{name} {e}{unit}/{z}{unit} (≥{k})")
    if not parts or len(parts) > 2:
        return ""
    prefix = "效应/近零阈值（最少可评窗口）" if zh else "Effect/near-zero cutoffs (minimum evaluable blocks) "
    return prefix + ", ".join(parts) + "."


def _cell(text):
    return " ".join(str(text).split()).replace("|", r"\|")


def _observations(prediction, assessment, output_labels, materials, zh):
    observations = []
    metric = (prediction.get("method") or prediction.get("external_endpoint") or {}).get("metric_id")
    for item in assessment.get("results", []):
        label = output_labels.get(item["artifact_ref"]) or ", ".join(
            materials[sha] for sha in item.get("matched_data_sha256", []) if sha in materials)
        value = str(item["value"])
        if isinstance(item["value"], float):
            short = format(item["value"], ".6g")
            if float(short) != prediction.get("threshold") or item["value"] == prediction.get("threshold"):
                value = short
            if metric in ("pair_delta_A", "median_delta_A", "pair_delta_deg", "median_delta_deg") and item["value"] > 0:
                value = "+" + value
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
    language = result.get("presentation", {}).get("language")
    zh = language == "zh" if language else bool(re.search(r"[\u3400-\u9fff]", result["goal"]))
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
    reason = _excerpt(final["reason"], 133 if zh else 230)
    finding = _finding(final["rationale"], 185 if zh else 350)
    for text in dict.fromkeys([reason, finding]):
        if text:
            lines.extend([text, ""])
    predictions = result["predictions"]
    if predictions:
        lines.extend([tr("## Original predictions", "## 原始预测"), "",
                      tr("| Prediction | Verdict | Observed |", "| 登记预测 | 原判决 | 实测 |"), "|---|---|---|"])
        # When many hypotheses compete for space, keep counterexamples and unresolved predictions visible.
        priority = {"opposed":0, "mixed":1, "undetermined":2, "supported":3}
        def metric(p):
            return (p["prediction"].get("method") or p["prediction"].get("external_endpoint") or {}).get("metric_id")
        opposed = {metric(p) for p in predictions if p["assessments"][-1].get("direction") == "opposed"}
        # Include a directly comparable positive outcome alongside counterexamples when space permits.
        selected = sorted(predictions, key=lambda p: (
            priority.get(p["assessments"][-1].get("direction"), 2),
            p["assessments"][-1].get("direction") == "supported" and metric(p) not in opposed))[:6]
        for p in sorted(selected, key=lambda item: item["registration"]["event_id"]):
            assessment = p["assessments"][-1]
            verdict = titles.get(assessment.get("direction"), tr("Undetermined", "未定"))
            if p["classification"] in ("historical_replay", "contains_retrospective_evidence"):
                verdict += tr(" (includes prior evidence)", "（含登记前证据）")
            criterion = p["registration"]["event_id"]
            observation = _observations(p["prediction"], assessment, labels, materials, zh)
            if any(revision["prediction"] != p["prediction"] for revision in p.get("revisions", [])):
                verdict += tr(" · later rule revised", " · 后续判据有修订")
            lines.append(f"| {criterion} · {_cell(_target(p, zh))} | {verdict} | {_cell(observation)} |")
        if len(predictions) > len(selected):
            directions = [p["assessments"][-1].get("direction", "undetermined") for p in predictions]
            totals = ", ".join(f"{directions.count(k)} {name}" for k,name in titles.items() if k in directions)
            lines.extend(["", tr(f"All {len(predictions)} original predictions: {totals}. See the full record.",
                                 f"全部 {len(predictions)} 项原预测中，{totals}。其余见完整记录。")])
        thresholds = _block_thresholds(selected, zh)
        if thresholds:
            lines.extend(["", thresholds])
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
        metric_methods = {m["metric_ref"]:j["method"]["id"] for j in result["jobs"] for o in j["outputs"]
                          for m in o.get("metrics", []) if m.get("metric_ref")}
        ranked = sorted(actions, key=lambda a:_action_rank(a, metric_methods), reverse=True)
        chosen = ranked[:2]
        if not any("ENACTED" in a.get("report_statuses", {}).values() for a in chosen):
            enacted = next((a for a in ranked if "ENACTED" in a.get("report_statuses", {}).values()), None)
            if enacted:
                chosen = [ranked[0], enacted]
        for action in sorted(chosen, key=lambda a:a["event_id"]):
            reports = ", ".join(map(str, action["report_events"]))
            sources = tr(f"cited reports {reports}", f"引用报告 {reports}") if reports else tr("used readings", "引用量测")
            used_methods = sorted({metric_methods[m] for m in action["metric_refs"] if m in metric_methods})
            if len(used_methods) > 1:
                sources += tr(" · read ", " · 已读 ") + " + ".join(used_methods[:2])
            effect = _action_text(action, 92 if zh else 160)
            n = sum(bool(j["job_id"]) for j in action["jobs"])
            commissioned = tr(f", {n} Jobs launched", f"，启动 {n} 项作业") if n else ""
            lines.append(f"- {action['event_id']} ({sources}{commissioned}) · {effect}")
        lines.append("")

    if final["unresolved"]:
        lines.extend([tr("## Scope and open questions", "## 适用范围与未决问题"), ""])
        coverage = next((s.strip() for s in _SENTENCE.split(final["rationale"])
                         if re.search(r"\d+/\d+", s) and re.search(r"限于|缺少|不足|缺测|too few|insufficient|unavailable", s, re.I)), "")
        if coverage and coverage not in final["unresolved"] and coverage not in finding:
            lines.append("- " + _excerpt(coverage, 110 if zh else 170))
        gaps = list(dict.fromkeys(final["unresolved"]))
        visible = gaps if len(gaps) <= 3 else [*gaps[:2], gaps[-1]]
        for item in visible:
            lines.append("- " + _excerpt(item, 75 if zh else 155))
        if len(visible) < len(gaps):
            lines.extend(["", tr(f"Plus {len(gaps)-len(visible)} open questions in the full record.",
                                 f"另有 {len(gaps)-len(visible)} 项未决问题，见完整记录。")])
        lines.append("")

    rejected = result["rejected_decisions"]
    event, closed = close["committed_event"], close["closed_event"]
    if rejected:
        lines.append(tr(f"The full record lists the reasons for {len(rejected)} rejected drafts.",
                        f"完整记录列明 {len(rejected)} 份草稿的拒绝原因。"))
    lines.append(tr(f"Decision {event} closed this study at event {closed}.", f"结题决定 {event} 已在事件 {closed} 生效。")
                 + " " + tr("[Full measurements, decision history and sources](result.json)",
                            "[完整量测、修订历史与来源](result.json)"))
    fallback = tr("Unresolved question (reference in result.json)", "未能还原的问题（索引见 result.json）")
    return re.sub(r"gap:[0-9a-f]{64}", lambda m:result["final_decision"].get("gap_questions", {}).get(m[0], fallback),
                  "\n".join(lines)) + "\n"
