"""Native official Codex with a read-only study desk and the same persistent physical CPU provider as GX."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from support import BINARY, EFFORT, MODEL, METHODS, P, environment, read, save, sha
from native import settings, final_answer
from gx_core.hostobs.app_server_client import AppServerClient, AppServerError
from gx_core.p0 import profiles
from gx_core.jobs import executor, resource_guard


def tool(name, description, fields, required):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": fields,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool(
        "read_record",
        "Open current desk materials by name. The initial files are record.json (intervals) and daily.json (USGS original).",
        {"name": {"type": "string", "enum": ["record.json", "daily.json", "sites.rdb"]}},
        ["name"],
    ),
    tool(
        "list_methods",
        "List the installed water-measurement methods, their inputs, outputs and applicability.",
        {},
        [],
    ),
    tool(
        "submit_measurement",
        "Submit a detached CPU measurement using the interval record; stable method requests reuse their physical receipt. Choose independent for a new replicate. Jobs persist after a controller restart.",
        {
            "method": {"type": "string", "enum": [m for m, _ in METHODS.values()]},
            "independent": {"type": "boolean"},
        },
        ["method"],
    ),
    tool(
        "observe_measurements",
        "List physical Job receipts and retrieve all completed named measurement outputs. Use after a restart to see previously accepted work.",
        {},
        [],
    ),
]


def submit(folder, release, method, *, replicate=None):
    label = next(k for k, v in METHODS.items() if v[0] == method)
    request_sha = hashlib.sha256((method + sha(P / "record.json") + (replicate or "")).encode()).hexdigest()
    job_id = "native-" + request_sha[:22]
    root = folder / "state/jobs"
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".submit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        job = executor.paths(root, job_id)
        if job.spec.is_file():
            return {
                "job_id": job_id,
                "existing": True,
                "observed": executor.observe(job.job_dir)["state"],
            }
        if len(list(root.glob("*/runner.json"))) >= 4:
            raise ValueError("Four physical Jobs are the registered limit")
        job.create()
        out = job.job_dir / "out"
        out.mkdir()
        worker = release / "gx-h/plugins/installed" / ("org.gx.hydro-" + label) / "1.0.0/worker.py"
        checked = profiles.clash_env()
        proxy = {
            k: v
            for k, v in checked.items()
            if k.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        }
        for name in ("executor.py", "runner.py", "resource_guard.py"):
            published = release / "gx-h/gx_core/jobs" / name
            if sha(published) != sha(Path(executor.__file__).with_name(name)):
                raise RuntimeError("Pinned physical worker implementation changed")
        env = {
            **proxy,
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "HOME": str(job.home_dir),
            "TMPDIR": str(job.tmp_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "GX_CPU_THREADS": "1",
            "CUDA_VISIBLE_DEVICES": "",
        }
        policy = resource_guard.policy_for(method, {"cpu_threads": 1, "wall_seconds": 30})
        spec = {
            "schema": "gx.job_runner/2",
            "job_id": job_id,
            "argv": [
                str(release / ".venv/bin/python3"),
                "-I",
                "-B",
                str(worker),
                "--record",
                str(P / "record.json"),
                "--out",
                str(out),
            ],
            "cwd": str(out),
            "out_dir": str(out),
            "env": env,
            "stdout_path": str(job.stdout),
            "stderr_path": str(job.stderr),
            "timeout_seconds": 30,
            "heartbeat_seconds": 2.0,
            "kill_grace_seconds": 3.0,
            "resource_guard": policy,
            "cpu_affinity": [min(os.sched_getaffinity(0))],
            "limits_path": str(release / "gx-h/config/limits.json"),
            "hold_scope": "jobs",
            "network": {"mode": "clash_proxy", "proxy": "http://127.0.0.1:7890"},
            "gx_context": {
                "capability_version": method,
                "request_sha256": request_sha,
                "independent_replicate": bool(replicate),
                "input_sha256": {"record": sha(P / "record.json")},
                "worker_sha256": sha(worker),
            },
        }
        executor.write_spec(job, spec)
        return {"job_id": job_id, **executor.launch(job.job_dir)}


def jobs(folder):
    out = []
    for sp in sorted((folder / "state/jobs").glob("*/spec.json")):
        d = sp.parent
        spec = read(sp)
        method = spec["gx_context"]["capability_version"]
        name = next(o for m, o in METHODS.values() if m == method)
        item = {"job_id": d.name, "method": method, "state": executor.observe(d)["state"]}
        f = d / "out" / name
        if f.exists() and (d / "outcome.json").exists():
            item.update(output_name=name, output=read(f), sha256=sha(f))
        out.append(item)
    return out


def operate(folder, phase, release):
    env = environment(folder)
    cwd = folder / "private-empty"
    cwd.mkdir(mode=0o700, exist_ok=True)
    argv = profiles.app_server_argv(test_mode=False, provider="openai")
    argv[0] = str(BINARY)
    late = "The requested USGS station inventory arrived.\n" + (P / "sites.rdb").read_text()
    prompt = (P / "question.txt").read_text() if phase == "first" else late

    def request(msg):
        pkt = msg.message
        p = pkt.get("params", {})
        a = p.get("arguments") or {}
        name = p.get("tool")
        if pkt.get("method") != "item/tool/call":
            return {
                "success": False,
                "contentItems": [{"type": "inputText", "text": "Unsupported request."}],
            }
        try:
            if name == "read_record" and a["name"] in {
                "record.json",
                "daily.json",
                *(["sites.rdb"] if (folder / "late-delivery.json").exists() else []),
            }:
                answer = (P / a["name"]).read_text()
            elif name == "list_methods":
                answer = {
                    v[0]: read(release / "examples/plugins" / ("hydro-" + k) / "method.json")[
                        "manifest"
                    ]["method_card"]
                    for k, v in METHODS.items()
                }
            elif name == "submit_measurement" and a["method"] in {m for m, _ in METHODS.values()}:
                answer = submit(
                    folder, release, a["method"], replicate=p["callId"] if a.get("independent") else None
                )
            elif name == "observe_measurements":
                answer = jobs(folder)
            else:
                raise ValueError("The named study material or method is unavailable.")
            success = True
        except (KeyError, OSError, ValueError) as err:
            answer = str(err)
            success = False
        save(
            folder / "scientific-calls" / (str(p["callId"]) + ".json"),
            {"tool": name, "arguments": a, "turn": p["turnId"], "at": time.time(), "success": success},
        )
        return {
            "success": success,
            "contentItems": [
                {"type": "inputText", "text": answer if isinstance(answer, str) else json.dumps(answer)}
            ],
        }

    with AppServerClient(argv, cwd=cwd, env=env, on_server_request=request) as client:
        client.request(
            "initialize",
            {
                "clientInfo": {"name": "water-bulletin", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized", {})
        account = client.request("account/read", {"refreshToken": False}).get("account") or {}
        assert account.get("type") == "chatgpt"
        cfg = client.request("config/read", {"cwd": str(cwd), "includeLayers": False})["config"]
        profiles.provider_endpoint("openai", cfg, test_mode=False, environ=env)
        par = settings(cwd, cfg, False)
        role = "Act as the scientific lead. Use the supplied archived records, and use the available measurement instruments or delegate within this workspace when you judge that appropriate. Submit a report that answers the desk question."
        par.update(baseInstructions=role, developerInstructions=role, dynamicTools=TOOLS)
        resume = phase == "second"
        if resume:
            for k in ("dynamicTools", "environments", "experimentalRawEvents"):
                par.pop(k, None)
            par["threadId"] = read(folder / "thread.json")["id"]
        thread = client.request("thread/resume" if resume else "thread/start", par)["thread"]["id"]
        if resume and thread != par["threadId"]:
            raise RuntimeError("Wrong native thread resumed")
        save(
            folder / "thread.json",
            {
                "id": thread,
                "provider": "openai",
                "model": MODEL,
                "effort": EFFORT,
                "subagents_enabled": True,
            },
        )
        reply = client.request(
            "turn/start",
            {
                "threadId": thread,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "cwd": str(cwd),
                "approvalPolicy": "never",
                "model": MODEL,
                "effort": EFFORT,
                "environments": [],
            },
        )
        turn = reply["turn"]["id"]
        save(folder / (phase + "-accepted.json"), {"thread": thread, "turn": turn})
        terminal = None
        tried_steer = False
        while terminal is None:
            terminal = client.run_until(
                lambda item: item.message.get("method") == "turn/completed"
                and item.message["params"]["turn"]["id"] == turn,
                timeout=900, poll_interval=0.3,
            )
            if terminal is not None or phase != "first" or tried_steer:
                continue
            if not (folder / "late-delivery.json").exists():
                continue
            tried_steer = True
            # An ordinary late arrival can be delivered on an active native turn outside callbacks.
            # If the turn finished in the intervening instants, the same native thread gets one follow-up.
            try:
                response = client.request("turn/steer", {
                    "threadId": thread, "expectedTurnId": turn, "clientUserMessageId": "original-station-inventory",
                    "input": [{"type":"text","text":late,"text_elements":[]}],
                })
            except AppServerError as error:
                save(folder/"native-steer-refusal.json",{"turn":turn,"error":str(error)})
            else:
                save(folder/"native-steer-acknowledged.json",{"turn":turn,"receipt":response})
        record = next(
            t
            for t in client.request("thread/read", {"threadId": thread, "includeTurns": True})["thread"][
                "turns"
            ]
            if t["id"] == turn
        )
        save(
            folder / (phase + "-terminal.json"),
            {
                "turn": turn,
                "status": terminal.message["params"]["turn"]["status"],
                "answer": final_answer(record["items"]),
            },
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder", type=Path)
    p.add_argument("phase", choices=["first", "second"])
    p.add_argument("release", type=Path)
    a = p.parse_args()
    operate(a.folder, a.phase, a.release)
