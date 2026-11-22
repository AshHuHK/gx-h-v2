"""Interpret supported, explicit operator requests and resolve references to a displayed preview."""
import json
import re
import unicodedata
from decimal import Decimal

from ..capabilities.manifest import CAPABILITY_ID
from ..errors import ArtifactNotFound
from ..persistence import store
from . import intent


def clarify(message, **fields):
    return {"status": "needs_clarification", "message": message, **fields}


def mark_displayed_together(conn, previews):
    """Record the exact GX output after flushing it to the operator, never from a model tool."""
    if not previews:
        return
    with store.transaction(conn):
        verified = []
        for mid, rendered in previews:
            if not isinstance(rendered, dict) or rendered.get("status") != "awaiting_confirmation":
                continue
            row = conn.execute("SELECT c.run_id,c.change_id,c.preview_sha256,h.result_json FROM p0_intent_changes c "
                               "JOIN p0_human_inputs h ON c.source_message_id=h.message_id "
                               "WHERE h.message_id=? AND c.change_id=?", (mid, rendered.get("change_id"))).fetchone()
            if (row and row["result_json"] and row["preview_sha256"] == rendered.get("preview_sha256") and
                    json.loads(row["result_json"]) == rendered):
                verified.append(row)
        if not verified or len({row["run_id"] for row in verified}) != 1:
            return
        run_id = verified[0]["run_id"]
        group = [row["change_id"] for row in verified]
        last = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='intent.preview_displayed' "
                            "ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
        if last and json.loads(last[0]).get("group") == group:
            return
        positions = conn.execute("SELECT (SELECT coalesce(max(rowid),0) FROM p0_operator_messages WHERE run_id=?),"
                                 "(SELECT coalesce(max(rowid),0) FROM p0_human_inputs WHERE run_id=?)",
                                 (run_id, run_id)).fetchone()
        for row in verified:
            store.append_event(conn, kind="intent.preview_displayed", actor="operator-client", run_id=run_id,
                               subject_kind="change", subject_id=row["change_id"],
                               payload={"operator_position": positions[0], "input_position": positions[1],
                                        "preview_sha256": row["preview_sha256"], "group": group})


def _presentations(conn, message_id, change_id=None):
    # Only displays strictly before this message count, including when a busy main
    # thread acknowledges a previously queued input later. Row order survives clock ties.
    return conn.execute("""
        SELECT e.subject_id,coalesce(json_array_length(e.payload_json,'$.group'),1) AS group_size
        FROM p0_human_inputs h JOIN events e ON e.run_id=h.run_id
        JOIN p0_intent_changes c ON c.run_id=e.run_id AND c.change_id=e.subject_id
        LEFT JOIN p0_operator_messages m ON m.message_id=h.message_id AND m.run_id=h.run_id
        WHERE h.message_id=? AND e.kind='intent.preview_displayed'
          AND (? IS NULL OR e.subject_id=?) AND c.preview_sha256=json_extract(e.payload_json,'$.preview_sha256')
          AND h.rowid>json_extract(e.payload_json,'$.input_position')
          AND (m.rowid IS NULL OR m.rowid>json_extract(e.payload_json,'$.operator_position'))
        ORDER BY e.event_id DESC
        """, (message_id, change_id, change_id))


def was_displayed(conn, change_id, message_id):
    return _presentations(conn, message_id, change_id).fetchone() is not None


def _confirmation(conn, run_id, message_id, key, cancel):
    rows = conn.execute("SELECT change_id,preview_sha256 FROM p0_intent_changes WHERE run_id=? "
                        "AND status='awaiting_confirmation' AND (? IS NULL OR change_id=? OR preview_sha256=?)",
                        (run_id, key, key, key)).fetchall()
    history = list(_presentations(conn, message_id))
    visible = {e["subject_id"] for e in history}
    shown = [row for row in rows if row["change_id"] in visible]
    unique_last = history and history[0]["group_size"] == 1 and shown and history[0]["subject_id"] == shown[0]["change_id"]
    if len(rows) != 1 or not shown or (not key and not unique_last):
        return clarify("Display the exact pending preview, then confirm it in a later message. "
                       "When several are pending, name the displayed change ID.",
                       displayed_change_ids=[row["change_id"] for row in shown])
    action = {"kind": "confirm", **dict(shown[0])}
    if cancel:
        action["acknowledge_consequences"] = cancel
    return action


