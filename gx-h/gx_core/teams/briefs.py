"""One compiled brief per task attempt.

A worker with a million-token window does better with the whole specification of its task
than with a row and a sentence, and the backend does better judging one bundle than forty
calls. So what a task hands a worker is a brief: one Markdown document, compiled by the
backend with no model, from sections that the services publish through
``gx_core.registry.brief_section``. The compiler owns the layout and nothing else: it
calls every section that applies to the task's kind, in the order the registry gives, and
writes what they returned.

    compile_brief(ctx, task) -> (artifact_ref, sha256)

The brief is an artifact, so it is addressed by the sha256 of its bytes; the task records
that sha256 and a submission records the brief it answered, which ties a result to what the
worker was told. It is compiled again for every attempt, and the attempt that follows a
refusal carries what was refused and why (``brief_sections.previous_attempts``).

Three rules of the compiler
---------------------------
**The same state compiles to the same bytes.** Sections are laid out by ``(order, name)``;
the text carries no clock reading. Compiling twice without a change in between returns the
same artifact.

**Nothing is dropped silently.** A section whose body exceeds its byte budget is published
whole as an artifact of its own, and the brief shows its beginning, cut at a line, with the
reference, size and sha256 of the whole. A section that does not apply is listed as such in
the manifest at the end. A section function that raises stops the compilation with an error
that names the section and the module that published it: a brief that quietly lacked its
evidence would read exactly like a task that has none.

**The compiler adds no content.** Everything a worker reads came from a section, and each
section says what it was built from (``refs``).

**Nothing the injection screen flags reaches a worker.** Every line of a section's body, its
title and each of its refs go through :mod:`gx_core.research.screening` before anything is
measured, hashed or published, and the rendered brief goes through it once more. A flagged
line is replaced by ``[withheld: line matched the injection screen; see <ids>]``, naming the
backend identifiers the line carried so the worker can still reach what it was about through
the tools; a flagged title becomes the section's registered name and a flagged ref is left
out. The manifest says how many were withheld in each section, and a published compilation
records a ``brief.withheld`` event with the task, the attempt and each section's count, never
the text. The brief's sha256 is the hash of the text after withholding. The screen is crude by
design (it misses paraphrase and fires on innocent prose about installing things); what this
adds is that a line it does flag is never handed to a worker as part of its instructions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import contracts, registry
from ..artifacts.service import ArtifactService
from ..context import Ctx
from ..domain import ids
from ..errors import ArtifactHashMismatch, GxError, ImplementationGap, SchemaInvalid
from ..persistence import store
from ..registry import BriefSection
from ..research import screening
from . import recovery

KIND_BRIEF = "brief"
KIND_SECTION = "brief_section"
MEDIA_TYPE = "text/markdown"
ORIGIN = "BACKEND"
contracts.require_label("artifact_origin", ORIGIN)

#: Bytes of one section's body shown in the brief. What is beyond them goes by reference.
SECTION_BUDGET = 48_000
#: Bytes of all section bodies together. Kept under what one inline artifact may hold.
BRIEF_BUDGET = 200_000
#: A section is never cut to less than this while making a brief fit its budget.
MIN_SECTION_BYTES = 4_000
MAX_REFS_SHOWN = 40

#: What a line the injection screen flags is replaced with. ``{see}`` names the identifiers in it.
WITHHELD_MARKER = "[withheld: line matched the injection screen{see}]"
#: The event a published compilation that withheld anything records: counts, never text.
EVENT_WITHHELD = "brief.withheld"
#: Identifiers a withheld line's marker names, at most.
MAX_IDS_IN_MARKER = 8
#: The screen reads the first 2,000 characters of a line. A longer line is read here in pieces
#: of at most this many characters, cut where the line has a space or a tab, two consecutive
#: pieces at a time (see :func:`_stretches`), so its far end is read too.
_SCREEN_SPAN = 900
#: A backend identifier: one of the minted prefixes, an underscore, base-32 characters.
_IDENTIFIER = re.compile(
    r"\b(?:" + "|".join(sorted((re.escape(prefix) for prefix in ids.PREFIXES.values()), key=len, reverse=True))
    + r")_[0-9a-z]{1,40}\b"
)
#: Indentation, a quote marker and a list bullet: kept so a withheld list item stays one.
_LEAD = re.compile(r"[ \t]*(?:>[ \t]*)*(?:(?:[-*+]|\d{1,3}[.)])[ \t]+)?")

if BRIEF_BUDGET + 20_000 > int(contracts.response_limits()["max_inline_put_bytes"]):
    raise RuntimeError("a compiled brief must fit one inline artifact")


@dataclass
class CompiledSection:
    name: str
    module: str
    order: int
    title: str
    body: str
    refs: list[str]
    bytes: int
    sha256: str
    shown_bytes: int
    overflow_ref: str | None = None
    #: Lines, titles and refs of this section the injection screen flagged and the brief withholds.
    withheld: int = 0

    def manifest_line(self) -> str:
        line = f"- `{self.name}` ({self.module}): {self.bytes} bytes, sha256 {self.sha256[:16]}"
        if self.overflow_ref:
            line += f", {self.shown_bytes} shown here, whole section in {self.overflow_ref}"
        if self.withheld:
            line += f", {self.withheld} withheld by the injection screen"
        return line


@dataclass
class CompiledBrief:
    text: str
    attempt: int
    sections: list[CompiledSection] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)
    #: ``{section name: count}`` of what the injection screen withheld. ``(layout)`` counts what
    #: it found only in the rendered brief: the compiler's own lines, or the end of a line that
    #: was cut to fit the budget and reads as an instruction once cut.
    withheld: dict[str, int] = field(default_factory=dict)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------- screen

def _stretches(line: str) -> list[str]:
    """A long line as the screen can read it: overlapping stretches that end where it has spaces.

    The line is cut into pieces of at most ``_SCREEN_SPAN`` characters, each ending before a
    space or a tab (a word longer than that is cut where it must be), and each stretch is two
    consecutive pieces. So a match no longer than a piece lies whole inside one stretch, and a
    stretch neither starts nor ends inside a word, which would make ``sudoers`` read as ``sudo``.
    """
    bounds: list[tuple[int, int]] = []
    start = 0
    while start < len(line):
        end = min(len(line), start + _SCREEN_SPAN)
        if end < len(line):
            space = max(line.rfind(" ", start + 1, end + 1), line.rfind("\t", start + 1, end + 1))
            if space > start:
                end = space
        bounds.append((start, end))
        start = end
    return [line[bounds[index][0]:bounds[min(index + 1, len(bounds) - 1)][1]] for index in range(len(bounds))]


def flagged(line: str) -> bool:
    """Whether the injection screen flags this line, the far end of a long line included."""
    if screening.count(line):
        return True
    if len(line) <= 2 * _SCREEN_SPAN:
        return False
    return any(screening.count(stretch) for stretch in _stretches(line))


def withheld_marker(line: str) -> str:
    """The text that stands in for a flagged line: its bullet, and the identifiers it carried."""
    lead = _LEAD.match(line).group(0)
    found = list(dict.fromkeys(_IDENTIFIER.findall(line)))
    shown = found[:MAX_IDS_IN_MARKER]
    see = ""
    if shown:
        see = "; see " + ", ".join(f"`{identifier}`" for identifier in shown)
        if len(found) > len(shown):
            see += f" and {len(found) - len(shown)} more"
    marker = lead + WITHHELD_MARKER.format(see=see)
    # Belt and braces: the marker is built from a bullet and identifiers only, so it cannot be
    # flagged; if a change to the screen ever made it so, it is said without the identifiers.
    return marker if not flagged(marker) else lead + WITHHELD_MARKER.format(see="")


def withhold(text: str) -> tuple[str, int]:
    """``text`` with each line the injection screen flags replaced by its marker, and how many."""
    kept: list[str] = []
    count = 0
    for line in text.split("\n"):
        if flagged(line):
            count += 1
            kept.append(withheld_marker(line))
        else:
            kept.append(line)
    return "\n".join(kept), count


# ----------------------------------------------------------------------------- sections

def _approved_worker_decision(ctx: Ctx, task: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read a real, applied approval; fields supplied in task/input grant nothing."""
    event = ctx.conn.execute(
        "SELECT payload_json FROM events WHERE subject_kind='task' AND subject_id=?"
        " AND kind='task.worker_refusal_decided' ORDER BY event_id DESC LIMIT 1", (task["task_id"],),
    ).fetchone()
    if event is None:
        return None
    payload = store.loads(event, "payload_json", {}) or {}
    approval = ctx.conn.execute("SELECT * FROM approvals WHERE approval_id=?", (payload.get("approval_id"),)).fetchone()
    if approval is None or approval["status"] != "GRANTED" or approval["action_kind"] != "worker_refusal" \
            or approval["subject_kind"] != "task" or approval["subject_id"] != task["task_id"]:
        return None
    request = store.loads(approval, "request_json", {}) or {}
    detail = store.loads(approval, "decision_json", {}) or {}
    if detail.get("choice") not in ("reword", "authorize_once"):
        return None
    actual = ctx.conn.execute("SELECT attempt FROM tasks WHERE task_id=? AND run_id=?",
                              (task["task_id"], task["run_id"])).fetchone()
    if actual is None or not isinstance(payload.get("attempt"), int) or payload["attempt"] > actual["attempt"] \
            or request.get("attempt") != payload["attempt"] \
            or request.get("brief_sha256") != payload.get("brief_sha256") \
            or (payload.get("brief_sha256") and approval["subject_sha256"] != payload["brief_sha256"]) \
            or any(payload.get(key) != value for key, value in detail.items()):
        return None
    return {"approval_id": approval["approval_id"], **detail}


