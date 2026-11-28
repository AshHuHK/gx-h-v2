"""One command to admit, drive, resume and report a Goal through the native research runtime."""
from contextlib import closing
import json
import math
from pathlib import Path
import shlex
import sys

from ..domain import machines
from ..native_release import native_state, ReleaseStateError
from ..persistence import store
from . import operator, workspace


def add_command(commands):
    parser = commands.add_parser("research", help="Run or resume a scientific Goal and export its findings")
    parser.add_argument("--state", required=True, type=Path)
    labelled = parser.add_mutually_exclusive_group(required=True)
    labelled.add_argument("--run-key", help="Persistent name for this study")
    labelled.add_argument("--run-id", help="Drive or export a previously admitted study, using its saved Goal")
    question = parser.add_mutually_exclusive_group()
    question.add_argument("--goal")
    question.add_argument("--goal-file", type=Path, help="Read the complete UTF-8 scientific question")
    question.add_argument("--task-card", type=Path, help="Read the question, acceptance and stopping criteria from JSON")
    parser.add_argument("--goal-config", type=Path, help="Resource bounds, allowed research methods and materials")
    parser.add_argument("--research-effort", choices=("quick", "medium", "deep"))
    parser.add_argument("--timeout", type=float, default=math.inf, help="Optional drive time in seconds")
    parser.set_defaults(input=None, capability=None)


def counts(conn, rid):
    return {
        "jobs": conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0],
        "host_turns": conn.execute("""SELECT count(*) FROM (SELECT DISTINCT thread_id,turn_id
            FROM p0_root_deliveries WHERE run_id=? AND method='turn/start' AND state='acknowledged'
            AND turn_id IS NOT NULL)""", (rid,)).fetchone()[0],
    }


def command(args, directory):
    parts = [str(Path(sys.argv[0]).resolve()), "research", "--state", str(directory)]
    if getattr(args, "run_id", None):
        parts.extend(["--run-id", args.run_id])
    else:
        parts.extend(["--run-key", args.run_key])
    if getattr(args, "task_card", None):
        parts.extend(["--task-card", str(args.task_card.resolve())])
    elif args.goal_file:
        parts.extend(["--goal-file", str(args.goal_file.resolve())])
    elif args.goal:
        parts.extend(["--goal", args.goal])
    if args.goal_config:
        parts.extend(["--goal-config", str(args.goal_config.resolve())])
    if args.research_effort:
        parts.extend(["--research-effort", args.research_effort])
    if math.isfinite(args.timeout):
        parts.extend(["--timeout", str(args.timeout)])
    return shlex.join(parts)


def _task_goal(card, *, indent_continuations=True):
    sections = [card["question"]]
    for key, label in (("acceptance", "Acceptance criteria"), ("stop_when", "Stopping conditions")):
        lines = [value.replace("\n", "\n  ") if indent_continuations else value for value in card[key]]
        sections.append(label + "\n" + "\n".join("- " + line for line in lines))
    return "\n\n".join(sections)


def _same_goal(saved, requested):
    # New CLI defaults never upgrade the frozen contract when the original command is replayed.
    for policy in ("discovery_policy", "harness_policy"):
        if policy not in saved and policy in requested:
            requested = {key: value for key, value in requested.items() if key != policy}
    if "task_card" not in saved and "task_card" in requested:
        # Earlier releases stored only the rendered terms. Keep that exact Goal resumable.
        card = requested["task_card"]
        requested = {key: value for key, value in requested.items() if key != "task_card"}
        if requested["objective"] != saved.get("objective"):
            requested["objective"] = _task_goal(card, indent_continuations=False)
    return saved == requested


