"""Two-stage independent review. Positive judgments must point to a delivered finding and actual action."""
from receipts import digest, need, parse


def lead_turns(evidence):
    return [t for t in evidence["turns"] if t["scope"] == "run"]


def key(turn):
    return digest([turn["thread_id"], turn["turn_id"], turn["brief_sha256"]])


def brief_packet(evidence):
    windows = []
    for turn in lead_turns(evidence):
        brief = parse(turn["frozen_brief"].split("\n", 1)[1])
        windows.append(dict(window=key(turn), brief_sha256=turn["brief_sha256"], goal=brief["goal"],
                            reports=[r["delivered"] for r in turn["reports"]],
                            observations=brief.get("observations", [])))
    return {"blinding": "Current and previous decisions, architecture label, costs, branch identities, and answer key withheld. Module names and scientific wording can reveal the method.",
            "windows": windows}


def matching_sources(report, quote):
    """Only the model-visible report text can define a prospective opportunity."""
    projection = report["delivered"]
    refs = set()
    for finding in projection.get("findings", []):
        if quote in finding["text"]:
            refs.update(finding["source_refs"])
    if any(quote in gap for gap in projection.get("gaps", [])):
        refs.update(projection.get("artifact_refs", []))
    if refs:
        refs.add(report["ref"])
    contrary = projection.get("counterevidence", [])
    if isinstance(contrary, str):
        contrary = [contrary]
    for item in contrary:
        text = item if isinstance(item, str) else item.get("text", "")
        if quote in text:
            refs.add(report["ref"])
            for original in report.get("original_findings", []):
                if original.get("kind") == "counterevidence" and quote in original["text"]:
                    refs.update(original["source_refs"])
            if isinstance(item, dict):
                refs.update(item.get("source_refs", []))
    return refs


def measurement_ids(report, refs):
    return {s["sha256"] for s in report["sources"] if s["artifact_ref"] in refs}


def earlier_measurement(reports, report, quote, refs):
    markers = measurement_ids(report, refs)
    related = []
    for old in reports:
        if matching_sources(old, quote) and (report["ref"] == old["ref"] or
                markers.intersection(measurement_ids(old, matching_sources(old, quote)))):
            return True, related
        for item in old["delivered"].get("findings", []):
            if item["text"].strip() and markers.intersection(measurement_ids(old, item["source_refs"])):
                related.append(item["text"])
    return False, related


def lock_opportunities(evidence, review, reader):
    packet = brief_packet(evidence)
    need(review["reviewer"] == reader and review["packet_sha256"] == digest(packet),
         "Opportunity review belongs to another reviewer or packet")
    turns = lead_turns(evidence)
    marks = review["windows"]
    need([m["window"] for m in marks] == [key(t) for t in turns], "Mark every delivered Lead window in order")
    previous, marked = [], []
    for turn, mark in zip(turns, marks):
        need(bool(mark.get("reason", "").strip()), "Supply a reason for each opportunity judgment")
        item = {"window": key(turn), "reason": mark["reason"], "source_refs": [], "is_new": False}
        if mark["report_ref"] is not None:
            quote = mark.get("quote", "").strip()
            need(quote, "A reconsideration opportunity requires an exact quotation")
            relevant = [r for r in turn["reports"] if r["ref"] == mark["report_ref"]]
            need(len(relevant) == 1, "The claimed report was absent from the delivered brief")
            sources = matching_sources(relevant[0], quote)
            need(sources, "No matching report text appeared in this delivered brief")
            repeated, related = earlier_measurement(previous, relevant[0], quote, sources)
            need(not repeated, "This information was already in an earlier delivered brief")
            if related:
                # One file can contain multiple measurements. Two formulations of one fact
                # can also differ verbatim. Retain the actual texts for independent judgment.
                comparison = mark.get("comparison", {})
                need(comparison.get("earlier_text") in related and comparison.get("difference", "").strip() and
                     comparison.get("earlier_contained_endpoint") is False,
                     "Compare the earlier delivered finding on the same source before treating this endpoint as new")
                item["same_source_comparison"] = {"earlier_findings": related, **comparison}
            item.update(report_ref=mark["report_ref"], quote=quote, source_refs=sorted(sources), is_new=True)
        marked.append(item)
        previous.extend(turn["reports"])
    return dict(reviewer=reader, packet_sha256=digest(packet), review_sha256=digest(review), windows=marked)


