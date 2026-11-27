"""The objective of a run: screened before anything is planned, confirmed before anything is spent.

``contracts/states.json``, machine ``objective``: ``DRAFT → SCREENED → CONFIRMED``, with
``NEEDS_HUMAN_REVIEW`` for a direction the screen flagged and ``BLOCKED`` for one it
refuses. The first build had an ``Objective.confirm("the user said yes")`` that nothing
in production called and that unlocked a GPU job on a sentence.

The record belongs to the core: ``gx_run_start`` creates the ``objectives`` row with what
:func:`screen` returned, and the workflow's first node moves it and asks for the
``objective_confirmation`` approval, bound to the sha256 of the stored objective. This
module owns the three things that decide:

* :func:`screen` — the applicability screen. It answers one question: does a legality,
  privacy or biosafety review by a person apply? It reads two things: what the requester
  declared in answer to the questions of ``screen_policy.json``, and what the text of the
  objective names. A declared answer and the text can each only make the result stricter.
  The screen is honest about what it is: it catches what is declared and what is named; it
  does not understand a question, and every result says so.
* :func:`confirmation_condition` — whether the standing rule for confirming an objective
  holds. It reads the objective from the store by the approval's subject and screens it
  again; what a request says the screen found is not consulted.
* :func:`require_confirmed` — what anything that spends calls first. No argument of it
  says that somebody agreed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from ..domain import ids, machines
from ..errors import ContentHashMismatch, DependencyUnmet, PolicyScreenBlocked, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from ..registry import Subject

MACHINE = "objective"
CONFIRMED = "CONFIRMED"
CLEAR, NEEDS_HUMAN_REVIEW, BLOCKED = "CLEAR", "NEEDS_HUMAN_REVIEW", "BLOCKED"
#: Screen results, worst first. The two that stop a run are states of the objective machine.
RESULTS = (BLOCKED, NEEDS_HUMAN_REVIEW, CLEAR)
for _state in (CONFIRMED, NEEDS_HUMAN_REVIEW, BLOCKED):
    if _state not in machines.all_states(MACHINE):  # pragma: no cover - contract drift
        raise RuntimeError(f"objective state {_state!r} left the contract")

SUBJECT_KIND = "objective"
ACTION_KIND = "objective_confirmation"
POLICY_PATH = Path(__file__).resolve().parent / "screen_policy.json"

#: Where a structured objective carries its answers to the screening questions.
ANSWERS_KEY = "screening"

LIMITS_OF_THE_SCREEN = (
    "This screen reads declared answers and the words of the objective. It does not "
    "understand the question, and a direction that neither declares nor names what it is "
    "passes it. The host's own safeguards and every later review still apply."
)


def policy() -> tuple[dict[str, Any], str]:
    """The screen policy and the sha256 of its bytes. Re-read on every call."""
    raw = POLICY_PATH.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    for item in list(document["questions"]) + list(document["tripwires"]):
        level = item.get("if_true", item.get("result"))
        if level not in RESULTS or level == CLEAR:
            raise SchemaInvalid(
                f"screen policy entry {item.get('id')!r} names result {level!r}; an entry can "
                f"only ask for {NEEDS_HUMAN_REVIEW} or {BLOCKED}",
                details={"entry": item.get("id"), "policy": str(POLICY_PATH.name)},
            )
    return document, hashlib.sha256(raw).hexdigest()


def objective_sha256(objective: Any) -> str:
    """The sha256 of an objective's canonical form."""
    return ids.sha256_of_obj(objective)


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.replace("_", " ").replace("-", " ").split())


def _names(text: str, term: str) -> bool:
    """Whether folded ``text`` names folded ``term``.

    A term that starts with a Latin letter or digit must start a word: "aspirated" does not
    name "pirated". Its end is left open, because the policy lists stems ("weaponiz") on
    purpose. A term in a script written without spaces (Chinese) is found anywhere.
    """
    if not term:
        return False
    if not term[0].isascii() or not term[0].isalnum():
        return term in text
    start = text.find(term)
    while start != -1:
        before = text[start - 1] if start else " "
        if not (before.isascii() and before.isalnum()):
            return True
        start = text.find(term, start + 1)
    return False


