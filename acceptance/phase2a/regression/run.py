"""Run the existing v3.1 gates against a named Git build, using only release-local data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


BASELINE = "51e0e13583d95e9cad1386e1d4c77324977e97ea"
B2 = "a691a4012d16bf813eaaf652a2cabc09881f8f2a"
REPO = Path(__file__).resolve().parents[3]
CATALOG_DIGEST = "a6540498a669ec2c510048de812fef7ff5c971bdf30ee788966224e884c3edc0"


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def git(*args, cwd=REPO):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def bootstrap(args, out, stage):
    assets = stage / "release-assets"
    lock = json.loads((assets / "lock.json").read_text())
    for name in ("generated", "vendor"):
        (stage / name).symlink_to(args.cache / name, target_is_directory=True)
    platform = lock["linux_x64"]
    (assets / platform["file"]).symlink_to(args.cache / "release-assets" / platform["file"])
    binary = stage / "vendor/codex-runtime/package" / platform["binary"]
    pins = {binary: platform["binary_sha256"], assets / platform["file"]: platform["sha256"],
            args.source_db: json.loads((stage / "acceptance/v3/staging/cold-baseline.json").read_bytes())["checkpoint"]["db_sha256"]}
    for item in (lock["source"], lock["experimental_schema"]):
        pins[assets / item["file"]] = item["sha256"]
    for wheel in lock["scientific_review"]["wheels"].values():
        pins[assets / "python" / wheel["file"]] = wheel["sha256"]
    digests = {str(file): sha(file) for file in pins}
    assert all(digests[str(file)] == expected for file, expected in pins.items())
    profile = next(entry for entry in json.loads(args.catalog.read_bytes())["models"] if entry["slug"] == "gpt-6.2-astra")
    model_digest = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert model_digest == CATALOG_DIGEST
    (stage / "var").mkdir(exist_ok=True)
    (stage / "var/cli-home").mkdir()
    (stage / "var/cli-home/.codex").mkdir()
    catalog = stage / "var/catalog.json"
    save(catalog, {"models": [profile]})
    save(out / "dependencies.json", {"git_commit": git("rev-parse", "HEAD", cwd=stage),
         "git_tree": git("rev-parse", "HEAD^{tree}", cwd=stage), "baseline": BASELINE,
         "codex": subprocess.check_output([binary, "--version"], text=True, env={**os.environ, "CODEX_HOME": str(stage / "var/cli-home/.codex")}).strip(),
         "source_commit": lock["source"]["commit"], "files_sha256": digests,
         "catalog_entry_sha256": model_digest, "catalog_slug": profile["slug"],
         "catalog_use": "Input schema fixture for local scripted Responses",
         "python": sys.version.split()[0], "node": subprocess.check_output(["node", "--version"], text=True).strip(),
         "stage": str(stage), "runner_sha256": sha(Path(__file__)),
         "isolation_runner_sha256": sha(Path(__file__).with_name("loopback.py"))})
    python = stage / ".venv/bin/python"
    wheels = assets / "python"
    setup = ([sys.executable, "-m", "venv", stage / ".venv"],
             [python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--no-index",
              "--no-cache-dir", "--require-hashes", "--find-links", wheels, "-r", wheels / "requirements.lock"],
             [python, "-B", "scripts/release_assets.py", "cordis"])
    with (out / "setup.log").open("w") as log:
        for command in setup:
            subprocess.run(command, check=True, cwd=stage, stdout=log, stderr=subprocess.STDOUT)
    return python, binary, catalog


def experiment(args):
    args.cache = args.cache.resolve()
    args.source_db = args.source_db.resolve()
    args.historical_briefs = args.historical_briefs.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    revision = git("rev-parse", "--verify", args.revision + "^{commit}")
    stage = Path(tempfile.mkdtemp(prefix="gx-p2a-reg-")) / "release"
    subprocess.run(["git", "worktree", "add", "--quiet", "--detach", str(stage), revision], cwd=REPO, check=True)
    python, binary, catalog = bootstrap(args, out, stage)
    work = stage / "var"
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "HTTP_PROXY", "http_proxy",
                                            "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy") if key in os.environ}
    env.update(PATH=str(python.parent) + os.pathsep + env["PATH"], PYTHONDONTWRITEBYTECODE="1",
               HOME=str(work / "cli-home"), CODEX_HOME=str(work / "cli-home/.codex"))
    records = {}
    def run(name, *command):
        argv = [str(value) for value in command]
        result = {"argv": argv, "started_utc": utc(), "cwd": str(stage)}
        with (out / (name + ".log")).open("w") as log:
            proc = subprocess.run(argv, cwd=stage, stdout=log, stderr=subprocess.STDOUT, env=env)
        result.update(returncode=proc.returncode, finished_utc=utc())
        save(out / (name + "-invocation.json"), result)
        records[name] = result
        return proc.returncode

    def probe(name, entry, *argv):
        return run(name, python, "-B", "acceptance/" + entry, *argv)

    def historical():
        return probe("at2-preservation", "v31/historical_migration.py", "--install", stage, "--source", args.source_db,
                     "--baseline", stage / "acceptance/v3/staging/cold-baseline.json", "--out", out / "at2-preservation.json")

    def child(drop=False):
        name = "child-lost-start" if drop else "child-scope"
        return probe(name, "v31/b2/role_probe.py", "--binary", binary, "--output", out / (name + ".json"),
                     *(["--drop-cold-start"] if drop else []))

    def broad():
        directory = out / "suite"
        probe("suite", "v21/regression.py", "--commit", revision, "--native", "--out", directory)
        probe("inherited-case-ids", "v3/compare_regression.py", directory)
        comparison = (out / "inherited-case-ids.log").read_text()
        try:
            save(out / "inherited-case-ids.json", json.loads(comparison))
        except ValueError:
            pass
        import re
        row = re.compile(r"^test_\w+ \(([^()]+)\) \.\.\. (.*)$", re.M)
        counts = {}
        for name in ("python-full", "p0-focused"):
            reference = dict(row.findall(git("show", f"{BASELINE}:acceptance/v31/regression-final/{name}.log")))
            now = dict(row.findall((directory / (name + ".log")).read_text()))
            missing = sorted(reference.keys() - now.keys())
            changed = sorted(key for key in reference.keys() & now.keys()
                             if now[key].startswith("skipped ") and reference[key] != now[key])
            counts[name] = {"baseline": len(reference), "measured": len(now), "missing": missing, "changed_skips": changed}
        save(out / "v31-test-identity.json", counts)
        if any(x["missing"] or x["changed_skips"] for x in counts.values()):
            raise RuntimeError("Missing/changed baseline v3.1 test identities")

    def at2_cold():
        if not probe("at2-checkpoint", "v3/staging/cold_fixture.py", "--work", work / "at2",
                     "--output", out / "at2-checkpoint.json"):
            probe("at2-cold", "v3/staging/replay_checkpoint.py", "--baseline", out / "at2-checkpoint.json",
                  "--candidate", revision, "--audit", work / "at2-replay", "--output", out / "at2-cold.json")

    def surface():
        probe("terminal-code-mode", "v31/terminal_code_mode_probe.py", "--catalog", catalog,
              "--output", out / "terminal-code-mode.json")
        probe("terminal-stream", "v31/terminal_stream_probe.py", "--catalog", catalog, "--output", out / "terminal-stream.json")
        probe("terminal-crash", "v31/terminal_crash_concurrency.py", "--output", out / "terminal-crash.json")
        probe("terminal-late", "v31/terminal_late_receipts.py", "--output", out / "terminal-late.json")
        probe("terminal-public-crash", "v31/terminal_official_crash.py", "--public-only", "--expect", "corrected",
              "--commit", revision, "--work", work / "terminal-public-crash", "--out", out / "terminal-public-crash.json")

    def public_grid():
        for mode in ("deterministic", "review", "repair"):
            probe("public-" + mode, "v31/public_grid.py", "--commit", revision, "--mode", mode, "--work",
                  work / ("public-" + mode), "--out", out / ("public-" + mode + ".json"))
        if not probe("schema-transport", "v31/schema_transport.py"):
            shutil.copyfile(stage / "acceptance/v31/schema-transport.json", out / "schema-transport.json")

    def extra_child_observation():
        return run("extra-child-observation", python, "-B", Path(__file__).with_name("standalone_child.py"),
                   "--stage", stage, "--out", out / "extra-child")

    def protocol():
        return probe("terminal-protocol", "v31/terminal_protocol_probe.py", "--repetitions", "100",
                     "--output", out / "terminal-protocol.json")

    def briefs():
        probe("brief-replay", "v31/brief_replay.py", "--states", args.historical_briefs, "--output", out / "brief-replay.json")
        return probe("brief-wire", "v31/brief_wire.py", "--b2", B2, "--b3", revision,
                     "--work", work / "brief-wire", "--output", out / "brief-wire.json")

    if args.phase == "preflight":
        if revision != BASELINE:
            raise ValueError("baseline preflight uses the original phase baseline")
        jobs = [historical, lambda: child(True)]
    else:
        jobs = [broad, protocol, at2_cold, surface, public_grid, briefs, child, lambda: child(True), historical, extra_child_observation]
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(job) for job in jobs]
        for future in futures:
            try:
                future.result()
            except Exception as error:
                errors.append(repr(error))
    dirty = git("status", "--porcelain", "--", "gx-h", "bin", "codex_adapter", "examples", "scripts", "release-assets", cwd=stage)
    passed = not errors and not dirty and all(row["returncode"] == 0 for row in records.values())
    save(out / "execution.json", {"revision": revision, "phase": args.phase, "operations": records,
                           "errors": errors, "tracked_product_changes": dirty, "passed": passed})
    print(json.dumps({"passed": passed, "directory": str(out), "errors": errors,
                      "failed": [key for key, value in records.items() if value["returncode"]]}))
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("preflight", "full"), required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--cache", type=Path, required=True, help="Read-only runtime release")
    parser.add_argument("--source-db", type=Path, required=True, help="Original historical @2 snapshot")
    parser.add_argument("--historical-briefs", type=Path, default=REPO.parent / "scratchpad/v3-real-performance/p1-2476f2b-xhigh",
                        help="Historical @2 workspaces for read-only preload replay")
    parser.add_argument("--catalog", type=Path, required=True, help="Local Codex model schema cache")
    parser.add_argument("--out", type=Path, required=True, help="New evidence directory")
    sys.exit(experiment(parser.parse_args()))
