"""One command to admit, drive, resume and report a Goal through the native research runtime."""
from contextlib import closing
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
    if args.goal_file:
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


def execute(args, directory):
    from . import cli, research_report
    if args.goal_file:
        args.goal = args.goal_file.read_text(encoding="utf-8")
    rid = getattr(args, "run_id", None)
    if rid and any((args.goal, args.goal_config, args.research_effort)):
        raise ValueError("Use --run-id alone to resume the saved study")
    if not rid and not args.goal:
        raise ValueError("Supply --goal or --goal-file for a named study")
    spec = None if rid else cli.start_specification(args)
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
                if saved != spec:
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
            if not run or store.loads(run, "inputs_json")["inputs"]["p0_spec"] != spec:
                raise error
            return {**cli.snapshot(conn, run["run_id"]), "driver":"another_native_client",
                    "resume_command":resume_command}