def _text_of(objective: Any) -> str:
    """Every string of the objective, keys included, joined: what the tripwires read."""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, Mapping):
            for key, value in node.items():
                if key == ANSWERS_KEY:
                    continue
                parts.append(str(key))
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(objective)
    return _fold(" ".join(parts))


def _worst(levels: list[str]) -> str:
    for level in RESULTS:
        if level in levels:
            return level
    return CLEAR


def screen(objective: Any) -> dict[str, Any]:
    """Whether a legality, privacy or biosafety review by a person applies to this objective.

    ``objective`` is a sentence, or an object that may carry its answers to the screening
    questions under ``screening`` (question id -> true or false). Returns ``result``
    (``CLEAR``, ``NEEDS_HUMAN_REVIEW`` or ``BLOCKED``), a ``reason``, the ``findings``
    behind it, which questions were ``answered``, the digests of the objective and of the
    policy, and the limits of the screen. Pure: it reads the policy file and writes nothing.
    """
    if isinstance(objective, str):
        if not objective.strip():
            raise SchemaInvalid("an objective is not empty", details={})
    elif not isinstance(objective, Mapping) or not objective:
        raise SchemaInvalid("an objective is a sentence or an object", details={})
    document, policy_sha = policy()
    findings: list[dict[str, Any]] = []
    answers = objective.get(ANSWERS_KEY) if isinstance(objective, Mapping) else None
    question_ids = [question["id"] for question in document["questions"]]
    answered: list[str] = []
    if answers is not None:
        if not isinstance(answers, Mapping):
            raise SchemaInvalid(f"{ANSWERS_KEY} is an object of question id -> true or false",
                                details={"questions": question_ids})
        unknown = sorted(set(answers) - set(question_ids))
        if unknown:
            raise SchemaInvalid(f"{ANSWERS_KEY} answers question(s) the screen does not ask: {unknown}",
                                details={"questions": question_ids})
        for question in document["questions"]:
            answer = answers.get(question["id"])
            if answer is None:
                findings.append({"kind": "unanswered", "axis": question["axis"],
                                 "result": NEEDS_HUMAN_REVIEW, "question": question["id"],
                                 "why": f"screening question {question['id']!r} was left unanswered "
                                        "while others were answered"})
            elif not isinstance(answer, bool):
                findings.append({"kind": "unanswered", "axis": question["axis"],
                                 "result": NEEDS_HUMAN_REVIEW, "question": question["id"],
                                 "why": f"the answer to {question['id']!r} is not true or false"})
            else:
                answered.append(question["id"])
                if answer:
                    findings.append({"kind": "declared", "axis": question["axis"],
                                     "result": question["if_true"], "question": question["id"],
                                     "why": f"declared yes to {question['id']!r}: {question['text']}"})
    text = _text_of(objective)
    for tripwire in document["tripwires"]:
        hits = sorted({term for term in tripwire["terms"] if _names(text, _fold(term))})
        if hits:
            findings.append({"kind": "text", "axis": tripwire["axis"], "result": tripwire["result"],
                             "tripwire": tripwire["id"], "terms": hits,
                             "why": f"the text names {hits}, which {tripwire['id']!r} watches for"})
    result = _worst([finding["result"] for finding in findings])
    basis = ("declared answers and the text" if answers is not None else
             "the text only: the screening questions were not answered")
    if findings:
        deciding = [finding["why"] for finding in findings if finding["result"] == result]
        reason = "; ".join(deciding)[:1200]
    else:
        reason = f"nothing declared or named asks for a review ({basis})"
    return {
        "result": result, "reason": reason, "findings": findings,
        "axes": sorted({finding["axis"] for finding in findings}),
        "answered": answered, "questions": question_ids,
        "declared": answers is not None, "basis": basis,
        "objective_sha256": objective_sha256(objective), "policy_sha256": policy_sha,
        "limits": LIMITS_OF_THE_SCREEN,
    }


