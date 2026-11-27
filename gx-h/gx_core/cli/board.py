"""Shared board presentation over the authoritative, read-only run snapshot.

No web server or second state store: ``gx board`` emits JSON, or publishes an escaped
HTML snapshot beside the selected database. Every refresh authorizes the same run.
"""
from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .. import api, context
from ..context import Principal
from ..errors import GxError
from ..persistence import store


def read_board(principal: Principal, run_id: str, *, after: int = 0, event_limit: int = 100) -> dict[str, Any]:
    from ..teams.board import SharedBoardService

    try:
        conn = store.open_store(context.store_path())
    except GxError as exc:
        return api.envelope.failure(f"board_{uuid.uuid4().hex}", exc, run_id=run_id)
    try:
        conn.execute("BEGIN")
        envelope = api.dispatch("gx_run_status", {"run_id": run_id}, principal=principal,
                                conn=conn, transport="cli")
        if not envelope.get("ok"):
            return envelope
        board_ctx = context.Ctx(conn, principal, tool="gx_shared_board", transport="cli")
        board = SharedBoardService(conn, ctx=board_ctx).snapshot(run_id=run_id, after_event_id=after,
                                                   event_limit=event_limit)
        run = board.get("run") or {}
        return {**envelope, "revision": run.get("revision", envelope.get("revision")),
                "status": run.get("status", envelope.get("status")), "data": board,
                "artifact_refs": [], "warnings": list(envelope.get("warnings") or [])}
    except GxError as exc:
        return api.envelope.failure(f"board_{uuid.uuid4().hex}", exc, run_id=run_id)
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return html.escape(str(value), quote=True)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th scope='col'>{_text(v)}</th>" for v in headers)
    body = "".join("<tr>" + "".join(f"<td>{_text(v)}</td>" for v in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if rows else "<p>None recorded.</p>"


def _organization_sections(board: Mapping[str, Any]) -> str:
    """Render the bounded P1 projections with the same escaping as legacy tables."""
    from ..teams.board import MAX_BOARD_ROWS

    organization, research, host = (board.get(name) or {} for name in ("organization", "research", "host_pool"))

    def page(section: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
        return list(section.get(name) or [])[:MAX_BOARD_ROWS]

    teams = _table(["Team / parent", "Status", "Leader / generation", "Context / budget"], [
        [str(t.get("team_id")) + " / " + str(t.get("parent_team_id")), t.get("status"),
         str(t.get("leader_subject")) + " / " + str(t.get("leader_generation")),
         str(t.get("context_ref")) + " / " + str(t.get("budget_ref"))] for t in page(organization, "teams")])
    memberships = _table(["Team", "Member", "Status", "Observed worker"], [
        [m.get("team_id"), m.get("subject"), m.get("status"), m.get("observed_worker_ref")]
        for m in page(organization, "memberships")])
    assignments = _table(["Task / team", "Worker", "Generation", "Status / handoff"], [
        [str(a.get("task_id")) + " / " + str(a.get("team_id")), a.get("subject"), a.get("assignment_generation"),
         str(a.get("status")) + " / " + str(a.get("handoff_pending"))] for a in page(organization, "assignments")])
    messages = _table(["Message / kind", "Sender / recipient", "Task / context", "Acknowledgements"], [
        [str(m.get("message_id")) + " / " + str(m.get("kind")),
         str(m.get("sender_team_id")) + " / " + str(m.get("recipient_team_id")),
         str(m.get("task_id")) + " / " + str(m.get("context_ref")), m.get("acknowledged_count")]
        for m in page(organization, "messages")])
    hypotheses = _table(["Hypothesis / revision", "Statement / scope", "Evidence counts"], [
        [h.get("ref"), str(h.get("statement")) + " / " + str(h.get("scope")), h.get("evidence_counts")]
        for h in page(research, "hypotheses")])
    rounds = _table(["Round / index", "Context", "Status", "Committed summary"], [
        [str(r.get("round_id")) + " / " + str(r.get("round_index")), r.get("context_ref"), r.get("status"),
         r.get("committed_proposal_id")] for r in page(research, "rounds")])
    summaries = _table(["Summary / status", "Round", "Interpretation", "Result dispositions"], [
        [str(s.get("proposal_id")) + " / " + str(s.get("status")), s.get("round_id"), s.get("interpretation"),
         s.get("disposition_counts")] for s in page(research, "summaries")])
    pools = _table(["Pool / host", "Occupied", "Admission capacity / available", "Slot states / external"], [
        [str(p.get("pool_id")) + " / " + str(p.get("host")), p.get("occupied"),
         str(p.get("admission_capacity")) + " / " + str(p.get("available")),
         str(p.get("counts")) + " / " + str(p.get("external_occupied"))] for p in page(host, "pools")])
    slots = _table(["Slot / run", "State", "Native agent / parent / depth", "Logical parent / depth"], [
        [str(s.get("slot_id")) + " / " + str(s.get("run_id")), s.get("state"),
         str(s.get("native_agent_ref")) + " / " + str(s.get("native_parent_ref")) + " / " + str(s.get("native_depth")),
         str(s.get("logical_parent_slot_id")) + " / " + str(s.get("logical_depth"))] for s in page(host, "slots")])
    cursors = {name: {key: value.get(key) for key in ("counts", "truncated", "cursors")}
               for name, value in (("organization", organization), ("research", research), ("host_pool", host))}
    return (f"<section><h2>Explicit teams</h2>{teams}<h3>Memberships</h3>{memberships}"
            f"<h3>Assignments</h3>{assignments}<h3>Team messages</h3>{messages}</section>"
            f"<section><h2>Hypotheses</h2>{hypotheses}<p>Current research context: "
            f"{_text(research.get('current_context'))}</p><h3>Research rounds</h3>{rounds}"
            f"<h3>Round summaries</h3>{summaries}</section>"
            f"<section><h2>Host pool occupancy</h2>{pools}<p>{_text(host.get('occupancy_basis'))}</p>{slots}</section>"
            f"<details><summary>Organization, research and Host pagination</summary><pre>{_text(cursors)}</pre></details>")


def render_html(board: Mapping[str, Any], *, refresh_seconds: float | None = None) -> str:
    """Render only board metadata; source bodies and executable artifact bytes are absent."""
    run = board.get("run") or {}
    tasks = list(board.get("tasks") or [])
    teams = list(board.get("teams") or [])
    refresh = "" if refresh_seconds is None else f"<meta http-equiv='refresh' content='{max(1, int(refresh_seconds))}'>"
    status = run.get("status")
    header = (f"<p class='eyebrow'>GX · Shared run board</p><h1>{_text(run.get('run_id'))}</h1>"
              f"<p><strong>{_text(status)}</strong> · revision {_text(run.get('revision'))} · "
              f"event {_text(board.get('as_of_event_id'))}</p>"
              f"<p>Stage {_text(run.get('stage') or run.get('phase'))} · round {_text(run.get('round_index'))}</p>"
              f"<p>{_text(run.get('objective'))}</p>"
              f"<p>Snapshot {_text(board.get('as_of'))}. This view does not advance the run. "
              "Team grouping is logical; worker identity uses the recorded observation level.</p>")
    cards = "".join(f"<article><span>{label}</span><strong>{value}</strong></article>" for label, value in
                    [("Task groups", len(teams)), ("Tasks", len(tasks)),
                     ("Worker observations", len(board.get("workers") or [])), ("Jobs", len(board.get("jobs") or []))])
    team_table = _table(["Team", "Grouping", "Tasks", "Status counts"],
                        [[t.get("team_id"), t.get("basis"), len(t.get("task_ids") or []), t.get("counts")]
                         for t in teams])
    task_table = _table(["Task / team", "Kind / role", "Status", "Attempts", "Lease", "Needs / blocking"],
                        [[str(t.get("task_id")) + " / " + str(t.get("team_id")),
                          str(t.get("kind")) + " / " + str(t.get("role")), t.get("status"),
                          str(t.get("attempt")) + " / consumed " + str(t.get("attempts_consumed")),
                          t.get("lease"), t.get("needs") or t.get("blocking")]
                         for t in tasks])
    dependencies = _table(["Task / team", "Waits for / team", "Satisfied"],
                          [[str(e.get("task_id")) + " / " + str(e.get("team_id")),
                            str(e.get("depends_on")) + " / " + str(e.get("depends_on_team_id")), e.get("satisfied")]
                           for e in board.get("dependencies") or []])
    workers = _table(["Worker / task", "Host / state", "Reported", "Observed", "Evidence / policy"],
                     [[str(w.get("principal")) + " / " + str(w.get("task_id")),
                       str(w.get("host")) + " / " + str(w.get("state")), w.get("reported"), w.get("observed"),
                       str(w.get("evidence_level")) + " / " + str(w.get("policy_verdict"))]
                      for w in board.get("workers") or []])
    events = _table(["Event", "Kind", "Subject", "Reason"],
                    [[e.get("event_id"), e.get("kind"), e.get("subject_id"), e.get("reason")]
                     for e in board.get("events") or []])
    sections = (_organization_sections(board) +
                f"<section><h2>Teams</h2>{team_table}</section><section><h2>Tasks</h2>{task_table}</section>"
                f"<section><h2>Dependencies</h2>{dependencies}</section><section><h2>Workers</h2>{workers}</section>"
                f"<section><h2>Waiting and feedback</h2><pre>{_text(board.get('needs'))}</pre></section>"
                f"<details><summary>Capacity, budget and dispatch</summary><pre>"
                f"{_text({k: board.get(k) for k in ('capacity', 'budget', 'dispatch')})}</pre></details>"
                f"<details><summary>Jobs and approvals</summary><pre>"
                f"{_text({k: board.get(k) for k in ('jobs', 'approvals')})}</pre></details>"
                f"<details><summary>Reactions and loop decisions</summary><pre>"
                f"{_text({k: board.get(k) for k in ('reactions', 'feedback')})}</pre></details>"
                f"<section><h2>Committed events</h2>{events}<pre>Cursor {_text(board.get('event_cursor'))}</pre></section>")
    style = """
    :root{color-scheme:light dark;font:15px system-ui,sans-serif;background:#101821;color:#e6edf3}
    body{max-width:1440px;margin:auto;padding:32px}h1{overflow-wrap:anywhere;font-size:26px}
    .eyebrow{color:#75d6c6;letter-spacing:.12em}p{line-height:1.6;color:#bdc9d5}
    .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:28px 0}
    article,section,details{background:#17232f;border:1px solid #304152;border-radius:10px;padding:18px;margin-bottom:18px}
    article span{display:block;color:#a9becf}article strong{font-size:30px}section{overflow:auto}
    table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:12px;border-bottom:1px solid #304152;vertical-align:top}
    td{max-width:400px;overflow-wrap:anywhere;font:13px ui-monospace,monospace}th{color:#8dddcf}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.5}summary{cursor:pointer}
    @media(max-width:700px){body{padding:14px}.cards{grid-template-columns:repeat(2,1fr)}th,td{padding:7px}}
    """
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
            f"{refresh}<title>GX run board</title><style>{style}</style></head>"
            f"<body><header>{header}</header><div class='cards'>{cards}</div><main>{sections}</main></body></html>")


def _owned_snapshot(path: Path, marker: bytes) -> tuple[Any, ...] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        identity = os.fstat(handle.fileno())
        if not stat.S_ISREG(identity.st_mode):
            raise ValueError("the board output is not a regular file")
        body = handle.read()
    if not body.startswith(marker):
        raise ValueError("the board output already exists and was not written by this view")
    return (identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns,
            hashlib.sha256(body).hexdigest())


def publish_html(board: Mapping[str, Any], *, refresh_seconds: float | None = None) -> Path:
    run_id = str((board.get("run") or {}).get("run_id") or "")
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        raise ValueError("the run id cannot name a board file")
    database = context.store_path()
    if str(database) == ":memory:":
        raise ValueError("HTML publication needs a persistent store location")
    root = Path(database).resolve().parent / "boards"
    if root.is_symlink():
        raise ValueError("the board output directory is a link")
    root.mkdir(exist_ok=True)
    target = root / f"{run_id}.html"
    marker = f"<!-- gx-board run={run_id} -->\n".encode()
    lock = os.open(root / f".{run_id}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    temporary = None
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("the board publication lock is not a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another view is publishing this board; refresh again") from exc
        before = _owned_snapshot(target, marker)
        body = marker + render_html(board, refresh_seconds=refresh_seconds).encode()
        descriptor, name = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=root)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if before is None:
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise ValueError("the board output appeared during publication; its bytes were preserved") from exc
        else:
            if _owned_snapshot(target, marker) != before:
                raise ValueError("the board output changed during publication; its bytes were preserved")
            os.replace(temporary, target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        os.close(lock)


def command(args: Any, principal: Principal) -> int:
    from .main import _emit, _exit_for

    if args.after < 0 or not 1 <= args.event_limit <= 1000:
        raise ValueError("after is non-negative and event-limit is between 1 and 1000")
    if not 0.1 <= args.interval <= 60 or args.iterations is not None and args.iterations < 1:
        raise ValueError("interval is between 0.1 and 60 seconds; iterations is positive")
    count, cursor = 0, args.after
    try:
        while True:
            envelope = read_board(principal, args.run_id, after=cursor, event_limit=args.event_limit)
            if envelope.get("ok") and args.html:
                path = publish_html(envelope["data"], refresh_seconds=args.interval if args.watch else None)
                envelope["data"]["html_snapshot"] = str(path)
            _emit(envelope, pretty=args.pretty)
            count += 1
            if not envelope.get("ok") or not args.watch or args.iterations is not None and count >= args.iterations:
                return _exit_for(envelope)
            cursor = int((envelope["data"].get("event_cursor") or {}).get("next_event_id") or cursor)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