def adjudicate(evidence, opportunities, review, answer, reviewer, dossier_sha):
    need(review["reviewer"] == reviewer and review["dossier_sha256"] == dossier_sha,
         "Outcome review belongs to another reviewer or dossier")
    turns = lead_turns(evidence)
    judgments = review["windows"]
    need([j["window"] for j in judgments] == [m["window"] for m in opportunities["windows"]] == [key(t) for t in turns],
         "Every delivered Lead window must be adjudicated")
    result = []
    prior_statements = []
    for turn, mark, judgment in zip(turns, opportunities["windows"], judgments):
        action = turn["decision"]
        need(type(judgment["unsupported_support"]) is bool and judgment["reason"].strip(),
             "Independently judge unsupported claims and explain each disposition")
        claimed = judgment["disposition"]
        need(claimed in ("CHANGE", "MAINTAIN", "MISSED", "AUTOMATIC_ONLY", "UNRESOLVED", "NO_OPPORTUNITY"),
             "Unknown adjudication")
        strict_effect = bool(action and (action["new_jobs"] or action["withdrawn"]))
        effect = strict_effect or bool(action and action.get("commissioned_jobs"))
        citation = bool(action and set(action["cited"]).intersection(mark["source_refs"]))
        accepted = False
        if claimed in ("CHANGE", "MAINTAIN"):
            excerpt = judgment.get("rationale_excerpt", "").strip()
            need(mark["is_new"] and citation and excerpt and excerpt in action["rationale"],
                 "A justified choice requires a fresh opportunity, actual citation and model rationale")
            need(type(judgment["scope_appropriate"]) is bool, "Record scope judgment")
            if claimed == "CHANGE":
                need(effect, "A graph edit or proposed Job has no verified follow-up effect")
            elif effect:
                # Commissioning the originally proposed work can fulfill a maintained plan.
                planned = judgment.get("prior_plan_excerpt", "").strip()
                need(planned and any(planned in p for p in prior_statements),
                     "Link the completed follow-up to the plan stated before the new evidence")
            choice = judgment.get("accepted_action")
            accepted = choice in answer["acceptable_actions"]
            if not accepted:
                need(choice == "UNLISTED" and bool(judgment.get("alternative_reason", "").strip()),
                     "Unlisted scientific alternatives require an independent reason")
                accepted = True
            accepted = accepted and judgment["scope_appropriate"] and not judgment["unsupported_support"]
            if mark.get("same_source_comparison"):
                need(type(judgment.get("novel_endpoint_confirmed")) is bool, "Independently adjudicate the earlier and later texts")
                accepted = accepted and judgment["novel_endpoint_confirmed"]
        elif claimed == "NO_OPPORTUNITY":
            need(not mark["is_new"], "A delivered substantive opportunity must retain its disposition")
        if claimed == "AUTOMATIC_ONLY":
            need(action and action["preplanned_jobs"] and not effect,
                 "No prearranged execution linked to this decision window")
        # The dedicated work-change measure also includes mismatched/incorrect changes.
        strict = bool(action and action["base"] and strict_effect and any(
            set(action["cited"]).intersection(matching_sources(r, f["text"]))
            for r in turn["reports"] for f in r["delivered"].get("findings", []) if f["text"].strip()))
        result.append(dict(window=key(turn), is_opportunity=mark["is_new"], disposition=claimed,
                           unsupported_support=judgment["unsupported_support"], reason=judgment["reason"],
                           strict_cited_work_change=strict, justified=accepted, citation_matched=citation,
                           unlisted=judgment.get("accepted_action") == "UNLISTED", judgment=judgment))
        prior_statements.append(parse(turn["frozen_brief"].split("\n", 1)[1])["goal"])
        if action:
            prior_statements.extend([action["rationale"], *[str(v) for op in action["ops"] for v in op.values()]])
    active = [r for r in result if r["is_opportunity"]]
    first = active[0] if active else None
    stopped = any(r["unsupported_support"] for r in result)
    passed = bool(first and first["justified"] and first["disposition"] == answer["condition"].upper()
                  and not stopped and not evidence["missing"])
    return dict(condition=answer["condition"], passed=passed, stop_unsupported=stopped, windows=result,
                opportunities=len(active), strict_cited_work_change=any(r["strict_cited_work_change"] for r in result),
                first_opportunity=first["window"] if first else None)


def visible_assignment(parent, ref, text):
    def includes(value):
        if isinstance(value, dict):
            return any(includes(v) for v in value.values())
        if isinstance(value, list):
            return any(includes(v) for v in value)
        return value == text
    packet = parse(parent["frozen_brief"].split("\n", 1)[1])
    return any(o.get("artifact_ref") == ref and includes(o.get("observation")) for o in packet["observations"])


def assignment_traces(evidence, report, source_refs):
    """Trace a chosen finding to the custodian assignment, its native delegation and actual scoped work."""
    event = evidence["intervention_event"]
    if event is None or report["author_kind"] != "MODULE" or not event < report["module_open_event"] < report["published_event"]:
        return []
    parents = lead_turns(evidence)
    owner = next((t for t in parents if [t["thread_id"], t["turn_id"]] == report["parent_window"]), None)
    if not owner or not any(m["call_id"] == report["parent_call"] for m in owner["commissions"]):
        return []
    if not any(visible_assignment(t, evidence["assignment_ref"], evidence["assignment_text"]) for t in parents
               if t["delivered_at"] <= owner["delivered_at"]):
        return []
    return [op for s in report["sources"] if s["artifact_ref"] in source_refs
            for op in s["scoped_operations"] if report["module_open_event"] < op["event_id"] <=
            op.get("settled_event", op["event_id"]) < report["published_event"]]


def matches_assay(trace, rule):
    if trace["kind"] == "job" and (trace["output"]["output_name"], trace["output"]["nature"]) != (
            rule.get("output_name"), rule.get("nature")):
        return False
    return (trace["kind"] == rule["kind"] and trace["capability"] == rule["capability"]
            and rule["input_sha256"] in trace["input_sha256"] and all(
                trace["parameters"].get(k) == v for k, v in rule["parameters"].items()))
