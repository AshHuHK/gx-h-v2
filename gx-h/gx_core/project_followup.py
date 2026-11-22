"""Persist a commissioned phase and find the GX run registered by its native Root."""
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

from . import budgets
from .domain import machines
from .jobs import service as jobs
from .native_cli import _open_store
from .native_release import native_state, write_native_bytes
from .persistence import store
from .project_work import _project_runs

PROMPT_LIMIT = 256 * 1024


def read_prompt(path):
    with Path(path).open("rb") as stream:
        data = stream.read(PROMPT_LIMIT + 1)
    if len(data) > PROMPT_LIMIT or not data.decode("utf-8").strip():
        raise ValueError("the project prompt must contain text within the native prompt limit")
    return data


HOST_LINES = ("max_sessions", "max_wall_seconds")
RESOURCE_NAMES = {
    "科研 Job 总数": "job_submissions", "GPU Job 数": "gpu_jobs", "GPU 分钟": "gpu_minutes",
    "CPU 秒": "cpu_seconds", "Worker 任务数": "worker_tasks",
    "Codex 会话数": "max_sessions", "墙钟秒数": "max_wall_seconds",
}


def _budget_sections(source):
    """Locate top-level budget headings without treating code examples as declarations."""
    raw = source.lstrip("\ufeff").splitlines()
    visible, sections = [], {}
    fence = section = None
    for index, line in enumerate(raw):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = None
            visible.append("")
            continue
        if marker:
            fence = marker[1]
            visible.append("")
            continue
        heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|$)", line)
        if heading and len(heading[1]) <= 2:
            if section:
                sections[section] = slice(sections[section].start, index)
                section = None
            title = re.sub(r"[ \t]+#+$", "", (heading[2] or "").strip())
            title = " ".join(title.split())
            if heading[1] == "##" and title in ("GX 预算", "模型预算"):
                if title in sections:
                    raise ValueError(f"duplicate ## {title} sections")
                section = title
                sections[title] = slice(index + 1, None)
        visible.append("" if line.startswith(("    ", "\t", ">")) else line)
    return raw, visible, sections


def _cells(line):
    # A literal pipe in an explanatory cell can be escaped in a Markdown table.
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().removeprefix("|").removesuffix("|"))]


def _markdown_terms(source, dedicated=False):
    raw, visible, sections = _budget_sections(source)
    model = None
    if "模型预算" in sections:
        span = sections["模型预算"]
        model = "\n".join(raw[span]).strip("\n")
        if not model.strip():
            raise ValueError("empty ## 模型预算 section")
        # An example resource table inside scientific instructions cannot set the ledger.
        visible[span] = [""] * len(visible[span])
    if "GX 预算" in sections:
        lines = visible[sections["GX 预算"]]
    elif dedicated:
        lines = visible
    else:
        return None, model
    headers = [i for i, line in enumerate(lines) if _cells(line)[:2] == ["资源", "上限"]]
    if not headers and model and "GX 预算" not in sections:
        return None, model
    if len(headers) != 1:
        raise ValueError("the Markdown budget needs exactly one 资源 | 上限 table")
    index = headers[0]
    header = _cells(lines[index])
    rule = _cells(lines[index + 1]) if index + 1 < len(lines) else []
    if (len(header) not in (2, 3) or not all(header) or len(rule) != len(header)
            or any(not re.fullmatch(r":?-+:?", part) for part in rule)):
        raise ValueError("invalid 资源 | 上限 table header or Markdown delimiter row")
    amounts = {}
    for line in lines[index + 2:]:
        if "|" not in line:
            break
        cells = _cells(line)
        if not 2 <= len(cells) <= len(header):
            raise ValueError(f"invalid Markdown budget row {line!r}")
        name, value = cells[:2]
        if name.startswith("`") and name.endswith("`"):
            name = name[1:-1].strip()
        name = RESOURCE_NAMES.get(" ".join(name.split()), name)
        if name not in (*budgets.PROJECT_LINES, *HOST_LINES):
            raise ValueError(f"unknown Markdown budget resource {name!r}")
        if name in amounts:
            raise ValueError(f"duplicate Markdown budget resource {name!r}")
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError(f"Markdown budget {name} must be a nonnegative integer")
        amounts[name] = int(value)
        if name in HOST_LINES and amounts[name] < 1:
            raise ValueError(f"Markdown budget {name} must be a positive integer")
    budgets.check_project_limits({k: v for k, v in amounts.items() if k not in HOST_LINES})
    return amounts, model