_RESOURCES = {
    "tokens": r"tokens?|reserved[ _]tokens?|令牌",
    "api_calls": r"(?:gx|api)[ _]calls?|API\s*调用(?:次数)?",
    "cpu_seconds": r"cpu[ _]seconds?|CPU\s*秒(?:数)?",
    "gpu_minutes": r"gpu[ _]minutes?|GPU\s*分钟(?:数)?",
    "gpu_jobs": r"gpu[ _]jobs?|GPU\s*作业(?:数)?",
    "job_submissions": r"job[ _]submissions?|作业提交(?:数|次数)?",
    "worker_tasks": r"worker[ _]tasks?|工作任务数",
}
_RESOURCE = "(?P<resource>" + "|".join(_RESOURCES.values()) + ")"
_NUMBER = r"(?P<n>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?P<scale>[kK万]?)"
_EN_PROJECT = r"(?:(?:the|our)\s+)?(?:(?:project(?:'s)?|GX)\s+)?"
_EN_LIMIT = r"(?:limit|cap|ceiling|budget|allowance)"
_ZH_LIMIT = r"(?:预算(?:上限)?|上限|限额|额度)"
_LIMIT_PATTERNS = (
    rf"(?P<verb>lower|reduce|decrease|cut|raise|increase|set|change)\s+{_EN_PROJECT}{_RESOURCE}\s+{_EN_LIMIT}\s+to\s+{_NUMBER}",
    rf"(?P<verb>lower|reduce|decrease|cut|raise|increase|set)\s+{_EN_PROJECT}{_RESOURCE}\s+to\s+{_NUMBER}",
    rf"(?P<verb>cap|limit)\s+{_EN_PROJECT}{_RESOURCE}\s+(?:at|to)\s+{_NUMBER}",
    rf"(?:把|将)?\s*(?:(?:项目|GX)(?:的)?\s*)?{_RESOURCE}\s*(?:(?:的)?{_ZH_LIMIT})?\s*(?P<verb>降低|减少|下调|降|提高|增加|上调|提|调整|设定|设置|设|改)(?:低)?[到为至]\s*{_NUMBER}",
    rf"(?P<verb>降低|减少|下调|提高|增加|上调|调整|设置|设定)\s*(?:项目(?:的)?\s*)?{_RESOURCE}\s*{_ZH_LIMIT}\s*[到为至]\s*{_NUMBER}",
)
_LOWER = {"lower", "reduce", "decrease", "cut", "降", "降低", "减少", "下调"}
_RAISE = {"raise", "increase", "提高", "增加", "上调", "提"}
_JOB_ID = r"gxj_[a-z0-9]+"
_CONFIRM_KEY = r"(?:gxq_[a-z0-9]+|[0-9a-f]{64})"
_CLAUSE = re.compile(r"[\n;；]|(?:\band\s+(?:then\s+)?|然后|同时|并且|并)(?:cancel|discard|change|remove|set|raise|lower|stop|取消|删除|停止|放宽|提高|降低|修改)", re.I)


def _single_edit(conn, run_id, kind, value, default, *, resource=None):
    current = intent.active_items(conn, run_id, kind)
    if resource:
        current = {key: item for key, item in current.items() if item["value"]["resource"] == resource}
    if len(current) > 1:
        return clarify("More than one current item matches. Specify the item to edit.", item_ids=list(current))
    if current:
        item, = current.values()
        key, version = item["item_id"], item["version"]
    else:
        key = default
        try:
            previous = intent.read_item(conn, run_id, key)
        except ArtifactNotFound:
            version = 0
        else:
            if previous["item_type"] != kind or previous["status"] != "removed":
                return clarify("The proposed item ID is already used for another setting.", item_ids=[key])
            version = previous["version"]
    return {"kind": "change", "items": [{"item_id": key, "item_type": kind,
                                         "expected_version": version, "value": value}]}


