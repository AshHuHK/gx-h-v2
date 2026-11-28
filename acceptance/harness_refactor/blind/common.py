"""Shared frozen materials, official subscription route and anonymization."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = "50d31f5792a178529a817ffd93f66ef9cbbb0be4"
sys.path[:0] = [str(ROOT), str(ROOT/"gx-h")]
from gx_core.native_release import write_native_bytes

MODEL = "gpt-6.2-astra"
EFFORT = "max"
VENDORED = ROOT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
BINARY = Path(os.environ.get("GX_P0_CODEX") or (VENDORED if VENDORED.is_file() else shutil.which("codex") or VENDORED))
ARMS = ("stock", "baseline", "revised")
LIMIT = 900
PACKET = HERE / "packet"

def oauth_host():
    """Load the live subscription binding only for operations that use the official host."""
    from acceptance.v623.oauth import host
    return host

def save(path, record):
    data=(json.dumps(record,sort_keys=True,ensure_ascii=False,indent=2,allow_nan=False)+"\n").encode()
    path.parent.mkdir(parents=True,exist_ok=True)
    with (path.parent/".receipt-writer.lock").open("a") as lockfile:
        fcntl.flock(lockfile,fcntl.LOCK_EX)
        if path.exists():
            if path.read_bytes()!=data:raise ValueError("An existing original receipt differs")
            return
        write_native_bytes(path,data,mode=0o600)

def read(path):
    return json.loads(path.read_bytes())

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def environment(folder):
    """Credentials remain in the deployment credential store. Each arm has its own session spool."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    tag = hashlib.sha256(os.fsencode(folder.resolve())).hexdigest()[:32]
    host = oauth_host()
    binary, _ = host._pin(BINARY)
    env = host._env(host._state()/("blind-"+tag),binary,MODEL,EFFORT,sessions=folder/"sessions")
    node = shutil.which("node")
    env["PATH"] = os.pathsep.join([str(ROOT/".venv/bin"), *([str(Path(node).parent)] if node else []),env["PATH"]])
    return env

def sources():
    return {k:sha(p) for k,p in (("question",PACKET/"question.txt"),("initial",PACKET/"initial.json"),
            ("late",PACKET/"late.json"),("rubric",HERE/"rubric.json"),("origins",PACKET/"sources.json"))}

def study_software():
    return {name:sha(HERE/name) for name in (
        "build_materials.py","common.py","native.py","probe.py","score.py","trial.py")}

def lock(folder):
    spec = read(HERE/"preregistration.json")
    if spec["inputs"] != sources():
        raise ValueError("Registered inputs, scientific rubric or source identities changed")
    if spec["study_software"] != study_software():
        raise ValueError("Registered benchmark, host or blind reader implementation changed")
    save(folder/"registration.json",spec)
    return spec

def blind_text(text):
    # Remove transport identifiers only. Preserve protein IDs, scientific locators and all numerical claims.
    text = re.sub(r'\b(?:gx[a-z]_[a-zA-Z0-9]{10,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b',
                  '[record]', text)
    text = re.sub(r'\bgx_[a-z0-9_]+\b', '[study tool]', text, flags=re.I)
    return re.sub(r'\b(?:GX-H|GX|Codex)\b', '[system]', text, flags=re.I)
