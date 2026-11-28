"""Read-only site diagnostic for the four frozen archive databases used by the yeast reader."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3

HERE = Path(__file__).resolve().parents[3] / "gx-h"
PROVIDER = HERE / "plugins/installed/org.gx.yeast-enzyme-archive"
MOUNT = HERE / "var/science-assets/yeast-archive"


def declarations():
    # One source of truth for file roles (read source via run_path, no import cache writes).
    reader = runpy.run_path(str(PROVIDER / "1.0.0/archive.py"))
    pins = json.loads((PROVIDER / "1.0.0/snapshots.json").read_text())
    return {role:(relative, pins[role]) for role, relative in reader["FILES"].items()}


def inspect_snapshot(path, expected):
    try:
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle,"sha256").hexdigest()
        if digest == expected:
            return {"status":"verified", "sha256":digest}
        # Nonmatching bytes: distinguish unreadable/damaged SQLite from a coherent
        # database outside the registered snapshot. Never query it for science.
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro&immutable=1", uri=True)) as db:
            integrity = [row[0] for row in db.execute("PRAGMA quick_check(1)")]
            has_tables = bool(db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1").fetchone())
        status = "unregistered_version" if has_tables and integrity == ["ok"] else "damaged"
        return {"status":status,"sha256":digest}
    except FileNotFoundError:
        return {"status":"missing"}
    except sqlite3.DatabaseError:
        return {"status":"damaged", "sha256":digest}
    except OSError as error:
        return {"status":"unreadable","error":type(error).__name__}


def inspect_mount(root):
    return {role:{"file":relative,"expected_sha256":pin,**inspect_snapshot(root/relative, pin)}
            for role,(relative,pin) in declarations().items()}


def require_archive(test):
    # Empty checkout may lack licensed data. A partial install or a present,
    # corrupt file is a real failure on a provisioned station.
    if not MOUNT.exists():
        test.skipTest("licensed yeast archive mount absent; run the portable missing-mount diagnostic")
    for item in inspect_mount(MOUNT).values():
        test.assertEqual(item["status"], "verified", item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", type=Path, default=MOUNT)
    report = inspect_mount(parser.parse_args().mount)
    print(json.dumps(report, indent=2))
    raise SystemExit(int(any(item["status"] != "verified" for item in report.values())))