def markdown_budget(source, *, dedicated=False):
    return _markdown_terms(source, dedicated)[0]


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON budget resource {key!r}")
        result[key] = value
    return result


def commission_budget(prompt, prompt_data, budget_file):
    embedded, model = _markdown_terms(prompt_data.decode()) if prompt_data is not None else (None, None)
    amounts = dict(embedded or {})
    if budget_file is not None:
        path = Path(budget_file)
        source = (prompt_data.decode() if prompt is not None and Path(prompt).resolve() == path.resolve()
                  else path.read_text(encoding="utf-8-sig"))
        suffix = path.suffix.lower()
        if suffix == ".json" or (suffix not in (".md", ".markdown") and source.lstrip().startswith("{")):
            selected = budgets.check_project_limits(json.loads(source, object_pairs_hook=_unique_json))
        else:
            selected, separate_model = _markdown_terms(source, dedicated=True)
            if model is not None and separate_model is not None and model != separate_model:
                raise ValueError("--budget-file conflicts with the embedded 模型预算")
            if separate_model is not None:
                model = separate_model
        for key, value in (selected or {}).items():
            if key in amounts and amounts[key] != value:
                raise ValueError(f"--budget-file conflicts with the embedded GX 预算 for {key}")
            amounts[key] = value
    limits = {k: amounts[k] for k in budgets.PROJECT_LINES} if amounts else None
    return limits, {k: amounts[k] for k in HOST_LINES if k in amounts}, model


def commission_limits(prompt, prompt_data, budget_file):
    return commission_budget(prompt, prompt_data, budget_file)[:2]


def check_model_budget(model_section):
    if model_section is None:
        return
    _, visible, _ = _budget_sections(model_section)
    if any(re.match(r" {0,3}资源估算方法[ \t]*[：:]", line) for line in visible):
        raise ValueError("automatic resource estimates are unavailable; specify limits in ## GX 预算 or --budget-file")


def current_phase(record):
    return record.get("follow_ups", {}).get(record.get("active_follow_up"))


