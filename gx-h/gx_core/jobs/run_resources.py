"""Capture the resources a Run starts under before acquiring its SQLite writer."""

from functools import lru_cache
import os
from pathlib import Path

from .. import config
from ..capabilities import probe
from ..domain import clock
from . import capacity


def observe(*, driver_reading=True):
    fixture = config.deterministic_test_resources()
    cpu = capacity.cpu_state()
    policy = config.limits()
    room = capacity.cpu_room(None, cpu=cpu, limits=policy, jobs={"cpu_threads_held": 0, "jobs": []})
    if fixture or driver_reading:
        reading = probe.read_gpu_state()
        gpu = {key: reading[key] for key in ("source", "devices", "problems")}
    else:
        # Direct low-level row creators get the kernel inventory sampled on store
        # open. A CLI that merely reads a store need never start a GPU utility.
        gpu = {"source": "Linux device nodes", "devices": [
            {"index": int(node.name[6:])} for node in sorted(Path("/dev").glob("nvidia[0-9]*"))
            if node.name[6:].isdigit() and node.is_char_device()], "problems": []}
    gpu["cuda_visible_devices"] = "" if fixture else os.environ.get("CUDA_VISIBLE_DEVICES")
    return {"mode": "deterministic" if fixture else "real", "observed_at": clock.now_iso(),
            "source": "controlled acceptance launcher" if fixture else "host",
            "cpu": {"source": cpu["source"], "logical_cpus": cpu["logical_cpus"],
                    "cgroup_quota_cpus": cpu["cgroup_quota_cpus"], "threads_allowed": room["allowed"],
                    "policy": policy["cpu"]},
            "memory": capacity.memory_state(), "gpu": gpu}


@lru_cache(maxsize=4)
def _process_observation(pid, fixture, gpu_visibility):
    return observe(driver_reading=False)


def process_observation():
    return _process_observation(os.getpid(), config.deterministic_test_resources(),
                                os.environ.get("CUDA_VISIBLE_DEVICES"))