def _call_sections(ctx: Ctx, task: Mapping[str, Any]) -> tuple[list[CompiledSection], list[str]]:
    registry.load_providers()
    from . import brief_sections  # noqa: F401  (the base sections publish themselves on import)

    compiled: list[CompiledSection] = []
    skipped: list[str] = []
    decision = _approved_worker_decision(ctx, task)
    omitted = set(decision.get("omit_sections") or []) if decision and decision.get("choice") == "reword" else set()
    if decision:
        body, withheld = withhold("A person decided " + str(decision["choice"]) + ".\n"
                                  + str(decision.get("note") or "")
                                  + ("\nSections omitted by that decision: " + ", ".join(sorted(omitted)) if omitted else ""))
        raw = body.encode("utf-8")
        compiled.append(CompiledSection(name="worker_refusal_decision", module=__name__, order=-1,
                                        title="The person's decision on the refused worker", body=body,
                                        refs=[decision["approval_id"]], bytes=len(raw),
                                        sha256=hashlib.sha256(raw).hexdigest(), shown_bytes=len(raw), withheld=withheld))
    for entry in registry.sections_for(str(task["kind"])):
        if entry.name in omitted:
            skipped.append(entry.name)
            continue
        try:
            section = entry.fn(ctx, task)
        except GxError as exc:
            exc.details = {**(exc.details or {}), "brief_section": entry.name, "published_by": entry.module}
            exc.message = f"brief section {entry.name!r} ({entry.module}): {exc.message}"
            raise
        except Exception as exc:  # noqa: BLE001 - the owner's defect, named rather than swallowed
            raise ImplementationGap(
                f"brief section {entry.name!r} ({entry.module}) raised {type(exc).__name__}: {exc}",
                details={"brief_section": entry.name, "published_by": entry.module,
                         "raised": type(exc).__name__},
            ) from exc
        if section is None:
            skipped.append(entry.name)
            continue
        if not isinstance(section, BriefSection) or not isinstance(section.title, str) \
                or not isinstance(section.body, str):
            raise ImplementationGap(
                f"brief section {entry.name!r} ({entry.module}) returned "
                f"{type(section).__name__}, not a BriefSection with a title and a body",
                details={"brief_section": entry.name, "published_by": entry.module},
            )
        body = section.body.strip("\n")
        if not body.strip():
            skipped.append(entry.name)
            continue
        # The screen runs before anything is measured, hashed or published: the section's
        # sha256, its byte count and the artifact of a whole section are all of the screened text.
        body, withheld = withhold(body)
        title = " ".join(section.title.split()) or entry.name
        if flagged(title):
            title, withheld = entry.name, withheld + 1
        refs: list[str] = []
        for ref in section.refs or []:
            if flagged(str(ref)):
                withheld += 1
            else:
                refs.append(str(ref))
        raw = body.encode("utf-8")
        compiled.append(CompiledSection(
            name=entry.name, module=entry.module, order=entry.order, title=title, body=body,
            refs=refs, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), shown_bytes=len(raw),
            withheld=withheld,
        ))
    return compiled, skipped


