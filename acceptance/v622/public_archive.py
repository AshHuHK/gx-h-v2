"""Preserve the original public Run evidence and replay its two-stage scorer offline."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
SCORE = ROOT / "acceptance/v622/public/rate.py"
ARCHIVE = "public-raw-replay.tar.gz"


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def snapshot(src, dst):
    with closing(sqlite3.connect(src.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(dst)) as out:
            original.backup(out)


def pack(work, dest):
    reg = json.loads((work / "registration.json").read_bytes())
    if reg["scope"] not in ("SCRIPTED_REHEARSAL", "FORMAL_OFFLINE") or reg["paid_calls_authorized"]:
        raise ValueError("Expected a registered public offline batch")
    dest.mkdir(parents=True, exist_ok=False)
    for name in ("registration.json", "runtime-pins.json", "result.json", "scores.json",
                 "verification.json", "capability-parity.json", "funding.json"):
        shutil.copyfile(work / name, dest / name)
    shutil.copyfile(work.parent / "network.json", dest / "network.json")
    with TemporaryDirectory() as temp, tarfile.open(dest / ARCHIVE, "x:gz", compresslevel=6) as out:
        def add(file, arcname=None):
            if file.is_symlink() or not file.is_file():
                raise ValueError(f"Invalid evidence file {file}")
            out.add(file, arcname or file.relative_to(work).as_posix(), recursive=False)

        for name in ("registration.json", "runtime-pins.json", "custody/oracle.json"):
            add(work / name)
        for file in sorted((work / "custody").glob("*/*")):
            if file.name in ("specimen.cif", "transform.json"):
                add(file)
        for registration in reg["slots"]:
            num = registration["slot"]
            for name in ("opportunity-packet", "opportunity-lock", "outcome-packet", "causal-trace", "review", "score"):
                add(work / "ratings" / num / (name + ".json"))
            slot = work / num
            for name in ("assignment.json", "summary.json", "first-attempt.json", "raw/commands.json", "raw/requests.jsonl", "raw/wire.json"):
                add(slot / name)
            backup = Path(temp) / (num + ".db")
            snapshot(slot / "state/gx.db", backup)
            add(backup, num + "/state/gx.db")
            for file in sorted((slot / "state/artifacts").rglob("*")):
                if file.is_file():
                    add(file)
            for file in sorted((slot / "codex/sessions").rglob("*.jsonl")):
                add(file)


def replay(directory, write=False):
    reg = json.loads((directory / "registration.json").read_bytes())
    with TemporaryDirectory() as tmp:
        work = Path(tmp) / "run"
        work.mkdir()
        with tarfile.open(directory / ARCHIVE, "r:gz") as tar:
            names = set()
            for member in tar:
                path = work / member.name
                if not member.isreg() or not path.resolve().is_relative_to(work) or member.name in names:
                    raise ValueError("Unexpected archive member " + member.name)
                names.add(member.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("xb") as out:
                    shutil.copyfileobj(source, out)
        for name in ("registration.json", "runtime-pins.json"):
            if (work / name).read_bytes() != (directory / name).read_bytes():
                raise ValueError("Archive differs from published " + name)
        output = Path(tmp) / "rerated"
        argv = [str(ROOT / ".venv/bin/python"), "-B", str(SCORE), "--work", str(work),
                "--output", str(output), "--stage", "both"]
        run = subprocess.run(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if write:
            (directory / "replay.log").write_bytes(run.stdout)
        if run.returncode:
            raise ValueError(run.stdout.decode(errors="replace")[-2000:])
        for original in sorted((work / "ratings").glob("*/*.json")):
            freshly_read = output / original.relative_to(work / "ratings")
            if sha(original) != sha(freshly_read):
                raise ValueError("Independent replay changed " + str(original.relative_to(work)))
        reproduced = sha(output / "scores.json")
        if reproduced != sha(directory / "scores.json"):
            raise ValueError("Independent replay of the original Runs produced different scores")
        receipt = dict(status="PASS", source_commit=reg["source_commit"], archive_sha256=sha(directory / ARCHIVE),
                       scores_sha256=reproduced, series=reg["series"], reproduced_slots=len(reg["slots"]))
        if write:
            (directory / "replay.json").write_text(json.dumps(dict(receipt, argv=argv, exit_code=run.returncode), indent=2) + "\n")
        return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--work", type=Path, required=True, help="Original completed batch directory containing registration.json")
    for subparser in (p, sub.add_parser("replay")):
        subparser.add_argument("--receipts", type=Path, required=True)
    args = parser.parse_args()
    dest = args.receipts.resolve()
    if args.command == "pack":
        pack(args.work.resolve(), dest)
    print(json.dumps(replay(dest, write=args.command == "pack")))