def _resource_action(conn, run_id, text):
    for pattern in _LIMIT_PATTERNS:
        match = re.fullmatch(pattern, text, re.I)
        if match:
            break
    else:
        return None
    amount = Decimal(match["n"].replace(",", "")) * {"": 1, "k": 1000, "万": 10000}[match["scale"].lower()]
    if amount > 2**53 or amount != amount.to_integral_value():
        return clarify("Specify a nonnegative whole number of project budget units.")
    resource = next(key for key, term in _RESOURCES.items() if re.fullmatch(term, match["resource"], re.I))
    value = {"resource": resource, "max": int(amount)}
    if resource in intent.NATIVE_UNITS:
        value["unit"] = intent.NATIVE_UNITS[resource]
    action = _single_edit(conn, run_id, "resource_limit", value, resource, resource=resource)
    if action.get("kind") != "change":
        return action
    edit, = action["items"]
    prior = intent.read_item(conn, run_id, edit["item_id"])["value"] if edit["expected_version"] else None
    if prior:
        before, requested, direction = prior["max"], value["max"], match["verb"].lower()
        if (direction in _LOWER and requested > before) or (direction in _RAISE and requested < before):
            return clarify("The requested amount moves the current limit in the opposite direction.",
                           current=before, requested=requested, resource=resource)
    elif match["verb"].lower() in _RAISE:
        return clarify("There is no current project Intent limit for this resource. Specify the initial GX limit "
                       "or identify the existing budget you mean to increase.", resource=resource)
    return action


