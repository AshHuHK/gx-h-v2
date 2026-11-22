#!/usr/bin/env python3
"""Exercise a freshly unpacked attachment using its files and a new Python environment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote

from release_assets import ROOT, LOCK, checked, verify_schema


def verify_candidate():
    manifest = json.loads((ROOT / "RELEASE_CONTENTS.json").read_text())
    files = manifest["files"]
    sha = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if sha != manifest["candidate_tree_sha256"]:
        raise ValueError("Candidate manifest checksum differs")
    executables = []
    for name, entry in files.items():
        path = ROOT / name
        if "link" in entry:
            if not path.is_symlink() or os.readlink(path) != entry["link"] or not path.exists():
                raise ValueError(f"Broken release symlink: {name}")
            continue
        checked(path, entry["sha256"])
        mode = int(entry["mode"], 8)
        if path.stat().st_mode & 0o777 != mode:
            raise ValueError(f"Release file mode differs: {name}")
        if mode & 0o111:
            executables.append(name)
    return {"files": len(files), "executable_files": executables, "git_base": manifest["git_base"],
            "candidate_tree_sha256": sha}


def operational_links():
    paths = ["README.md", "RELEASE.md", "DEPLOYMENT.md", "gx-h/README.md", "audit/CODEX_SOURCE.md"]
    if (ROOT / "MODEL_PROFILES.md").is_file():
        paths.append("MODEL_PROFILES.md")
    count = 0
    for name in paths:
        page = ROOT / name
        for link in re.findall(r"\]\(([^)\s]+)\)", page.read_text()):
            target = unquote(link.split("#", 1)[0])
            if target and "://" not in target:
                destination = (page.parent / target).resolve()
                if not destination.is_relative_to(ROOT) or not destination.exists():
                    raise FileNotFoundError(f"{name} links to missing {target}")
                count += 1
    return {"pages": paths, "resolved_local_links": count}


def smoke(out, archive):
    out.mkdir(parents=True, exist_ok=False)
    summary = {"result": "FAIL", "commands": [], "candidate": verify_candidate()}
    with tempfile.TemporaryDirectory(prefix="gx-release-test-") as tmp:
        scratch = Path(tmp)
        # Remove caller Python/GX/npm configuration and keep the configured proxy route.
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH") or
               k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        env.update(PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   GIT_CEILING_DIRECTORIES=str(ROOT.parent), npm_config_cache=str(scratch / "npm-cache"))
        def run(label, argv, *, cwd=ROOT, extra=None):
            proc = subprocess.run([str(x) for x in argv], cwd=cwd, env={**env, **(extra or {})},
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=360)
            (out / (label + ".log")).write_text(proc.stdout)
            summary["commands"].append({"log": label + ".log", "exit": proc.returncode})
            if proc.returncode:
                raise RuntimeError(f"{label} exited {proc.returncode}; see {out / (label + '.log')}")
            return proc.stdout
        try:
            if subprocess.run(["git", "rev-parse", "--git-dir"], env=env, cwd=ROOT,
                              capture_output=True).returncode == 0:
                raise ValueError("Run this check from a fresh tar extraction outside the release checkout")
            summary["package_has_git_repository"] = False
            official = ROOT / "release-assets" / LOCK["linux_x64"]["file"]
            rebuilt = json.loads(run("rebuild", [sys.executable, "-B", "scripts/build_release.py", "--output",
                                     scratch / "rebuilt.tar", "--codex-archive", official]))
            summary["rebuilt_archive_sha256"] = rebuilt["sha256"]
            if archive:
                checked(archive, rebuilt["sha256"])
                summary["bit_identical_to_supplied_attachment"] = True
            assets = run("offline-assets", [sys.executable, "-B", "scripts/release_assets.py", "all",
                                           "--codex-archive", official])
            summary["assets"] = json.loads(assets)
            summary["operational_doc_links"] = operational_links()
            binary = summary["assets"]["codex"]["official_codex"]
            run("cli-help", ["bin/gx-p0", "--help"])
            version = run("official-version", [binary, "--version"]).strip()
            if version.splitlines()[-1] != "codex-cli " + LOCK["codex_version"]:
                raise ValueError(f"Unexpected official release version: {version}")
            schema = scratch / "regenerated-schema"
            for command in ("generate-ts", "generate-json-schema"):
                run(command, [binary, "app-server", command, "--experimental", "--out", schema])
            summary["regenerated_experimental_schema_files"] = verify_schema(schema)
            run("create-venv", [sys.executable, "-m", "venv", scratch / "venv"])
            python = scratch / "venv/bin/python"
            wheels = ROOT / "release-assets/python"
            run("offline-python", [python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
                                   "--no-index", "--no-cache-dir", "--require-hashes", "--find-links", wheels,
                                   "-r", wheels / "requirements.lock"])
            env.update(PATH=str(python.parent) + os.pathsep + env["PATH"],
                       PYTHONPATH=str(ROOT / "gx-h"), GX_P0_CODEX=binary)
            run("migration", [python, "-B", "-m", "unittest", "-v", "tests.test_v2_fencing.BaselineMigration",
                               "tests.test_v2_rollback"], cwd=ROOT / "gx-h",
                extra={"GX_MIGRATION_TEST_REPORT": str(out / "migration.json")})
            run("cordis", [python, "-B", "-m", "unittest", "-v", "tests.test_plugin_evaluator"], cwd=ROOT / "gx-h")
            run("cordis-node", ["node", "--experimental-strip-types", "--test", "runtime/plugin-host/host.test.ts"],
                cwd=ROOT / "gx-h")
            dsh = run("optional-dsh", [python, "-B", "-m", "unittest", "-v", "tests.test_dsh_activity",
                                       "tests.test_dsh_runtime"], cwd=ROOT / "gx-h")
            summary["dsh_skipped"] = len(re.findall(r"\.\.\. skipped ", dsh))
            run("geometry", [python, "-B", "acceptance/acc1_coordinate_method.py",
                             "--output", out / "geometry.json"])
            run("coordinate-project", [python, "-B", "acceptance/acc1_research.py", "--loopback", "--extended-budget",
                                       "--state", scratch / "coordinate-state", "--output", out / "coordinate-project",
                                       "--timeout", "180"])
            coordinate = json.loads((out / "coordinate-project/summary.json").read_text())
            limits = json.loads((out / "coordinate-project/initial-budget.json").read_text())["actual_root_ledger_limits"]
            decisions = coordinate["decisions"]
            if (coordinate["status"] != "PASS" or coordinate["job_count"] != limits["job_submissions"]
                    or limits["job_submissions"] != 4 or len(decisions) != 2 or not decisions[-1]["stop_reason"]):
                raise ValueError("The project must publish its final scientific decision after spending the fourth Job")
            summary["coordinate_project"] = {"root_job_limit": limits["job_submissions"], "jobs": coordinate["job_count"],
                                             "decisions": len(decisions), "report_ref": coordinate["report_ref"]}
            run("loopback-doctor", [python, "-B", "acceptance/oauth_loopback.py"])
            run("loopback-protocol", [python, "-B", "acceptance/native_probe.py", "--binary", binary, "--out",
                                      out / "protocol", "session"])
            run("loopback-cli", [python, "-B", "acceptance/e2e_grid.py", "--codex", binary, "--output",
                                 out / "grid", "--timeout", "180"])
            run("active-project-upgrade", [python, "-B", "acceptance/acc1_upgrade.py", "--out", out / "upgrade.json"])
            summary["result"] = "PASS"
        finally:
            (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory outside the package")
    parser.add_argument("--archive", type=Path, help="Compare a fresh rebuild with these original attachment bytes")
    args = parser.parse_args()
    result = smoke(args.output.resolve(), args.archive)
    print(result["result"], args.output.resolve() / "summary.json")
