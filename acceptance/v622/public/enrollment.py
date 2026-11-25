"""Freeze the original public assignments, custody digests, product and role grants."""
import hashlib
import json
from pathlib import Path
import os
import platform
import subprocess
from importlib.metadata import version
from tempfile import NamedTemporaryFile

from acceptance.v62c import rehearse as historical
from acceptance.v62c import materials


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PROTOCOL = json.loads((HERE / "protocol.json").read_bytes())


def read(path):
    return json.loads(path.read_bytes())


def save(path, value):
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def grant(architecture):
    result = historical.grant(architecture)
    result["evidence_policy"] = PROTOCOL["evidence_policy"]
    return result


def pin_files():
    specs = [*HERE.glob("*.py"), HERE / "protocol.json", HERE / "run-public.sh",
             historical.CARD, historical.HERE / "goal.md", materials.PARENT,
             ROOT / "release-assets/lock.json", ROOT / "bin/gx-p0", ROOT / "acceptance/e2e_responses.py",
             ROOT / "acceptance/phase2a/regression/loopback.py", ROOT / "acceptance/closure/profile.py",
             ROOT / "vendor/codex/codex-rs/models-manager/models.json"]
    for prefix, pattern in [("gx-h/gx_core", "*.py"), ("gx-h/contracts", "*"),
                            ("gx-h/config", "*.json"), ("gx-h/adapters/scientific", "*.py"),
                            ("gx-h/plugins/installed/org.gx.protein/0.3.0", "*"),
                            ("gx-h/plugins/installed/org.gx.data-inventory/2.0.0", "*"),
                            ("acceptance/v62c", "*.py"), ("acceptance/v62b/engineering", "*.py")]:
        specs.extend((ROOT / prefix).rglob(pattern))
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(set(specs)) if p.is_file()
            and "__pycache__" not in p.parts and "receipts" not in p.parts}


def register(work, suite, binary):
    """Also validate all frozen inputs before resuming an incomplete enrollment."""
    existing = (work / "registration.json").is_file()
    if existing:
        frozen = read(work / "registration.json")
        if frozen["suite"] != suite or frozen["protocol_sha256"] != digest(HERE / "protocol.json"):
            raise ValueError("An enrolled study must retain its original suite and protocol")
    binary_sha = read(ROOT / "release-assets/lock.json")["linux_x64"]["binary_sha256"]
    if digest(binary) != binary_sha:
        raise ValueError("Official host differs from its release pin")
    custody = work / "custody"
    if not custody.is_dir():
        if existing:
            raise ValueError("The original committed materials are missing")
        # Registration may be continued even when preparation was interrupted before any slot started.
        from tempfile import TemporaryDirectory
        with TemporaryDirectory(dir=work) as tmp:
            staging = Path(tmp) / "custody"
            materials.prepare(staging)
            staging.rename(custody)
    endpoints = read(custody / "oracle.json")
    for code, item in endpoints.items():
        declared = PROTOCOL["materials"][code]
        if (item["sha256"] != digest(custody / code / "specimen.cif") or
                item["transform_sha256"] != digest(custody / code / "transform.json") or
                any(item[k] != v for k, v in declared.items())):
            raise ValueError("Frozen source material or transformation changed")
    pins = pin_files()
    if existing:
        if (pins != read(work / "runtime-pins.json") or digest(work / "runtime-pins.json") != frozen["runtime_pins_sha256"]
                or digest(custody / "oracle.json") != frozen["endpoints_sha256"] or frozen["binary_sha256"] != binary_sha):
            raise ValueError("Resume requires the originally registered files")
        return frozen
    save(work / "runtime-pins.json", pins)
    slots = PROTOCOL["slots"] if suite == "standard" else PROTOCOL["controls"]
    reg = {"series": PROTOCOL["series"] if suite == "standard" else PROTOCOL["controls_series"], "suite": suite,
           "source_commit": subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
           "scope": "SCRIPTED_REHEARSAL" if suite == "standard" else "FORMAL_OFFLINE",
           "provider": "gx_loopback", "wire_model": PROTOCOL["model"], "wire_effort": PROTOCOL["model_effort"],
           "sampling": PROTOCOL["sampling"], "research_effort": PROTOCOL["research_effort"],
           "python": platform.python_version(), "biopython": version("biopython"), "binary_sha256": binary_sha,
           "source_pins": {p.name: digest(p) for p in HERE.glob("*.py")},
           "runtime_pins_sha256": digest(work / "runtime-pins.json"), "protocol_sha256": digest(HERE / "protocol.json"),
           "endpoints_sha256": digest(custody / "oracle.json"), "slots": slots,
           "grants": {a:grant(a) for a in ("single","delegation_available")},
           "production_paid_batch_ready": False, "paid_calls_authorized": False}
    save(work / "registration.json", reg)
    return reg
