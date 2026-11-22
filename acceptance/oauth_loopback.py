"""Run the package's xhigh smoke command against a local scripted Responses service."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from e2e_grid import fixture_env
from native_probe_peer import Peer


PACKAGE = Path(__file__).resolve().parents[1]


class SmokePeer(Peer):
    def __init__(self):
        super().__init__()
        self.efforts = []

    def record(self, body):
        self.efforts.append(body.get("reasoning", {}).get("effort"))
        return super().record(body)

    def item(self, row, n):
        return self.message(row["id"], "GX_P0_OAUTH_OK")


def run():
    binary = shutil.which(os.environ.get("GX_P0_CODEX") or "codex")
    if binary is None:
        raise FileNotFoundError("Select the pinned Codex CLI with GX_P0_CODEX.")
    catalog = PACKAGE / "vendor/codex/codex-rs/models-manager/models.json"
    models = json.loads(catalog.read_text())["models"]
    model = next(row["slug"] for row in models if row["visibility"] == "list" and row["supported_in_api"]
                 and any(level["effort"] == "xhigh" for level in row["supported_reasoning_levels"]))
    with tempfile.TemporaryDirectory(prefix="gx-p0-doctor-loopback-") as work, SmokePeer() as peer:
        server = threading.Thread(target=peer.serve_forever, daemon=True)
        server.start()
        try:
            env = fixture_env(Path(work), peer, binary)
            env["GX_P0_MODEL"] = model
            config = Path(env["CODEX_HOME"]) / "config.toml"
            config.write_text(f"model_catalog_json = {json.dumps(str(catalog))}\n" + config.read_text())
            process = subprocess.run([str(PACKAGE / "bin/gx-p0"), "doctor", "--smoke"], env=env,
                                     capture_output=True, text=True, timeout=65, check=True)
        finally:
            peer.shutdown()
            server.join()
        result = json.loads(process.stdout)
        if not result["inference"]["matched"] or peer.efforts != ["xhigh"]:
            raise RuntimeError("The official client did not complete exactly one xhigh loopback smoke request.")
        return {"command": "bin/gx-p0 doctor --smoke", "service": "scripted loopback Responses",
                "cli_version": result["codex_version"], "model": model, "provider": result["provider"],
                "inference": result["inference"], "observed_wire_efforts": peer.efforts,
                "exit_code": process.returncode}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