# ------------------------------------------------------------------------------ records

def view(row: sqlite3.Row) -> dict[str, Any]:
    spec = store.loads(row, "spec_json", {}) or {}
    return {
        "objective_id": row["objective_id"], "run_id": row["run_id"], "status": row["status"],
        "objective": spec.get("objective"), "spec_sha256": ids.sha256_of_obj(spec),
        "screen": store.loads(row, "screen_json", {}) or {}, "approval_id": row["approval_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """The objective record the run points at, or None when it was started without one."""
    row = conn.execute(
        "SELECT o.* FROM objectives o JOIN runs r ON r.objective_id = o.objective_id WHERE r.run_id=?",
        (run_id,),
    ).fetchone()
    return None if row is None else view(row)


def subject_of(record: Mapping[str, Any]) -> Subject:
    """What an approval of this objective is bound to: the record at the sha256 of its content."""
    return Subject(kind=SUBJECT_KIND, id=record["objective_id"], sha256=record["spec_sha256"])


def require_confirmed(conn: sqlite3.Connection, run_id: str, *, action: str) -> dict[str, Any]:
    """The run's CONFIRMED objective, or the refusal that says what it is waiting for."""
    record = for_run(conn, run_id)
    if record is None:
        raise DependencyUnmet(
            f"run {run_id} has no objective record; {action} is not derived from an objective "
            "nobody screened and confirmed",
            details={"run_id": run_id, "action": action},
        )
    if record["status"] in (BLOCKED, NEEDS_HUMAN_REVIEW):
        raise PolicyScreenBlocked(
            f"the objective of run {run_id} is {record['status']}; {action} waits for a person",
            details={"run_id": run_id, "objective_id": record["objective_id"],
                     "status": record["status"], "screen_result": record["screen"].get("result"),
                     "approval_id": record["approval_id"]},
        )
    if record["status"] != CONFIRMED:
        raise DependencyUnmet(
            f"the objective of run {run_id} is {record['status']}, not CONFIRMED; {action} is not "
            "derived from an unconfirmed objective",
            details={"run_id": run_id, "objective_id": record["objective_id"],
                     "status": record["status"], "approval_id": record["approval_id"]},
        )
    return record


def confirmation_condition(conn: sqlite3.Connection, subject: Subject) -> tuple[bool, dict[str, Any], list[str]]:
    """Whether the standing rule for confirming an objective holds: the screen, run again, is CLEAR.

    The objective is read from the store by the approval's subject and screened here; the
    approval must be bound to the sha256 of what the store holds.
    """
    row = conn.execute("SELECT * FROM objectives WHERE objective_id=?", (subject.id,)).fetchone()
    if subject.kind != SUBJECT_KIND or row is None:
        return False, {}, ["the approval names no objective the store holds"]
    spec = store.loads(row, "spec_json", {}) or {}
    stored = store.loads(row, "screen_json", {}) or {}
    facts: dict[str, Any] = {"objective_id": subject.id, "spec_sha256": ids.sha256_of_obj(spec),
                             "recorded_screen_result": stored.get("result")}
    reasons: list[str] = []
    if facts["spec_sha256"] != subject.sha256:
        reasons.append("the approval is bound to another sha256 than the objective the store holds")
    objective = spec.get("objective")
    if objective is None:
        reasons.append("the record holds no objective to screen")
        return False, facts, reasons
    try:
        recomputed = screen(objective)
    except SchemaInvalid as exc:
        reasons.append(f"the objective could not be screened: {exc.message}")
        return False, facts, reasons
    facts.update({"screen_result": recomputed["result"], "basis": recomputed["basis"],
                  "axes": recomputed["axes"], "policy_sha256": recomputed["policy_sha256"],
                  "objective_sha256": recomputed["objective_sha256"]})
    if recomputed["result"] != CLEAR:
        reasons.append(f"the screen finds this objective {recomputed['result']}: "
                       f"{recomputed['reason'][:300]}; that is a person's decision")
    return not reasons, facts, reasons


# ------------------------------------------------------------------ its specification

#: The task kind whose specification is the objective's (SCHEDULING_DESIGN §3: design_evaluation is
#: keyed on the objective).
DESIGN_KIND = "design_evaluation"
SPECIFICATION_EVENT = "objective.specification_recorded"


def records_specification(conn: sqlite3.Connection) -> bool:
    """Whether this store's objectives hold a specification: the columns arrive with contract r5."""
    return {"eval_spec_ref", "eval_spec_sha256"} <= {
        row["name"] for row in conn.execute("PRAGMA table_info(objectives)")}


def specification(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """The specification the run's objective records, ``{ref, sha256, objective_id}``; None while it records none.

    The objective records it when that specification is first frozen (SCHEDULING_DESIGN §5.9,
    contract r5 ``objectives.eval_spec_ref`` and ``eval_spec_sha256``). Only evaluations under
    it support a layer-3 claim, and only its paths are owed the confirmatory read. A store of
    an earlier contract has nowhere to record it, and so no objective has one there.
    """
    if not records_specification(conn):
        return None
    row = conn.execute(
        "SELECT o.objective_id, o.eval_spec_ref, o.eval_spec_sha256 FROM objectives o"
        " JOIN runs r ON r.objective_id = o.objective_id WHERE r.run_id=?", (run_id,)).fetchone()
    if row is None or not row["eval_spec_ref"] or not row["eval_spec_sha256"]:
        return None
    return {"ref": row["eval_spec_ref"], "sha256": row["eval_spec_sha256"], "objective_id": row["objective_id"]}


def _record_specification_for_run(conn, run_id, spec_record, *, actor):
    row = conn.execute(
        "SELECT o.* FROM objectives o JOIN runs r ON r.objective_id = o.objective_id WHERE r.run_id=?", (run_id,)).fetchone()
    if row is None or row["eval_spec_ref"]:
        return None
    # Only the known first-assay delta gets a mechanical successor. Existing
    # unrelated staleness still requires its original scientific rebase.
    from . import hypotheses as h
    current = h.current_context(conn, run_id)
    if current is not None:
        try:
            h.validate_context(conn, run_id, current["context_ref"], require_current=True)
        except StaleRevision:
            current = None
    recorded = {"ref": spec_record["ref"], "sha256": spec_record["frozen_sha"], "objective_id": row["objective_id"]}
    conn.execute("UPDATE objectives SET eval_spec_ref=?, eval_spec_sha256=? WHERE objective_id=? AND eval_spec_ref IS NULL",
                 (recorded["ref"], recorded["sha256"], row["objective_id"]))
    store.append_event(
        conn, kind=SPECIFICATION_EVENT, actor=actor, run_id=run_id, subject_kind=SUBJECT_KIND,
        subject_id=row["objective_id"], evidence_level="EXECUTION_OBSERVED",
        reason=f"{recorded['ref']} is the first selected frozen specification for this objective",
        payload={"eval_spec": recorded["ref"], "sha256": recorded["sha256"],
                 "author_task_id": spec_record.get("author_task_id")})
    if current is not None:
        snapshot = current["snapshot"]
        h.create_context(conn, run_id, actor=actor,
            evidence_refs=[item["evidence_id"] for item in snapshot["evidence"] if item.get("usable", True)],
            path_ids=[item["path_id"] for item in snapshot["paths"]],
            eval_spec_refs=sorted({item["ref"] for item in snapshot["eval_specs"]} | {recorded["ref"]}),
            asset_refs=[{key:item[key] for key in ("kind", "ref")} for item in snapshot["assets"]],
            round_index=snapshot["round_index"], expected_context_ref=current["context_ref"])
    return recorded


def bind_frozen_specification(conn, run_id, spec_record, *, actor):
    """Bind an explicitly selected intact assay to a consumer, retaining authorship.

    The caller has verified its code/content through require_frozen. Compare its
    current rows here and enforce the original namespace boundary; no builder,
    new assay or transferred author task is involved.
    """
    if not records_specification(conn):return None
    objective_row=conn.execute('SELECT o.eval_spec_ref FROM objectives o JOIN runs r ON r.objective_id=o.objective_id WHERE r.run_id=?',(run_id,)).fetchone()
    if objective_row is None or objective_row['eval_spec_ref']:return None
    from ..evaluation import datasets, spec as eval_specs
    from ..knowledge import scopes
    actual=eval_specs.get_ref(conn,spec_record['ref'])
    if actual!=spec_record or actual['status'] not in eval_specs.RUNNABLE or not actual['frozen_sha']:
        raise ContentHashMismatch('the selected frozen assay changed before objective binding')
    run=conn.execute('SELECT project_ref FROM runs WHERE run_id=?',(run_id,)).fetchone()
    if run is None:raise ScopeDenied('a selected assay needs an existing consumer run')
    allowed=scopes.for_run(conn,run_id)
    namespace=actual.get('namespace')
    if namespace is None:
        producer=conn.execute('SELECT r.project_ref FROM tasks t JOIN runs r ON r.run_id=t.run_id WHERE t.task_id=?',(actual.get('author_task_id'),)).fetchone()
        if producer is None or producer['project_ref']!=run['project_ref']:raise ScopeDenied('the original assay author belongs to another project')
    elif not any(scopes.namespace_in_scope(namespace,scope) for scope in allowed):
        raise ScopeDenied('the selected frozen assay is outside the consumer project scope')
    for split in actual['spec'].get('data_split',[]):
        dataset=datasets.get(conn,split['dataset_id'],split['version'])
        if dataset['status']!=datasets.REGISTERED or not any(scopes.namespace_in_scope(dataset['namespace'],scope) for scope in allowed):
            raise ScopeDenied('the selected assay dataset is withdrawn or outside the consumer project scope')
    return _record_specification_for_run(conn,run_id,actual,actor=actor)


def record_specification(conn: sqlite3.Connection, spec_record: Mapping[str, Any], *, actor: str) -> dict[str, Any] | None:
    """When a specification is frozen: record it on its run's objective, if that objective records none yet.

    Called in the transaction that freezes it. The specification is the objective's when the
    task that authored it is the run's ``design_evaluation``; one written by any other hand,
    or for no run, is not. The first freeze wins and is never replaced here: a later version
    (a successor, a bridge) is the objective's only through its own rule. Returns what was
    recorded, or None.
    """
    if not records_specification(conn) or not spec_record.get("author_task_id"):
        return None
    task = conn.execute("SELECT run_id, kind FROM tasks WHERE task_id=?", (spec_record["author_task_id"],)).fetchone()
    if task is None or task["kind"] != DESIGN_KIND or not task["run_id"]:
        return None
    return _record_specification_for_run(conn,task['run_id'],spec_record,actor=actor)


__all__ = [
    "ACTION_KIND", "ANSWERS_KEY", "BLOCKED", "CLEAR", "CONFIRMED", "DESIGN_KIND", "NEEDS_HUMAN_REVIEW", "RESULTS",
    "SPECIFICATION_EVENT", "SUBJECT_KIND", "confirmation_condition", "for_run", "objective_sha256", "policy",
    "bind_frozen_specification", "record_specification", "records_specification", "require_confirmed", "screen", "specification", "subject_of",
    "view",
]