def _read_task_card(args):
    card = json.loads(args.task_card.read_text(encoding="utf-8"))
    fields = {"question", "acceptance", "stop_when", "effort"}
    if not isinstance(card, dict) or set(card) - fields or not isinstance(card.get("question"), str) or not card["question"].strip():
        raise ValueError("A task card needs a scientific question, acceptance and stop_when; effort is optional")
    normalized = {"question": card["question"].strip()}
    for key in ("acceptance", "stop_when"):
        values = card.get(key)
        if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"Task card {key} must list at least one nonempty criterion")
        normalized[key] = [value.strip() for value in values]
    if "effort" in card:
        if card["effort"] not in ("quick", "medium", "deep"):
            raise ValueError("Task card effort must be quick, medium or deep")
        if args.research_effort and args.research_effort != card["effort"]:
            raise ValueError("Task card and command line specify different research effort")
        args.research_effort = normalized["effort"] = card["effort"]
    return _task_goal(normalized), normalized


def execute(args, directory):
    from . import cli, research_report
    task_card = None
    if getattr(args, "task_card", None):
        args.goal, task_card = _read_task_card(args)
    elif args.goal_file:
        args.goal = args.goal_file.read_text(encoding="utf-8")
    rid = getattr(args, "run_id", None)
    if rid and any((args.goal, args.goal_config, args.research_effort)):
        raise ValueError("Use --run-id alone to resume the saved study")
    if not rid and not args.goal:
        raise ValueError("Supply --goal, --goal-file or --task-card for a named study")
    spec = None if rid else cli.start_specification(args)
    if task_card is not None:
        spec["task_card"] = task_card
    resume_command = command(args, directory)
    try:
        with native_state(directory), closing(store.open_store(directory/"gx.db")) as conn:
            previous = workspace.find_run(conn, run_key=args.run_key, run_id=rid)
            if rid:
                spec = store.loads(previous, "inputs_json")["inputs"]["p0_spec"]
            operator.authorize_project(operator.local_context(conn), spec["project"])
            before = counts(conn, previous["run_id"]) if previous else {"jobs": 0, "host_turns": 0}
            if previous:
                saved = store.loads(previous, "inputs_json")["inputs"]["p0_spec"]
                if not _same_goal(saved, spec):
                    raise ValueError("This run key belongs to a different Goal specification")
            if previous and conn.execute("SELECT 1 FROM v6_goals WHERE run_id=? AND status='CLOSED'",
                                         (previous["run_id"],)).fetchone():
                status = {"run_id":previous["run_id"], "status":previous["status"], "goal_status":"CLOSED"}
                action = "export_closed"
            else:
                status = (cli.snapshot(conn, previous["run_id"]) if previous else
                          cli.start(conn, directory, spec, run_key=args.run_key))
                rid = status["run_id"]
                action = "resume" if previous else "start"
                run = cli.project(conn, rid)
                if run["status"] == "PAUSED":
                    cli.change_state(conn, run, "continue")
                if run["status"] not in machines.terminal_states("run"):
                    status = cli.run_until_stopped(conn, directory, rid, args.timeout)
            rid = status["run_id"]
            after = counts(conn, rid)
            invocation = {"action":action, "before":before, "after":after,
                          "added":{key:after[key]-before[key] for key in before}, "command":resume_command}
            if status["goal_status"] == "CLOSED":
                return {"run_id":rid, "goal_status":"CLOSED", "status":status["status"],
                        **research_report.export(conn, rid, directory, invocation)}
            return {**status, "invocation":invocation}
    except ReleaseStateError as error:
        if "another native client" not in str(error) or not (directory/"gx.db").exists():
            raise
        with closing(cli.existing_db(directory, readonly=True)) as conn:
            run = workspace.find_run(conn, run_key=args.run_key, run_id=getattr(args, "run_id", None))
            if getattr(args, "run_id", None):
                spec = store.loads(run, "inputs_json")["inputs"]["p0_spec"]
            operator.authorize_project(operator.local_context(conn), spec["project"])
            if not run or not _same_goal(store.loads(run, "inputs_json")["inputs"]["p0_spec"], spec):
                raise error
            return {**cli.snapshot(conn, run["run_id"]), "driver":"another_native_client",
                    "resume_command":resume_command}
