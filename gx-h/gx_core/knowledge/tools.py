"""Native tools for scoped memory retrieval and verified, paginated document reads."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .. import contracts
from ..context import Ctx
from ..errors import SchemaInvalid
from ..registry import BriefSection, ToolResult, brief_section, tool
from . import scopes as scope_rules
from .memory import MemoryService

#: Leave room under the envelope ceiling for the envelope itself.
_RESULT_BUDGET_FRACTION = 0.8


def _budget() -> int:
    return int(int(contracts.response_limits()["max_response_bytes"]) * _RESULT_BUDGET_FRACTION)


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


@tool("gx_memory_search")
def memory_search(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    scopes = scope_rules.from_argument(args.get("project_scope"))
    if not scopes:
        raise SchemaInvalid(
            "project_scope is required: name the namespace(s) to search, for example "
            "'<project>,real:library'. There is no default scope.",
            details={"argument": "project_scope"},
        )
    scope_rules.check_principal(ctx.principal, scopes)
    response = MemoryService(ctx.conn).search(
        args.get("query") or "", scopes, args.get("filters"), args.get("limit"),
        principal=ctx.principal.subject,
    )
    data = {
        "results": response["results"],
        "retrieval_mode": response["retrieval_mode"],
        "channels": response["channels"],
        "strategy_version": response["strategy_version"],
        "strategy_sha256": response["strategy_sha256"],
        "searched_namespaces": response["searched_namespaces"],
        "query_id": response.get("query_id"),
        "note": response["note"],
    }
    if response.get("reason"):
        data["reason"] = response["reason"]
    warnings: list[str] = []
    dropped = 0
    while data["results"] and _size(data) > _budget():
        data["results"] = data["results"][:-1]
        dropped += 1
    if dropped:
        warnings.append(
            f"{dropped} lower-ranked result(s) were left out to keep the answer under the response ceiling; "
            "ask with a smaller limit or a narrower scope to see them"
        )
    return ToolResult(data=data, warnings=warnings)


@tool("gx_memory_read")
def memory_read(ctx: Ctx, args: dict[str, Any]) -> ToolResult:
    doc_id = args.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id:
        raise SchemaInvalid("doc_id is required", details={"argument": "doc_id"})
    document = MemoryService(ctx.conn).read(
        doc_id, offset=args.get("offset"), limit=args.get("limit"), principal=ctx.principal
    )
    data = {key: document[key] for key in (
        "doc_id", "title", "body", "truncated", "content_sha256", "source_refs", "locators", "evidence_ids",
        "namespace", "memory_class", "outcome", "offset", "next_offset", "total_chars", "hash_verified",
    )}
    warnings: list[str] = []
    if not document["hash_verified"]:
        warnings.append("this document has no sha256 on record from when it was written, so the body could not be verified")
    # Locators can be many; the body is what was asked for. Trim the list, never the text.
    while len(data["locators"]) > 5 and _size(data) > _budget():
        data["locators"] = data["locators"][: max(5, len(data["locators"]) // 2)]
        if "locators were shortened" not in " ".join(warnings):
            warnings.append(f"locators were shortened; the document has {len(document['locators'])}")
    return ToolResult(data=data, warnings=warnings)


# ----------------------------------------------------------------------- brief section

#: What a worker is told about the retrieval it is handed. The backend matches words and
#: character pairs; understanding the question is the worker's part of the work.
LEXICAL_GUIDANCE = (
    "This retrieval matches words and character pairs. It does not understand meaning, and it is "
    "labelled that way in every answer. So do the semantic part yourself: ask each question several "
    "ways with gx_memory_search (a method's name and its abbreviation, the English and the Chinese "
    "term, the quantity as well as the claim), take more candidates than you need, and rank them by "
    "reading them. The backend records each query and the ranking it returned; the order you put "
    "them in after reading is your judgement and is reported as yours, not as a retrieval result. "
    "No score here means relevant, and finding nothing means these words found nothing."
)


def _task_json(task: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = task.get(key) if hasattr(task, "get") else task[key]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def questions_of(ctx: Ctx, task: Mapping[str, Any]) -> list[str]:
    """What a task is asking, from its own input, falling back to the run's objective."""
    given = task.get("input") if isinstance(task.get("input"), Mapping) else _task_json(task, "input_json")
    questions: list[str] = []
    for key in ("question", "objective", "topic"):
        if isinstance(given.get(key), str) and given[key].strip():
            questions.append(given[key].strip())
    for item in given.get("sub_questions") or []:
        text = item.get("question") if isinstance(item, Mapping) else item
        if isinstance(text, str) and text.strip():
            questions.append(text.strip())
    if not questions and task.get("run_id"):
        row = ctx.conn.execute("SELECT objective FROM runs WHERE run_id=?", (task["run_id"],)).fetchone()
        if row is not None and row["objective"]:
            questions.append(row["objective"])
    return list(dict.fromkeys(questions))


_SOURCE_ID = re.compile(r"gxs_[0-9A-Za-z]{1,40}")
_SEGMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}")


def _locator_line(locator: Mapping[str, Any]) -> str:
    """One locator as a brief shows it: source id, segment and anchor status."""
    source_id = str(locator.get("source_id") or "")
    if not _SOURCE_ID.fullmatch(source_id):
        return "a locator that names no source id"
    segment = str(locator.get("segment_id") or "")
    anchor = locator.get("anchor_status")
    return (f"{source_id} {segment if _SEGMENT_ID.fullmatch(segment) else ''}".rstrip()
            + (f" [{anchor}]" if anchor in contracts.label_values("anchor_status") else ""))


@brief_section(
    "prior_knowledge",
    kinds=("research_round",),
    order=30,
)
def prior_knowledge(ctx: Ctx, task: Mapping[str, Any]) -> BriefSection | None:
    """What scoped retrieval returns for the task's questions: pointers, never bodies."""
    run_id = task.get("run_id")
    if not run_id:
        return None
    questions = questions_of(ctx, task)
    if not questions:
        return None
    scopes = scope_rules.for_run(ctx.conn, run_id)
    memory = MemoryService(ctx.conn)
    lines: list[str] = []
    refs: list[str] = []
    for question in questions[:6]:
        answer = memory.search(question, scopes, None, 6, run_id=run_id, principal=ctx.principal.subject)
        lines.append(f"Question: {question}")
        lines.append(
            f"  retrieval {answer['retrieval_mode']}, strategy {answer['strategy_version']}, "
            f"searched {len(answer['searched_namespaces'])} namespace(s)"
        )
        if not answer["results"]:
            lines.append(f"  nothing found: {answer.get('reason', 'no match')}")
            continue
        for hit in answer["results"]:
            refs.append(hit["doc_id"])
            # Only identifiers and labels go into a worker's instructions.
            located = "; ".join(_locator_line(loc) for loc in hit["locators"][:2])
            lines.append(
                f"  - {hit['doc_id']} [{hit['memory_class']}{'/' + hit['outcome'] if hit['outcome'] else ''}] "
                f"{hit['namespace']}" + (f" | read at: {located}" if located else "")
            )
    body = "\n".join(
        [
            "Open scoped memory records with gx_memory_read. Read a cited artifact with "
            "gx_artifact_get when your current run has access. A PAGE_ONLY locator identifies "
            "the page where the underlying claim should be checked.",
            "",
            LEXICAL_GUIDANCE,
            "",
            *lines,
        ]
    )
    return BriefSection(title="Prior knowledge", body=body, refs=list(dict.fromkeys(refs)))