def cut_at_line(text: str, budget: int) -> str:
    """The longest beginning of ``text`` within ``budget`` bytes that ends at a line end.

    A text with no line end inside the budget is cut at a character, never inside one.
    """
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    budget = max(budget, 0)
    head = raw[:budget]
    if raw[budget:budget + 1] != b"\n":      # the budget does not happen to fall on a line end
        newline = head.rfind(b"\n")
        if newline > 0:
            head = head[:newline]
    return head.decode("utf-8", errors="ignore").rstrip("\n")


def _fit(sections: list[CompiledSection]) -> None:
    """Decide how many bytes of each section the brief shows. Changes ``shown_bytes`` only."""
    for section in sections:
        section.shown_bytes = min(section.bytes, SECTION_BUDGET)
    # Still too long with every section at its budget: halve the longest, again and again.
    while sum(section.shown_bytes for section in sections) > BRIEF_BUDGET:
        longest = max(sections, key=lambda item: (item.shown_bytes, -item.order, item.name))
        if longest.shown_bytes <= MIN_SECTION_BYTES:
            raise SchemaInvalid(
                f"{len(sections)} brief sections do not fit {BRIEF_BUDGET} bytes even at "
                f"{MIN_SECTION_BYTES} bytes each",
                details={"sections": [section.name for section in sections]},
            )
        longest.shown_bytes = max(MIN_SECTION_BYTES, longest.shown_bytes // 2)


def _publish_overflow(ctx: Ctx, task: Mapping[str, Any], section: CompiledSection) -> str:
    """Publish a whole section as its own artifact and return its reference."""
    service = ArtifactService(ctx.conn)
    text = f"# {section.title}\n\n{section.body}\n"
    limit = int(contracts.response_limits()["max_inline_put_bytes"])
    if len(text.encode("utf-8")) <= limit:
        return service.put(
            run_id=task["run_id"], kind=KIND_SECTION, media_type=MEDIA_TYPE, content=text,
            task_id=task["task_id"], origin=ORIGIN, put_by=ctx.actor,
        ).artifact_ref
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return service.publish_bytes(
        namespace=f"{task['run_id']}/put/{task['task_id']}",
        rel_path=f"{KIND_SECTION}/{digest[:24]}.md", kind=KIND_SECTION, data=text,
        run_id=task["run_id"], task_id=task["task_id"], media_type=MEDIA_TYPE,
        provenance={"via": "brief_compiler", "section": section.name}, actor=ctx.actor,
        origin=ORIGIN, put_by=ctx.actor,
    ).artifact_ref


# ------------------------------------------------------------------------------- layout

def coming_attempt(task: Mapping[str, Any]) -> int:
    """The attempt a brief is compiled for: one more than the attempts already made."""
    return int(task.get("attempt") or 0) + 1


def _render(task: Mapping[str, Any], attempt: int, sections: list[CompiledSection],
            skipped: list[str]) -> str:
    lines = [
        f"# GX brief: {task['kind']} for the {task['role']}",
        "",
        f"Run `{task['run_id']}`, task `{task['task_id']}`, attempt {attempt} of "
        f"{int(task.get('max_attempts') or 0) or '?'}. Compiled by the backend from the "
        f"{len(sections)} sections below; this text is what the task asks of you, and the "
        "sha256 of its bytes is recorded on the task and on what you hand in.",
        "",
    ]
    for number, section in enumerate(sections, start=1):
        lines.append(f"## {number}. {section.title}")
        lines.append("")
        if section.overflow_ref:
            lines.append(cut_at_line(section.body, section.shown_bytes))
            lines.append("")
            lines.append(
                f"> This section is {section.bytes} bytes and the brief shows its beginning. The "
                f"whole section is artifact `{section.overflow_ref}` (sha256 {section.sha256}); "
                "Open the complete section with `gx_artifact_get` before relying on it. "
                "Use `materialize=true` when the local thread offers file delivery, or read all text pages."
            )
        else:
            lines.append(section.body)
        if section.refs:
            shown = section.refs[:MAX_REFS_SHOWN]
            more = len(section.refs) - len(shown)
            lines.append("")
            lines.append("Built from: " + ", ".join(f"`{ref}`" for ref in shown)
                         + (f" and {more} more" if more > 0 else ""))
        lines.append("")
    lines.append("## Brief manifest")
    lines.append("")
    lines.extend(section.manifest_line() for section in sections)
    if skipped:
        lines.append("- sections that do not apply to this task: " + ", ".join(f"`{name}`" for name in skipped))
    lines.append(f"- contract sha256 {contracts.contract_digest()['__combined__']}")
    return "\n".join(lines).rstrip("\n") + "\n"


def compile_text(ctx: Ctx, task: Mapping[str, Any], *, publish: bool = True) -> CompiledBrief:
    """Compile the brief of a task's coming attempt as text.

    With ``publish=False`` nothing is written: a section over its budget is shown cut, and
    the reference to the whole reads ``(not published)``. That is for looking at a brief
    without creating artifacts; the brief a task records is always compiled with publishing.
    """
    for key in ("task_id", "run_id", "kind", "role"):
        if not task.get(key):
            raise SchemaInvalid(f"a brief is compiled for a task row; this one has no {key}",
                                details={"missing": key})
    attempt = coming_attempt(task)
    sections, skipped = _call_sections(ctx, task)
    if not sections:
        raise ImplementationGap(
            f"no brief section applies to a {task['kind']} task; a worker would be handed nothing",
            details={"task_kind": task["kind"], "task_id": task["task_id"]},
        )
    _fit(sections)
    for section in sections:
        if section.shown_bytes < section.bytes:
            section.overflow_ref = _publish_overflow(ctx, task, section) if publish else "(not published)"
    # Once more over the whole text: a section's lines were screened above, so what this can
    # still find is the compiler's own layout, or the end of a line cut to fit the budget.
    text, layout = withhold(_render(task, attempt, sections, skipped))
    withheld = {section.name: section.withheld for section in sections if section.withheld}
    if layout:
        withheld["(layout)"] = layout
    compiled = CompiledBrief(text=text, attempt=attempt, sections=sections, not_applicable=skipped,
                             withheld=withheld)
    if withheld and publish:
        _record_withheld(ctx, task, compiled)
    return compiled


def _record_withheld(ctx: Ctx, task: Mapping[str, Any], compiled: CompiledBrief) -> None:
    """The ``brief.withheld`` event: which task, attempt and sections, how many. Never the text."""
    store.append_event(
        ctx.conn, kind=EVENT_WITHHELD, actor=ctx.actor, run_id=task["run_id"], subject_kind="task",
        subject_id=task["task_id"], evidence_level="EXECUTION_OBSERVED",
        reason=f"{sum(compiled.withheld.values())} line(s) of the brief matched the injection screen "
               "and were withheld",
        payload=recovery.derived({
            "task_id": task["task_id"], "attempt": compiled.attempt,
            "sections": [{"section": name, "count": count} for name, count in sorted(compiled.withheld.items())],
            "count": sum(compiled.withheld.values()), "brief_sha256": compiled.sha256,
        }),
    )


def compile_brief(ctx: Ctx, task: Mapping[str, Any]) -> tuple[str, str]:
    """Compile and publish the brief of a task's coming attempt: ``(artifact_ref, sha256)``.

    Called by the task service when a task becomes READY, inside its transaction. The
    caller records both values on the task.
    """
    compiled = compile_text(ctx, task, publish=True)
    artifact = ArtifactService(ctx.conn).put(
        run_id=task["run_id"], kind=KIND_BRIEF, media_type=MEDIA_TYPE, content=compiled.text,
        task_id=task["task_id"], origin=ORIGIN, put_by=ctx.actor,
    )
    if artifact.sha256 != compiled.sha256:
        raise ArtifactHashMismatch(
            "the published brief does not hash to the text that was compiled",
            details={"compiled": compiled.sha256, "published": artifact.sha256},
        )
    return artifact.artifact_ref, artifact.sha256


__all__ = [
    "BRIEF_BUDGET", "CompiledBrief", "CompiledSection", "EVENT_WITHHELD", "KIND_BRIEF", "KIND_SECTION",
    "SECTION_BUDGET", "WITHHELD_MARKER", "coming_attempt", "compile_brief", "compile_text", "cut_at_line",
    "flagged", "withheld_marker", "withhold",
]