def create_phase(state, record, key, predecessor, launch_count, prompt_data, limits, allowances):
    if prompt_data is None:
        raise ValueError("a new follow-up needs --prompt-file")
    # User keys can contain file separators or non-ASCII text.
    directory = state / "follow-ups" / hashlib.sha256(key.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = directory / "commission.md"
    write_native_bytes(snapshot, prompt_data)
    business_key = "gx-project-follow-up:" + quote(record["project"], safe="") + ":" + quote(key, safe="")
    phase = {"predecessor_run_id": predecessor, "idempotency_key": business_key, "run_id": None,
             "prompt_file": str(snapshot), "budget_limits": limits, "started_at": time.time(),
             "baseline_launches": launch_count, **allowances}
    record.setdefault("follow_ups", {})[key] = phase
    record["active_follow_up"] = key
    record.pop("dispatch", None)
    return phase


def verify_reuse(phase, prompt_data, limits, host_limits):
    if prompt_data is not None and prompt_data != Path(phase["prompt_file"]).read_bytes():
        raise ValueError("this follow-up key already has a different commission. Select a new --follow-up KEY for a different commission.")
    if limits is not None and limits != phase["budget_limits"]:
        raise ValueError("this follow-up key already has a different scientific budget")
    for name, amount in host_limits.items():
        if amount is not None and phase[name] != amount:
            raise ValueError(f"this follow-up key already has a different {name} allowance")


def registered_successor(conn, project, phase):
    row = conn.execute(
        "SELECT i.scope,i.side_effect_status,i.completed_at,r.run_id,r.project_ref,"
        " json_extract(r.inputs_json,'$.inputs.parent_run_id') FROM idempotency i"
        " LEFT JOIN runs r ON r.run_id=json_extract(i.response_json,'$.run_id')"
        " WHERE i.idempotency_key=?", (store.tool_key("gx_run_start", phase["idempotency_key"]),)
    ).fetchone()
    if row is None:
        return None
    scope, effect, completed, run_id, actual_project, predecessor = row
    if scope != "gx_run_start" or effect != "APPLIED" or not completed or not run_id:
        raise ValueError("the follow-up run-start receipt needs reconciliation before another launch")
    if actual_project != project or predecessor != phase["predecessor_run_id"] or run_id == predecessor:
        raise ValueError("the registered follow-up belongs to a different project or predecessor")
    return run_id


def open_work(conn, project):
    """Find other live branches, outstanding external Jobs or held scientific work."""
    runs, _, settled_probes, _, _ = _project_runs(conn, project, None)
    terminal_runs = machines.terminal_states("run")
    open_runs = [row["run_id"] for row in runs.values() if row["status"] not in terminal_runs
                 and row["run_id"] not in settled_probes]
    open_jobs = [row[0] for row in conn.execute(
        "SELECT j.job_id FROM jobs j JOIN runs r USING(run_id) WHERE r.project_ref=?"
        " AND j.status NOT IN (SELECT value FROM json_each(?)) ORDER BY j.job_id",
        (project, json.dumps(sorted(jobs.TERMINAL))))]
    held_tasks = [row[0] for row in conn.execute(
        "SELECT t.task_id FROM tasks t JOIN runs r USING(run_id) JOIN leases l ON l.lease_id=t.lease_id"
        " WHERE r.project_ref=? AND l.released_at IS NULL AND t.status NOT IN (SELECT value FROM json_each(?))"
        " ORDER BY t.task_id", (project, json.dumps(sorted(machines.terminal_states("task")))))]
    return {name: rows for name, rows in (("runs", open_runs), ("jobs", open_jobs), ("tasks", held_tasks)) if rows}


def save_settings(state, record):
    write_native_bytes(state / "project-driver.json", json.dumps(record).encode())


def publish_commission(state, record, limits, phase_id, current):
    if limits is None or (current and current["phase_id"] == phase_id and current["limits"] == limits):
        save_settings(state, record)
        return current
    # Reject unfundable commissions before selecting them. Persist the snapshot and
    # its original host deadline before committing the ledger. An interrupted commit
    # can then be retried from the saved inputs before any Host is launched.
    with native_state(state) as owned, _open_store(owned) as conn, store.transaction(conn):
        budget = budgets.activate_project_budget(conn, record["project"], limits, phase_id=phase_id)
        save_settings(state, record)
        return budget


def save_model_budget(state, record, phase, current_budget):
    """Publish the portable budget Markdown alongside the saved commission."""
    subject = phase or record
    path = ((Path(phase["prompt_file"]).parent if phase else state / "commissions" / "initial") / "budget.md")
    lines = ["# 项目预算", ""]
    limits = subject.get("budget_limits")
    if limits is not None:
        lines += ["## GX 预算", "", "| 资源 | 上限 |", "| --- | ---: |"]
        lines += [f"| `{key}` | {limits[key]} |" for key in budgets.PROJECT_LINES]
    else:
        if current_budget:
            lines += ["## 沿用的项目额度", "", f"预算阶段 `{current_budget['phase_id']}`", "",
                      "| 资源 | 原上限 |", "| --- | ---: |"]
            lines += [f"| `{key}` | {current_budget['limits'][key]} |" for key in budgets.PROJECT_LINES]
            lines.append("")
        lines += ["## 宿主额度", "", "| 宿主资源 | 上限 |", "| --- | ---: |"]
    lines += [f"| `{key}` | {subject[key]} |" for key in HOST_LINES]
    if subject.get("lower_explicit_caps"):
        lines += ["", "## 用户指定的较低上限", "",
                  "这些项目按用户指定值执行。科研 Root 需在这些额度内安排模型工作。", "",
                  "| 资源 | 估算建议 | 执行上限 |", "| --- | ---: | ---: |"]
        lines += [f"| `{key}` | {values['recommended']} | {values['effective']} |"
                  for key, values in subject["lower_explicit_caps"].items()]
    lines += ["", "## 模型预算", "", subject["model_budget"], ""]
    contents = "\n".join(lines).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_bytes() != contents:
        write_native_bytes(path, contents)
    subject["model_budget_file"] = str(path)


def _paged_brief(state, project, sections):
    from .artifacts import ArtifactService

    namespace = "commissions/" + hashlib.sha256(project.encode()).hexdigest()
    result = ["Read every referenced section using gx_artifact_get before acting. Begin at offset 0 and "
              "request metadata.next_offset while truncated is true.".encode()]
    with native_state(state) as owned, _open_store(owned) as conn:
        service = ArtifactService(conn)
        for label, data in sections:
            if not data:
                continue
            # Keep short registration instructions and recovery details in the turn itself.
            if len(data) <= PROMPT_LIMIT // 8:
                result.append(label.encode() + b"\n" + data)
                continue
            digest = hashlib.sha256(data).hexdigest()
            name = f"{digest}.md"
            saved = conn.execute("SELECT artifact_ref FROM artifacts WHERE namespace=? AND rel_path=? "
                                 "AND sha256=? AND published=1", (namespace, name, digest)).fetchone()
            ref = saved[0] if saved else service.publish_bytes(namespace=namespace, rel_path=name,
                kind="project_launch_brief", media_type="text/markdown", data=data,
                provenance={"project_ref": project}, actor="gx-project").artifact_ref
            result.append(f"{label}: artifact_ref={ref} ({len(data)} bytes).".encode())
    return b"\n\n".join(result)


def launch_brief(snapshot, budget, *, state=None, project=None, phase=None, host=None,
                 model_terms=None, continuation=False):
    """Give Root the scientific commission, its durable location and current allowances."""
    if phase is None and budget is None and model_terms is None:
        return snapshot
    header = []
    if phase:
        header.append("This single turn registers the commissioned follow-up on the same native Root. "
                      "Choose an installed workflow whose completion evidence answers the commission. "
                      f"Call gx_run_start with idempotency_key={phase['idempotency_key']} and "
                      f"inputs.parent_run_id={phase['predecessor_run_id']}. "
                      "Record the user's commission in that run's objective. "
                      "End this turn after obtaining the successor run ID. The project driver will "
                      "continue that run and its scientific work on this Root.")
    if budget:
        header.append("The GX project budget applies across this project's runs. Its ledger is "
                      + json.dumps(budget, ensure_ascii=False, sort_keys=True) +
                      ". Include the scientific allowances in the objective and allocate work within them.")
    if host:
        header.append("Native host allowances for this phase are " + json.dumps(host, sort_keys=True))
    if model_terms:
        if model_terms.get("lower_explicit_caps"):
            header.append("The user selected ceilings below the model estimate. Plan within these effective values "
                          "and report any work they leave unfunded. " + json.dumps(model_terms["lower_explicit_caps"]))
        header.append("Use the model-budget terms to plan and track scientific model work.")
    subject = "continuation instructions" if continuation else "user commission"
    controls = "\n\n".join(header).encode()
    introduction = controls + f"\n\nUse the following complete {subject} directly as your input for this turn.\n\n".encode()
    body = snapshot.read_bytes()
    terms = model_terms["model_budget"].encode() if model_terms else b""
    excerpt = "## 模型预算\n\n".encode() + terms + b"\n\n" if terms else b""
    contents = introduction + (excerpt if terms not in body else b"") + body
    if len(contents) > PROMPT_LIMIT:
        if state is None or not project:
            raise ValueError("a paged launch brief needs its project and native state directory")
        contents = _paged_brief(state, project, [
            ("Launch instructions", controls), ("Model-budget terms", terms if terms not in body else b""),
            (subject.capitalize(), body),
        ])
    if continuation:
        target = Path(model_terms["model_budget_file"]).with_name("continue.md")
    else:
        target = snapshot.with_name("register.md" if phase else "project-bootstrap.md")
    write_native_bytes(target, contents)
    return target