def prepare(conn, run_id, message_id, original):
    """Use the entire trusted input. A matching substring of a quotation cannot authorize an edit."""
    if re.match(r"^\s*(?:补充(?:资料|证据|说明)?|supplement(?:ary (?:note|evidence))?|additional (?:evidence|context))\s*[:：]\s*\S", original, re.I):
        return {"kind": "supplement", "text": original}
    stripped = original.strip().rstrip(".。!！").strip()
    stripped = re.sub(r"^(?:please\s+|请\s*)", "", stripped, count=1, flags=re.I)
    text = unicodedata.normalize("NFKC", stripped).replace("’", "'")
    confirm = re.fullmatch(r"(?:I\s+)?confirm\s+(?:(?:the|this|that)\s+)?(?:displayed |previewed )?change(?: (?:you )?just (?:showed(?: me)?|shown|displayed))?|确认(?:刚(?:才)?(?:展示|显示|预览|提出)(?:的)?|刚才(?:的)?)?(?:的)?(?:这|那)(?:一)?项(?:变更|更改|修改)", text, re.I)
    named = re.fullmatch(rf"(?:confirm(?:\s+(?:the\s+)?(?:change|preview))?\s+|确认(?:变更|预览)?\s*)({_CONFIRM_KEY})", text, re.I)
    cancel_confirm = re.fullmatch(rf"(?:(?:I\s+)?confirm (?:cancellation of|cancel(?:l)?ing) (?:the )?(?:jobs? (?:listed|shown) in (?:the |this )?preview|listed jobs?)|确认取消(?:该|本次|刚才(?:的)?)?预览中(?:列出|显示)(?:的)?(?:作业|任务))(?:\s+({_CONFIRM_KEY}))?", text, re.I)
    impact_confirm = re.fullmatch(rf"(?:I accept the listed consequences and confirm this change|确认执行并接受预览中列出的后果)(?:\s+({_CONFIRM_KEY}))?", text, re.I)
    if confirm or named or cancel_confirm or impact_confirm:
        specific = named or cancel_confirm or impact_confirm
        acknowledgment = intent.CANCEL_ACK if cancel_confirm else intent.IMPACT_ACK if impact_confirm else None
        return _confirmation(conn, run_id, message_id, specific[1] if specific else None, acknowledgment)

    # Cancellation is an explicit, independently previewed side effect. Accept a named
    # Job or an explicit request for all open Jobs in this run. Never infer it from cost.
    suffix = re.search(rf"(?:(?:\s+and\s+|[,，]\s*(?:and\s+)?|[，,]?\s*并(?:且)?\s*)|^)(?:cancel\s+(?:the\s+)?job\s+|取消(?:作业|任务)\s*)({_JOB_ID})$", text, re.I)
    all_jobs = re.search(r"(?:(?:\s+and\s+|[,，]\s*(?:and\s+)?|[，,]?\s*并(?:且)?\s*)|^)(?:cancel all (?:active|open|in-flight|running) jobs|取消所有(?:在途|进行中|运行中)(?:的)?作业)$", text, re.I)
    cancellation = suffix or all_jobs
    if cancellation:
        before = text[:cancellation.start()].rstrip()
        cap = _resource_action(conn, run_id, before) if before else None
        if before and (not cap or cap.get("kind") != "change"):
            return clarify("Specify the new resource cap and identify the Jobs to cancel.")
        if suffix:
            job_ids = [suffix[1]]
        else:
            only_running = bool(re.search(r"running|运行中|进行中", all_jobs[0], re.I))
            job_ids = [row[0] for row in conn.execute(
                "SELECT job_id FROM jobs WHERE run_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','CANCEL_REQUESTED') "
                "AND (?=0 OR status='RUNNING') ORDER BY job_id", (run_id, only_running))]
        if not job_ids:
            return cap or {"kind": "proposal", "text": "The project has no open Jobs to cancel."}
        if cap:
            return {**cap, "cancel_job_ids": job_ids}
        return {"kind": "cancel_jobs", "job_ids": job_ids}

    cap = _resource_action(conn, run_id, text)
    if cap:
        return cap
    if re.search(r"\bdiscard (?:all |the |pending |existing )*(?:work|results?|evidence)\b|(?:丢弃|删除|销毁)(?:所有|全部|待定|已有|在途)?(?:结果|证据|产物)", text, re.I):
        return clarify("Discarding research evidence is unsupported. To stop an open Job, identify it and "
                       "review the cancellation consequences. A prospective budget reduction can be requested on its own.",
                       proposed_text=original, unsupported_effects=["discard_results"])
    if not _CLAUSE.search(stripped) and "?" not in stripped and "？" not in stripped:
        # Capture literal scientific text from the original spelling, including Unicode.
        targets = [
            ("objective", r"(?:replace|change|set) (?:the |our )?(?:project )?(?:objective|goal) (?:with|to)\s+(.+)|(?:把|将)?(?:项目|研究)?目标(?:替换|更换|修改|改|设置|设)(?:为|成)\s*(.+)"),
            ("soft_preference", r"set (?:the )?(?:output |report )?preference to\s+(.+)|(?:把|将)?(?:输出|报告)偏好(?:设|改|设置)为\s*(.+)"),
            ("optimization_target", r"set (?:the )?optimi[sz]ation target to\s+(.+)|(?:把|将)?优化目标(?:设|改|设置)为\s*(.+)"),
        ]
        for kind, pattern in targets:
            match = re.fullmatch(pattern, stripped, re.I)
            if match:
                value = next(v for v in match.groups() if v is not None).strip()
                return _single_edit(conn, run_id, kind, value, kind)
        if re.fullmatch(r"(?:用)?中文解释每(?:一)?项结果|(?:explain|describe) (?:each|every) result in Chinese|cite (?:a source for )?(?:each|every) result", stripped, re.I):
            return _single_edit(conn, run_id, "soft_preference", stripped, "soft_preference")
        method = re.fullmatch(r"(ban|forbid|unban|allow) (?:the )?(?:method|capability)\s+([\w.@-]+)|(禁用|禁止|解禁|允许)(?:方法|能力)\s*([\w.@-]+)", stripped, re.I)
        if method:
            verb, capability = (v for v in method.groups() if v is not None)
            if CAPABILITY_ID.fullmatch(capability.split("@", 1)[0]):
                action = _single_edit(conn, run_id, "method_ban", {}, "methods")
                if action.get("kind") != "change":
                    return action
                edit, = action["items"]
                prior = intent.read_item(conn, run_id, edit["item_id"])["value"] if edit["expected_version"] else None
                bans = set((prior or {}).get("forbidden_capabilities", []))
                if verb.lower() in ("ban", "forbid", "禁止", "禁用"):
                    bans.add(capability)
                else:
                    bans.discard(capability)
                edit["value"] = {"forbidden_capabilities": sorted(bans)}
                return action
    return clarify("Specify the item and exact value you want to change, or identify the information as a supplement.")
