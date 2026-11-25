"""Pinned archive requests for a custodian workspace; read cached records on replay."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
from urllib.parse import urlparse

PROXY = "http://127.0.0.1:7890"
SOCKS = "socks5h://127.0.0.1:7891"
BYPASS = "localhost,127.0.0.1,::1"
HOSTS = {"rest.uniprot.org", "search.rcsb.org", "data.rcsb.org", "files.rcsb.org",
         "www.ebi.ac.uk", "www.wwpdb.org", "www.rcsb.org", "pmc.ncbi.nlm.nih.gov"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def fetch(work, name, url):
    """Retrieve from the named public archival origin over the workspace Clash. No credentials."""
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in HOSTS:
        raise ValueError("Unsupported archive URL")
    raw = work / "snapshots" / name
    raw.parent.mkdir(parents=True, exist_ok=True)
    receipt = raw.with_name(raw.name + ".http.json")
    if receipt.exists():
        meta = json.loads(receipt.read_bytes())
        if meta["url"] != url or not raw.exists() or sha(raw) != meta["sha256"]:
            raise ValueError("Cached archive bytes differ from the pinned request")
        return raw
    for port in (7890, 7891):
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    env = os.environ.copy()
    for key, value in {"HTTP_PROXY": PROXY, "HTTPS_PROXY": PROXY, "ALL_PROXY": SOCKS, "NO_PROXY": BYPASS}.items():
        env[key] = env[key.lower()] = value
    with tempfile.NamedTemporaryFile(dir=raw.parent) as tmp:
        cmd = ["curl", "--silent", "--show-error", "--fail", "--location",
               "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10",
               "--max-time", "45", "--retry", "1", "--proxy", PROXY,
               "--user-agent", "gx-h/archived-structure-qualification", "--output", tmp.name,
               "--write-out", "%{http_code} %{url_effective}", url]
        bundle = ssl.get_default_verify_paths().cafile
        if bundle and Path(bundle).is_file():
            cmd[1:1] = ["--cacert", bundle]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode:
            write(raw.with_name(raw.name + ".failed.json"), {"url":url, "at":now(), "exit":result.returncode,
                   "status": result.stdout.strip(), "error": result.stderr.strip()[:600]})
            raise RuntimeError("Archive request failed: " + name)
        response = tmp.read()
        if raw.exists() and raw.read_bytes() != response:
            raise ValueError("Unreceipted archive file already exists")
        raw.write_bytes(response)
    write(receipt, {"url":url, "at":now(), "http":result.stdout.strip(), "sha256":sha(raw), "bytes":len(response)})
    return raw
