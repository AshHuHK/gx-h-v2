"""Identify the release being exercised by an acceptance tool."""
import json
from pathlib import Path
import re
import subprocess


def revision(directory: Path) -> str:
    root = directory.resolve()
    manifest = root / "RELEASE_CONTENTS.json"
    if manifest.is_file():
        base = json.loads(manifest.read_bytes()).get("git_base")
        if not isinstance(base, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base):
            raise ValueError(f"Invalid git_base in {manifest}")
        return base
    git_dir = root / ".git"
    if not git_dir.exists():
        raise ValueError(f"Missing release manifest or Git checkout at {root}")
    # Pin the gitdir explicitly, including a linked worktree's .git file.
    return subprocess.check_output(
        ["git", "--git-dir", str(git_dir), "rev-parse", "--verify", "HEAD"], text=True
    ).strip()
