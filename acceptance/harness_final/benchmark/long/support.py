"""Stage a pinned GX release and the same two published hydrometry instruments in independent workspaces."""

import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
BASE = "4b6aa93ad5bf638d06bed7004c26c27d030a59ea"
ORIGINAL = REPO / "acceptance/harness_refactor/blind"
sys.path.insert(0, str(ORIGINAL))
from common import BINARY, EFFORT, MODEL, environment, read, save, sha

P = HERE / "packet"
METHODS = {
    "concurrent": ("discharge-concurrent@1", "paired.json"),
    "marginal": ("discharge-marginal@1", "marginal.json"),
}


def registered():
    spec = read(HERE / "preregistered.json")
    for n, h in spec["registered_science_files"].items():
        if sha(HERE / n) != h:
            raise ValueError("Changed frozen scientific material " + n)
    return spec


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args])


def deploy(folder, rev):
    release = folder / "release"
    if (release / "pin.json").exists():
        assert read(release / "pin.json")["revision"] == rev
        return release
    release.mkdir(parents=True)
    with tempfile.TemporaryFile() as archive:
        subprocess.run(
            [
                "git",
                "-C",
                str(REPO),
                "archive",
                rev,
                "bin",
                "gx-h",
                "examples/plugins/hydro-concurrent",
                "examples/plugins/hydro-marginal",
            ],
            stdout=archive,
            check=True,
        )
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            tar.extractall(release, filter="data")
    (release / ".venv").symlink_to(REPO / ".venv", target_is_directory=True)
    (release / "vendor").symlink_to(REPO / "vendor", target_is_directory=True)
    (release / "release-assets").symlink_to(REPO / "release-assets", target_is_directory=True)
    policy = release / "gx-h/config/science_site_access.json"
    conf = read(policy)
    hashes = {}
    for label, (method, _) in METHODS.items():
        pkg = release / "examples/plugins" / ("hydro-" + label)
        plugin = "org.gx.hydro-" + label + "@1.0.0"

        def cli(*args):
            return json.loads(
                subprocess.check_output(
                    [sys.executable, "-B", str(release / "bin/gx-p0"), "plugin", *map(str, args)],
                    cwd=release,
                )
            )

        h = cli("digest", pkg)["sha256"]
        cli("install", pkg, "--sha256", h)
        checked = cli("verify", plugin)
        assert not checked["failures"] and checked["plugins"][0]["sha256"] == h
        conf["cpu_analysis"][plugin] = [method]
        hashes[plugin] = h
    policy.write_text(json.dumps(conf, indent=2) + "\n")
    save(
        release / "pin.json",
        {
            "revision": rev,
            "core_tree": git("rev-parse", rev + ":gx-h/gx_core").decode().strip(),
            "plugins": hashes,
            "site_sha256": sha(policy),
        },
    )
    return release


def goal_config():
    return {
        "project": "USGS-bulletin",
        "budget_profile": "oauth_research",
        "research_effort": "deep",
        "evidence_policy": "gx.evidence/6.2.4",
        "capability_collections": [],
        "plugins": ["org.gx.hydro-" + s + "@1.0.0" for s in METHODS],
        "capabilities": [m for m, _ in METHODS.values()],
        "initial_job_limit": 4,
        "run_budget": {"job_submissions": 4, "gpu_jobs": 0},
        "input_files": [
            {"role": role, "path": str(P / file), "sha256": sha(P / file)}
            for role, file in [("record", "record.json"), ("upstream_original", "daily.json")]
        ],
    }
