"""Lead visibility and delivery size for a Goal with research modules."""
from ..artifacts import ArtifactService
from ..domain import ids
from ..persistence import store


def internal_event(row, branches):
    """Module observations become Lead evidence when the owner releases their report."""
    p = store.loads(row, "payload_json")
    if row["kind"] == "v6.decision.superseded":
        return str(p.get("cause_scope", "")).startswith("module:")
    if row["kind"] not in {"v6.node.settled", "v6.observation.added", "v6.claim.adopted"}:
        return False
    return p.get("branch_id") in branches or p.get("scope") in branches or str(p.get("scope", "")).startswith("module:")


def branch_ids(conn, run_id):
    return {row[0] for row in conn.execute("SELECT branch_id FROM v62_modules WHERE run_id=?", (run_id,))}


def attach_reports(conn, brief, cursor, policy):
    from . import v62_reports
    reports, report_index = v62_reports.report_views(conn, brief["run_id"], through_cursor=cursor,
                                                     per_report_bytes=policy["report_bytes"], total_bytes=policy["brief_bytes"])
    brief["module_reports"] = reports
    if report_index:
        brief["module_report_index"] = report_index
    released = v62_reports.released_evidence_refs(conn, brief["run_id"], through_cursor=cursor)
    brief["evidence_refs"] = list(dict.fromkeys([*brief["evidence_refs"], *released]))


def _size(obj):
    return len(ids.canonical_json(obj).encode("utf-8"))


def _excerpt(value, limit):
    raw = value.encode("utf-8")
    return value if len(raw) <= limit else raw[:limit - 3].decode("utf-8", errors="ignore") + "…"


def deliver(conn, brief, limit):
    """Keep the authoritative scope intact when a large frozen packet needs pagination.

    This is called after the WAL read snapshot. The referenced artifact contains exactly
    the authorized packet, including all of its released results. Its content digest
    makes concurrent preparation/retry converge on the same bytes.
    """
    from .v6_native import PREFIX
    room = limit - len(PREFIX.encode("utf-8"))
    if _size(brief) <= room:
        return brief, brief
    raw = ids.canonical_json(brief)
    sha = ids.sha256_of_obj(brief)
    artifact = ArtifactService(conn).publish_bytes(
        namespace=brief["run_id"] + "/briefs", rel_path=sha + ".json", kind="research_brief",
        data=raw, expected_sha256=sha, media_type="application/json", run_id=brief["run_id"],
        provenance={"goal_version": brief["goal_version"], "scope": "run", "through_cursor": brief["trigger"]["through_cursor"]},
    )
    ref = artifact.artifact_ref
    full = {**brief, "full_brief_ref": ref, "evidence_refs": list(dict.fromkeys([*brief["evidence_refs"], ref]))}
    packet = {k: brief[k] for k in ("run_id", "scope", "goal_version", "head", "research_effort")}
    packet.update(goal=_excerpt(brief["goal"], 512), full_brief_ref=ref, read_with="gx_read_artifact",
                  module_catalog_available=bool(brief.get("module_catalog_available") or brief.get("module_catalog")),
                  **({"source_tools": brief["source_tools"]} if brief.get("source_tools") else {}),
                  budget=brief["budget"], previous_decision=None, evidence_refs=[ref],
                  trigger={k: brief["trigger"][k] for k in ("since_cursor", "through_cursor")})
    # Stage the fields used by the public decision tools even in the smallest supported packet.
    packet.update(materials=[], capabilities=[], branches=[], nodes=[], claims=[], observations=[],
                  intent={}, module_reports=[], module_catalog=[])
    if "research_evidence_version" in brief:
        packet["research_evidence_version"] = brief["research_evidence_version"]
    if "evidence_policy" in brief:
        packet["evidence_policy"] = brief["evidence_policy"]
    progress = brief.get("research_progress")
    if progress:
        packet["research_progress"] = {k: progress[k] for k in (
            "phase", "no_progress_rounds", "consecutive_no_progress", "decision_options")}
        packet["research_progress"].update(open_gaps=[], open_gap_count=len(progress["open_gaps"]),
                                          waiting_for=[], waiting_count=len(progress["waiting_for"]))
    if _size(packet) > room:
        packet["budget"] = {"initial_job_limit": brief["budget"]["initial_job_limit"]} if "initial_job_limit" in brief["budget"] else {}
    # JSON escaping can expand the goal beyond its UTF-8 excerpt size.
    if _size(packet) > room:
        packet["goal"] = _excerpt(brief["goal"], 96)

    def append(key, value, *, owner=packet):
        owner[key].append(value)
        if _size(packet) <= room:
            return True
        owner[key].pop()
        return False

    # Reserve space for adverse results and their durable citations ahead of routine inventory.
    report_index = brief.get("module_report_index", [])
    adverse = [entry for entry in reversed(report_index) if entry.get("counterevidence")]
    adverse_refs = {entry["report_ref"] for entry in adverse}
    ordinary = [entry for entry in reversed(brief.get("module_reports", [])) if entry["report_ref"] not in adverse_refs]
    if adverse:
        packet["reports_with_counterevidence"] = len(adverse)
    for report in [*adverse, *ordinary]:
        summary = {k: report[k] for k in ("module_instance_id", "status", "report_ref") if k in report}
        if report.get("applicability"):
            summary["applicability"] = report["applicability"]
        contrary = report.get("counterevidence", [])
        if contrary:
            first = contrary[0]
            summary["counterevidence"] = [{"text": _excerpt(first["text"], 256), "source_refs": first["source_refs"][:1]}]
        if append("module_reports", summary):
            append("evidence_refs", report["report_ref"])
    if progress:
        projected = packet["research_progress"]
        for key in ("waiting_for", "open_gaps"):
            allowance = room // 8
            for row in progress[key]:
                item = {"gap_ref": row["gap_ref"]} if key == "open_gaps" else row
                allowance -= _size(item) + 1
                if allowance < 0 or not append(key, item, owner=projected):
                    break
    for key in ("module_catalog", "materials", "capabilities", "branches", "nodes", "claims", "observations"):
        for item in brief.get(key, []):
            if not append(key, item):
                break
    for key in ("previous_decision", "catalog_view", "module_catalog_view", "research_progress", "intent", "trigger"):
        if key not in brief:
            continue
        old = packet.get(key)
        packet[key] = brief[key]
        if _size(packet) > room:
            if old is None and key not in ("previous_decision",):
                packet.pop(key)
            else:
                packet[key] = old
    return full, packet
