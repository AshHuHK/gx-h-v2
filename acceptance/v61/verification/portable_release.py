"""Build an exact local commit and exercise the Linux archive without the GX checkout or network."""
import argparse
from contextlib import closing
import ctypes
import datetime
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def isolate_reads(work):
    # Landlock ABI v1 is available on this host although tmpfs mounting is restricted.
    # Existing product and operator directories must be unreadable to this process and every descendant.
    libc = ctypes.CDLL(None, use_errno=True)
    rights = (1 << 0) | (1 << 2) | (1 << 3)  # execute, read file, read directory
    rule = ctypes.c_uint64(rights)
    def checked(rc):
        if rc < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return rc
    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    rules = checked(libc.syscall(444, ctypes.byref(rule), ctypes.sizeof(rule), 0))
    try:
        for allowed in ("/", "/usr", "/opt/conda", "/etc", "/sys", "/dev", "/proc", work):
            fd = os.open(allowed, os.O_PATH | os.O_CLOEXEC)
            try:
                entry = Beneath((1 << 3) if allowed == "/" else rights, fd)
                checked(libc.syscall(445, rules, 1, ctypes.byref(entry), 0))
            finally:
                os.close(fd)
        checked(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        checked(libc.syscall(446, rules, 0))
    finally:
        os.close(rules)


def verify_clean(work, forbidden, commit, expected_hash):
    os.chdir(work)
    root = work / "install"
    logs = work / "records"
    archive = work / "release.tar"
    raw = {}
    def run(label, argv, *, cwd=None, rc=0):
        proc = subprocess.run([str(i) for i in argv], cwd=cwd or root, capture_output=True,
                              env=os.environ.copy(), timeout=300, text=True)
        (logs / (label + ".log")).write_text(proc.stdout + proc.stderr)
        raw[label] = {"exit": proc.returncode, "expected": rc, "argv": list(map(str, argv))}
        save(logs / "commands.json", raw)
        if proc.returncode != rc:
            raise RuntimeError(f"{label} exited {proc.returncode}: {(proc.stdout+proc.stderr)[-1200:]}")
        return proc.stdout.strip()

    assert digest(archive) == expected_hash
    root.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(root, filter="data")
    assert not (root / ".git").exists()
    route = Path("/proc/net/route").read_text().splitlines()
    assert not any(line.split()[1:2] == ["00000000"] for line in route[1:]), route
    assert not list(Path(os.environ["CODEX_HOME"]).iterdir())
    interpreter = Path(sys.executable)
    def pycheck(label, body):
        return json.loads(run(label, [interpreter, "-I", "-B", "-c", "import json,os,sys; "
                      "sys.path.insert(0,os.path.abspath('scripts')); import smoke_release as s; "
                      f"print(json.dumps({body}))"]))
    files = pycheck("verify-manifest", "s.verify_candidate()")
    assert files["git_base"] == commit
    rebuilt = json.loads(run("rebuild", [interpreter, "-B", "scripts/build_release.py",
                                         "--output", work / "rebuilt.tar"]))
    assert rebuilt["sha256"] == expected_hash
    assets = json.loads(run("offline-codex-cordis", [interpreter, "-B", "scripts/release_assets.py", "all"]))
    links = pycheck("verify-operator-links", "s.operational_links()")
    native = json.loads(run("gx-native-contract", [interpreter, "-I", "-B", "-c",
        "import json,os,sys; sys.path.insert(0,os.path.abspath('gx-h')); from gx_core import contracts; "
        "print(json.dumps(contracts.contract_digest()))"]))
    assert native == json.loads((root / "gx-h/contracts/CONTRACT_SHA").read_bytes())
    expected_contract = json.loads((root / "acceptance/v61/contracts/p1-isolated.json").read_bytes())["contract"]
    assert native["__combined__"] == expected_contract
    code = Path(assets["codex"]["official_codex"])
    version = run("official-version", [code, "--version"]).splitlines()[-1]
    assert version == "codex-cli 0.169.0"
    generated = work / "schema"
    for action in ("generate-ts", "generate-json-schema"):
        run(action, [code, "app-server", action, "--experimental", "--out", generated])
    count = pycheck("regenerated-schema", f"s.verify_schema(s.Path({str(generated)!r}))")
    assert count == assets["codex"]["experimental_schema_files"]
    run("venv", [interpreter, "-m", "venv", work / "venv"])
    python = work / "venv/bin/python"
    wheels = root / "release-assets/python"
    run("python-wheels", [python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
                           "--no-index", "--no-cache-dir", "--require-hashes", "--find-links", wheels,
                           "-r", wheels / "requirements.lock"])
    os.environ["PATH"] = str(python.parent) + ":" + os.environ["PATH"]
    os.environ["GX_P0_CODEX"] = str(code)
    runtimes = json.loads(run("python-versions", [python, "-I", "-c", "import Bio,numpy,json,platform; "
              "print(json.dumps(dict(python=platform.python_version(),biopython=Bio.__version__,numpy=numpy.__version__)))"]))
    runtimes.update(node=run("node-version", ["node", "--version"]), npm=run("npm-version", ["npm", "--version"]))
    assert runtimes == {"python": "3.11.9", "biopython": "1.88", "numpy": "1.26.4",
                        "node": "v22.17.0", "npm": "10.9.2"}, runtimes
    cli = root / "bin/gx-p0"
    plugin_catalog = json.loads((root / "gx-h/config/workflow_packages.json").read_bytes())
    installed = {row["path"]: row["sha256"] for row in plugin_catalog["plugins"]}
    sensor = root / "examples/v61-sensor-calibration"
    sensor_pin = json.loads((sensor / "package-pin.json").read_bytes())
    source_plugin = root / "examples/plugins/sensor_calibration"
    assert sensor_pin["id"] + "/" + sensor_pin["version"] not in installed
    protein_pins = {"org.gx.protein/0.1.0-v6.1":"ac5b5d5cf96e942bfc57dc8492aa56b77ad9ecee0716735a3e6338463630fce9",
                    "org.gx.protein/0.2.0":"f0a8dac2f226aea8a69e19c39e9070f0ac2c16c76507b26f9bb92848369fa5bd"}
    assert all(installed.get(path) == sha for path, sha in protein_pins.items())
    added = json.loads(run("sensor-plugin-install", [cli, "plugin", "install", source_plugin, "--sha256", sensor_pin["sha256"]]))
    assert {k: added[k] for k in sensor_pin} == sensor_pin
    refs = [path.replace("/", "@") for path in protein_pins] + [f"{sensor_pin['id']}@{sensor_pin['version']}"]
    receipt = json.loads(run("plugin-verify", [cli, "plugin", "verify", *refs]))
    verified = {f"{p['id']}/{p['version']}":p["sha256"] for p in receipt["plugins"]}
    expected_pins = {**protein_pins, added["path"]:sensor_pin["sha256"]}
    assert not receipt["failures"] and verified == expected_pins
    assert "methods/deposited_geometry.json" in next(p for p in receipt["plugins"] if p["version"] == "0.2.0")["contributions"]["capabilities"]
    state = work / "state"
    def call(label, action, args, rc=0):
        return json.loads(run(label, [cli, action, "--state", state, *args], cwd=work, rc=rc))
    inputs = sensor / "measurements.csv"
    opened = call("goal-start", "start", ["--goal", (sensor / "goal.md").read_text().strip(),
                                          "--run-key", "release-verify", "--goal-config", sensor / "grant.json"])
    rid = opened["run_id"]
    by_run = ["--run-id", rid]
    initial = call("cold-status", "status", by_run)
    paused = call("stop", "stop", by_run)
    paused_cold = call("cold-paused-status", "status", by_run)
    resumed = call("resume-offline", "resume", by_run + ["--timeout", "0.5"], rc=75)
    reopened = call("cold-reopened-status", "status", by_run)
    timeline = []
    for body in (opened, initial, paused_cold, resumed, reopened):
        assert body["run_id"] == rid and body["mode"] == "goal" and body["graph_head"] is None, body
        assert body["goal_version"] == 1 and body["tasks"] == initial["tasks"], body
        timeline.append(body["status"])
    assert paused_cold["status"] == "PAUSED" and reopened["status"] == "ACTIVE", timeline
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        counts = {t: db.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in (
              "runs", "tasks", "checkpoints", "jobs", "p0_roots", "p0_meter_native_turns")}
        assert counts == dict(runs=1, tasks=1, checkpoints=0, jobs=0, p0_roots=0, p0_meter_native_turns=0), counts
        frozen_inputs = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
        catalog = frozen_inputs["inputs"]["capability_catalog"]
        assert len(catalog) == 1 and catalog[0]["capability_version"] == "sensor-calibration@1" and catalog[0]["plugin"] == sensor_pin
        assert [f["sha256"] for f in frozen_inputs["files"]] == [digest(inputs)]
        with closing(sqlite3.connect(logs / "cold-goal.db")) as frozen:
            db.backup(frozen)
    run("coordinate-method", [python, "-B", "acceptance/acc1_coordinate_method.py", "--output", logs / "geometry.json"])
    geometry = json.loads((logs / "geometry.json").read_bytes())
    assert geometry["external_job_count_after_cold_replay"] == 4 and len(geometry["independent_checks"]) == 3
    original_methods = {"jobs_after_replay":4,
                        "independent_protocols":len(geometry["independent_checks"]),
                        "measurement_receipt_sha256":digest(logs / "geometry.json")}
    # ABI v1 rejects cross-directory rename(2) even between two allowed paths. The
    # installer, SQLite and JobService legitimately perform this operation. After
    # installation, enforce the read allowlist for a fresh operator process.
    isolate_reads(work)
    try:
        with open(forbidden, "rb") as stream:
            stream.read(1)
    except PermissionError as exc:
        assert exc.errno == errno.EACCES
    else:
        raise AssertionError("Original product remained readable")
    fenced = call("cold-status-workspace-denied", "status", by_run)
    assert fenced["run_id"] == rid and fenced["tasks"] == reopened["tasks"]
    after = json.loads(run("plugin-verify-workspace-denied", [cli, "plugin", "verify", *refs]))
    assert after == receipt
    # Run the new portable coordinate implementation after denying reads of the author's checkout.
    # Independent recomputation uses the separately shipped Bio.PDB and SVD oracle.
    study = root / "acceptance/v61/adaptive"
    material = json.loads((study / "study-config.json").read_bytes())["input_files"]
    direct = []
    for record in material:
        path = (study / record["path"]).resolve(strict=True)
        assert path.is_relative_to(root) and digest(path) == record["sha256"]
        label = "coordinates-" + record["role"]
        folder = logs / label
        run(label, [python, "-I", "-B", root / "gx-h/plugins/installed/org.gx.protein/0.2.0/methods/deposited_geometry.py",
                    "--structure", path, "--fitter", root / "gx-h/adapters/scientific/tl1a_geometry/worker.py",
                    "--mask", "common_backbone", "--out", folder])
        values = json.loads((folder / "geometry.json").read_bytes())
        assert values["input_sha256"] == record["sha256"] and values["selected_residues"] > 0
        cross = json.loads(run(label + "-independent", [python, "-I", "-B", "-c",
            "import json,sys,pathlib; sys.path.insert(0,sys.argv[1]); from study_score import geometry_oracle; "
            "print(json.dumps(geometry_oracle(pathlib.Path(sys.argv[2]),[json.loads(pathlib.Path(sys.argv[3]).read_bytes())])))",
            study, path, folder / "geometry.json"]))
        assert len(cross[0]["fits"]) == 3 and all("rmsd_difference" in f for f in cross[0]["fits"])
        direct.append({"source_sha256":record["sha256"], "output_sha256":digest(folder / "geometry.json"), "reference":cross[0]})
    assert {item["reference"]["entry"] for item in direct} == {"2RE9", "2O0O"}
    source_files = len(files.pop("executable_files"))
    save(logs / "cold-result.json", {"git_commit": commit, "archive_sha256": expected_hash, "manifest": files,
         "executable_files": source_files, "bit_identical_rebuild": True, "fresh_home": True,
         "original_checkout_read": "EACCES", "filesystem_gate": "Independent status, plugin reads, both new coordinate calculations and their separate SVD recomputations ran behind Landlock. The fresh install and compound Job execution used the isolated network namespace, scratch workspace and pinned inputs.",
         "network_namespace": {"interfaces": socket.if_nameindex(), "routes": route}, "operator_links": links,
         "platform": {"system":platform.system(),"architecture":platform.machine(),"kernel":platform.release()},
         "versions": runtimes, "assets": assets, "plugin_pins": verified, "fresh_addon_install": added,
         "released_v6_coordinate_jobs": original_methods, "gx_contract_sha256": native["__combined__"], "gx_ddl_sha256": native["db_schema.sql"], "direct_cpu": direct, "goal_frozen_grant": catalog[0]["plugin"], "regenerated_schema_files": count, "official_cli": version,
         "offline_goal": {"run_id": rid, "input_sha256": digest(inputs), "counts": counts, "run_states": timeline,
                          "resume_exit": 75, "goal_version": 1, "graph_head": None, "sqlite_quick_check": "ok"}})


def outer(commit, product, node, official, interpreter, out, rehearse, target=None):
    parent = product.parent
    if not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError("Supply the final full git commit")
    if rehearse and out.name != "release-rehearsal.json":
        raise ValueError("Rehearsals must write release-rehearsal.json")
    archive = ((out.parent / "release-rehearsal.tar") if rehearse else
               (target or product / "dist/gx-h-v6.1-linux-x86_64.tar"))
    trial = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = archive.resolve()
    raw_dir = out.parent / out.stem / commit / trial
    raw_dir.mkdir(parents=True, exist_ok=True)
    def run(argv, cwd=None):
        return subprocess.run(list(map(str, argv)), cwd=cwd, capture_output=True, text=True, check=True).stdout
    assert run(["git", "rev-parse", "--verify", commit + "^{commit}"], product).strip() == commit
    receipt = {"status":"FAIL", "commit":commit, "started_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "verifier_sha256":digest(Path(__file__)), "cold_records":str(raw_dir.relative_to(out.parent)),
               "rerun_argv":[str(interpreter), "-B", str(Path(__file__).resolve()), "--commit", commit,
                             "--product", str(product), "--official", str(official), "--node", str(node),
                             "--out", str(out)] + (["--rehearse"] if rehearse else []) + (["--archive", str(target)] if target else [])}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-v61-release-") as tmp:
            temp = Path(tmp)
            source = temp / "source"
            run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(product), source])
            run(["git", "checkout", "--quiet", "--detach", commit], source)
            assert run(["git", "status", "--short"], source) == ""
            package = json.loads(run([interpreter, "-B", "scripts/build_release.py", "--output", archive,
                                      "--codex-archive", official.resolve(strict=True)], product if rehearse else source))
            assert package["git_base"] == commit
            receipt["includes_uncommitted_work"] = rehearse
            receipt["build"] = package
            work = temp / "cold"
            work.mkdir()
            for sub in ("records", "home", "codex-home", "tmp"):
                (work / sub).mkdir()
            shutil.copy2(archive, work / "release.tar")
            shutil.copytree(node, work / "node", symlinks=True)
            child = work / "verifier.py"
            shutil.copyfile(__file__, child)
            env = {"PATH": f"{work}/node/bin:/opt/conda/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                   "HOME": str(work / "home"), "CODEX_HOME": str(work / "codex-home"), "TMPDIR": str(work / "tmp"),
                   "PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                   "GX_CODEX_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":"/dev/null",
                   "http_proxy":"http://127.0.0.1:7890", "https_proxy":"http://127.0.0.1:7890",
                   "all_proxy":"socks5h://127.0.0.1:7891","no_proxy":"localhost,127.0.0.1,::1"}
            env.update({k.upper(): env[k] for k in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")})
            isolated = subprocess.run(["unshare", "--user", "--map-root-user", "--net", interpreter, "-I", "-B",
                    child, "--inside", work, product / "README.md", commit, package["sha256"]],
                    cwd=work, env=env, capture_output=True, text=True)
            (work / "records/verifier.log").write_text(isolated.stdout + isolated.stderr)
            shutil.copytree(work / "records", raw_dir, dirs_exist_ok=True)
            if isolated.returncode:
                raise RuntimeError(isolated.stderr[-1600:])
            receipt["cold"] = json.loads((raw_dir / "cold-result.json").read_text())
            receipt["status"] = "PASS"
    except Exception as ex:
        receipt["error"] = str(ex)
        raise
    finally:
        receipt["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        receipt["records"] = {str(f.relative_to(out.parent)): digest(f) for f in sorted(raw_dir.rglob("*")) if f.is_file()}
        save(raw_dir / "receipt.json", receipt)
        save(out, receipt)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--inside"]:
        work, forbidden, sha, value = sys.argv[2:]
        verify_clean(Path(work), forbidden, sha, value)
    else:
        root = Path(__file__).resolve().parent.parent
        p = argparse.ArgumentParser(description=__doc__)
        p.add_argument("--commit", required=True)
        p.add_argument("--product", type=Path, default=root / "product-v6.1")
        p.add_argument("--node", type=Path, default=root.parent.parent / "node-v22.17.0-linux-x64")
        p.add_argument("--official", type=Path, default=root / "reuse-sources/codex/codex-0.169.0-linux-x64.tgz")
        p.add_argument("--python", type=Path, default=Path("/opt/conda/bin/python3.11"))
        p.add_argument("--out", type=Path, default=root / "v61-validation/release.json")
        p.add_argument("--archive", type=Path, help="Destination archive for an intermediate verified commit")
        p.add_argument("--rehearse", action="store_true", help="Check current working tree in a separate rehearsal archive")
        a = p.parse_args()
        outer(a.commit, a.product.resolve(), a.node.resolve(), a.official.resolve(), a.python.resolve(), a.out.resolve(), a.rehearse, a.archive.resolve() if a.archive else None)
