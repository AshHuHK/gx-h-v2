"""Read the PI's actual approvals for the single, direct-OAuth pilot and its blinded judge."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from uuid import UUID

SERIES = "GX-V62C-PUBLIC-COVERAGE-OAUTH-01"
QUESTIONS = {
    "research": ("若要實跑，請選這次新系列的授權範圍。僅用現有 ChatGPT OAuth 配額，不設試點 token 上限，不切 API key、不購買額外額度；若需額外付費或上一格用量未對清就停。這份授權只涵蓋公開試點。", "授權四格按順序實跑"),
    "judge": ("四格的執行授權已收到。任務包另要求在入場前指定獨立裁決者，檢查「引用是否正確、理由能否推出行動」。你要採哪種盲評安排？", "授權獨立 Codex 盲評，也只用同一現有 OAuth 配額"),
}
DIRECT = "並且限定直走限定只走現有OAuth"
TERMS = {
    "series": SERIES, "slots": ["01", "02", "03", "04"],
    "provider": "openai", "authentication": "chatgpt", "oauth_only": True, "api_key_fallback": False,
    "direct_official_host": True, "purchase_credits": False, "additional_currency_authorized": 0,
    "pilot_token_cap": None, "stop_on_unsettled_usage": True,
    "judge": "independent_blind_codex_same_oauth", "judge_per_slot": 1,
    "real_science_series_authorized": False,
}


def _enrollment(thread):
    UUID(thread)
    return Path.home() / ".local/state/gx-h/approved-pilots" / (thread + "-" + SERIES + ".json")


def _source(thread):
    if str(UUID(thread)) != thread:
        raise ValueError("Invalid conversation reference")
    # Authorship comes from the operator's original conversation store. Never export the transcript.
    parent = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    matches = list(parent.rglob("*" + thread + ".jsonl"))
    if len(matches) != 1:
        raise ValueError("The original PI conversation is unavailable or ambiguous")
    return matches[0]


def _evidence(thread):
    facts = {}
    with _source(thread).open(encoding="utf-8") as stream:
        for line in stream:
            if not ("限定直走" in line or any(answer in line for _, answer in QUESTIONS.values())):
                continue
            row = json.loads(line)
            msg = row.get("payload", {})
            if row.get("type") != "response_item" or (msg.get("type"), msg.get("role")) != ("message", "user"):
                continue
            for item in msg["content"]:
                text = item.get("text", "")
                kind = None
                if "".join(text.split()) == DIRECT:
                    kind = "direct_route"
                else:
                    m = re.fullmatch(r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*", text, re.S)
                    if not m:
                        continue
                    for answer in json.loads(m.group(1)):
                        for name, (question, approved) in QUESTIONS.items():
                            item_id = json.loads(answer["questionItemId"])
                            if (len(item_id) == 3 and item_id[0] == "request_user_input_async" and item_id[2] == 0
                                    and answer["question"] == question and answer["answer"] == approved):
                                kind = name
                if kind:
                    facts[kind] = {"timestamp": row["timestamp"], "role": "user",
                                   "message_sha256": sha256(text.encode("utf-8")).hexdigest()}
    if set(facts) != {"research", "judge", "direct_route"}:
        raise ValueError("Missing original PI authorization, direct-route restriction or judge approval")
    return facts


def bind(receipt_path, *, thread, work, candidate):
    """The operator creates exactly one enrollment location for the approved allocations."""
    path = Path(receipt_path).resolve()
    if path != _enrollment(thread):
        raise ValueError("Use the one enrollment record for the verified PI approval")
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("Pin the tested candidate commit")
    document = {**TERMS, "schema": "gx.pi.oauth-pilot/1", "conversation": thread,
                "messages": _evidence(thread), "candidate": candidate, "work": str(Path(work).resolve())}
    payload = (json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
    from tempfile import NamedTemporaryFile
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(pending, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("This approval already belongs to another registered cohort")
    finally:
        pending.unlink()
    return sha256(payload).hexdigest()


def verify(receipt_path, pinned_sha, *, series=SERIES):
    if series != SERIES:
        raise ValueError("Approval does not cover this series")
    path = Path(receipt_path).resolve()
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != pinned_sha:
        raise ValueError("The registered PI approval has changed")
    doc = json.loads(raw)
    canonical = _enrollment(doc["conversation"])
    if path != canonical or doc.get("schema") != "gx.pi.oauth-pilot/1" or any(doc.get(k) != v for k,v in TERMS.items()):
        raise ValueError("Expected the original OAuth-only pilot approval")
    if doc.get("messages") != _evidence(doc["conversation"]):
        raise ValueError("Approval differs from the original PI messages")
    return doc


def issue(*, thread, work, candidate):
    location = _enrollment(thread)
    return location, bind(location, thread=thread, work=work, candidate=candidate)
