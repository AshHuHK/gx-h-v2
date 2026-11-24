"""Run both supported child fixture paths inside one already isolated loopback namespace."""
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

stage, out = (Path(value).resolve() for value in sys.argv[1:])
python = stage / ".venv/bin/python"
binary = stage / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
namespace = os.readlink("/proc/self/ns/net")
summary = {"namespace": namespace, "cases": {}}
with tempfile.TemporaryDirectory(prefix="gx-child-long-path-", dir="/tmp") as tmp:
    nested = Path(tmp) / ("acceptance-worker-" * 6) / ("inner-regression-" * 5)
    nested.mkdir(parents=True)
    env = {**os.environ, "TMPDIR": str(nested)}
    os.environ.update(TMPDIR=str(nested))
    commands = {
        "legacy": ["acceptance/phase2a/regression/standalone_child.py", "--stage", str(stage)],
        "direct": ["acceptance/closure/child_double_loss.py", "--binary", str(binary)],
    }
    for name, entry in commands.items():
        argv = [str(python), "-B", *entry, "--out", str(out / name)]
        observation = {}
        with (out / (name + ".log")).open("w") as log, subprocess.Popen(argv, cwd=stage, env=env, stdout=log,
                                                                          stderr=subprocess.STDOUT) as proc:
            deadline = time.monotonic() + 90
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    proc.kill()
                    raise TimeoutError(name)
                if not observation:
                    for case_file in nested.glob("gx-child-loss-*/case.json"):
                        address = Path(json.loads(case_file.read_text())["relay_socket"])
                        try:
                            mode = address.stat().st_mode
                        except FileNotFoundError:
                            continue
                        assert stat.S_ISSOCK(mode)
                        observation = {"work": str(case_file.parent), "socket": str(address),
                                       "socket_bytes": len(os.fsencode(address)),
                                       "previous_socket_bytes": len(os.fsencode(case_file.parent / "gx.sock")),
                                       "namespace": os.readlink(f"/proc/{proc.pid}/ns/net")}
                        break
                time.sleep(.02)
            assert proc.returncode == 0, (out / (name+".log")).read_text()
        assert observation["previous_socket_bytes"] >= 108 > observation["socket_bytes"]
        assert observation["namespace"] == namespace and not Path(observation["socket"]).exists()
        target = out / name
        if name == "legacy":
            wrapper = json.loads((target / "observation.json").read_text())
            assert wrapper["returncode"] == 0 and wrapper["observed_status"] == "PASS"
            target = target / "evidence"
        receipt = json.loads((target / "receipt.json").read_text())
        assert receipt["status"] == "PASS" and receipt["closure"] == "CLOSED"
        assert receipt["extra_model_requests"] == receipt["duplicate_jobs"] == 0
        summary["cases"][name] = {**observation, "argv": argv, "run": receipt["run_id"],
                                  "single_settlement": receipt["settled"]["settlements"][0]["event_id"]}

    # Inject a startup failure immediately after spawning the official host and
    # before a reader can start. Check cleanup using the actual subprocess handle.
    sys.path.insert(0, str(stage / "acceptance/closure"))
    from child_wire import Wire
    launched = []
    original = subprocess.Popen
    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        launched.append(process)
        return process
    setup = Path(tmp) / "startup-failure"
    setup.mkdir()
    fault_env = {**env, "HOME": str(setup), "CODEX_HOME": str(setup / "codex")}
    with tempfile.TemporaryDirectory(prefix="gx-wire-fault-", dir="/tmp") as addresses:
        with patch("child_wire.subprocess.Popen", side_effect=capture), patch("child_wire.threading.Thread.start",
                side_effect=RuntimeError("injected reader-start failure")):
            try:
                Wire(setup, binary, fault_env, setup, Path(addresses) / "gx.sock")
            except RuntimeError as error:
                assert str(error) == "injected reader-start failure"
            else:
                raise AssertionError("fault injection did not fire")
    process, = launched
    assert process.poll() is not None and process.stdin.closed and process.stdout.closed
    summary["startup_fault"] = {"pid": process.pid, "host_exit_code": process.returncode, "stdio_closed": True}
(out / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({"checks": list(summary["cases"]), "startup_fault":summary["startup_fault"]}))
