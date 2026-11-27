"""Durable user additions and delivery receipts for an existing GX Root."""

import json
import sqlite3
from contextlib import closing


INBOX_FILE = "web-messages.sqlite3"


def enqueue(state, ident, text):
    path = state / INBOX_FILE
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY, body TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
            thread TEXT, turn TEXT)""")
        db.execute("INSERT OR IGNORE INTO messages(id,body) VALUES(?,?)", (ident, text))


def inbox(state):
    path = state / INBOX_FILE
    if not path.is_file():
        return None
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
        return db
    db.close()
    return None


def last_entry(state):
    db = inbox(state)
    if db is None:
        return 0
    with closing(db):
        return db.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]


def _commission_start(state):
    try:
        settings = json.loads((state / "project-driver.json").read_bytes())
    except FileNotFoundError:
        return 0
    phase = settings.get("follow_ups", {}).get(settings.get("active_follow_up"), {})
    return phase.get("feedback_after", 0)


def has_queued(state):
    db = inbox(state)
    if db is None:
        return False
    with closing(db):
        return db.execute("SELECT 1 FROM messages WHERE phase='queued' AND rowid>? LIMIT 1",
                          (_commission_start(state),)).fetchone() is not None


def delivery_status(state, ident):
    db = inbox(state)
    if db is not None:
        with closing(db):
            row = db.execute("SELECT phase FROM messages WHERE id=?", (ident,)).fetchone()
            return row[0] if row else None


def take_for_turn(state, thread, *, steer_turn=None):
    db = inbox(state)
    if db is None:
        return []
    with closing(db), db:
        db.execute("BEGIN IMMEDIATE")
        # One live steer has one official clientUserMessageId. Ordinary turn
        # starts retain the existing batched text and its legacy receipts.
        rows = db.execute(
            "SELECT id,body FROM messages WHERE phase='queued' AND rowid>? ORDER BY rowid LIMIT ?",
            (_commission_start(state), 1 if steer_turn else 4),
        ).fetchall()
        db.executemany(
            "UPDATE messages SET phase=?,thread=?,turn=? WHERE id=?",
            [("steering" if steer_turn else "sending", thread, steer_turn, k) for k, _ in rows],
        )
        return [{"id": k, "text": v} for k, v in rows]


def update_delivery(state, rows, phase, turn):
    if not rows:
        return
    db = inbox(state)
    if db is not None:
        with closing(db), db:
            db.executemany(
                "UPDATE messages SET phase=?,turn=? WHERE id=?", [(phase, turn, r["id"]) for r in rows]
            )


def return_refused_steer(state, ident, thread, turn):
    """Only a definite refusal of this outstanding delivery permits a handoff."""
    db = inbox(state)
    if db is not None:
        with closing(db), db:
            db.execute("UPDATE messages SET phase='queued',thread=NULL,turn=NULL"
                       " WHERE id=? AND phase='steering' AND thread=? AND turn=?", (ident, thread, turn))


def attach_to_input(inputs, rows):
    if rows:
        return [
            *inputs,
            {
                "type": "text",
                "text": "项目用户追加要求。请在原预算和现有实验记录上处置并说明结果。\n"
                + json.dumps(rows, ensure_ascii=False),
            },
        ]
    return inputs


def _entries(read, params):
    cursor = None
    while True:
        page = read({**params, **({"cursor": cursor} if cursor else {})})
        yield from page.get("data") or []
        following = page.get("nextCursor")
        if not following or following == cursor:
            return
        cursor = following


def _saved_items(thread, turn, list_items):
    if list_items is None:
        yield from turn.get("items") or []
        return
    # Query only the turn which can contain this input. Large command and
    # tool outputs are irrelevant to the original userMessage receipt.
    entries = _entries(list_items, {"threadId": thread, "turnId": turn["id"], "limit": 32,
        "sortDirection": "desc", "deferOutput": {"commandExecution": True, "mcpToolCall": True}})
    for entry in entries:
        if entry.get("turnId") == turn["id"]:
            yield entry["item"]


def _submitted_rows(item):
    for content in item.get("content") or []:
        text = content.get("text")
        if not isinstance(text, str) or not text.startswith("项目用户追加要求。"):
            continue
        try:
            submitted = json.loads(text.split("\n", 1)[1])
        except (IndexError, ValueError):
            continue
        if isinstance(submitted, list):
            yield from (r for r in submitted if isinstance(r, dict) and isinstance(r.get("id"), str))


def reconcile(state, thread, list_turns, list_items=None):
    """Confirm a lost reply from this Root's persisted input, without replay."""
    db = inbox(state)
    if db is None:
        return
    with closing(db):
        batches = {}
        targeted = {}
        for key, body, phase, turn in db.execute(
                "SELECT id,body,phase,turn FROM messages WHERE thread=? AND phase IN ('sending','steering','received')",
                (thread,)):
            if turn:
                targeted.setdefault(turn, {})[key] = phase
            else:
                batches[key] = body
        if not batches and not targeted:
            return
        turns = _entries(list_turns, {"threadId": thread, "sortDirection": "desc",
            "limit": 100 if list_items else 2, "itemsView": "notLoaded" if list_items else "full"})
        for index, turn in enumerate(turns):
            turn_id = turn.get("id")
            if not turn_id:
                continue
            # Unacknowledged batched turn/start inputs have no turn ID. Retain
            # their existing eight-turn search and address steers by exact turn,
            # including older ones, without loading intervening conversations.
            scan_batch = bool(batches) and index < 8
            messages = targeted.pop(turn_id, {})
            if messages or scan_batch:
                received = {k for k, phase in messages.items() if phase == "received"}
                steering = messages.keys() - received
                if steering or scan_batch:
                    for item in _saved_items(thread, turn, list_items):
                        if item.get("type") != "userMessage":
                            continue
                        key = item.get("clientId")
                        if isinstance(key, str) and key in steering:
                            received.add(key)
                            steering.remove(key)
                        if scan_batch:
                            for row in _submitted_rows(item):
                                key = row["id"]
                                if key in batches and batches[key] == row.get("text"):
                                    received.add(key)
                                    del batches[key]
                        if not steering and not (scan_batch and batches):
                            break
                phase = "turn_completed" if turn.get("status") == "completed" else "received"
                with db:
                    db.executemany("UPDATE messages SET phase=?,turn=? WHERE id=?",
                                   [(phase, turn_id, k) for k in received])
            if not targeted and (not batches or index >= 7):
                break


def deliveries(state):
    db = inbox(state)
    if db is None:
        return []
    with closing(db):
        return [
            {"id": k, "status": phase, "turn": turn}
            for k, phase, turn in db.execute(
                "SELECT id, CASE WHEN phase='queued' AND rowid<=? THEN 'previous_commission_closed'"
                " WHEN phase='steering' THEN 'sending' ELSE phase END, turn"
                " FROM messages ORDER BY rowid DESC LIMIT 30", (_commission_start(state),)
            )
        ]
