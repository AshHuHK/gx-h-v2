"""Prepare and verify immutable task deliveries before acquiring the GX write lock.

A complete directory is installed by rename. Before finish, its manifest and bytes
can be reopened, but it has no submission or artifact-registration side effects.
Existing GX artifacts can be cited in place. A writer registers new artifacts, the
submission and its receipt in the same transaction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from ..domain import clock, ids, machines
from ..errors import ArtifactHashMismatch, ArtifactNotFound, PolicyScreenBlocked, SchemaInvalid
from ..persistence import store
from .service import ArtifactService, PublishedArtifact, _SAFE_KIND, credentials_in, sync_directory_chain

SCHEMA = "gx.artifact_manifest/1"


@dataclass(frozen=True)
class PreparedManifest:
    manifest_ref: str
    run_id: str
    task_id: str
    attempt: int
    provenance: dict
    entries: tuple[dict, ...]


@contextmanager
def _directory_fd(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def _staging(artifacts):
    path = artifacts.staging_root() / "task-delivery"
    path.mkdir(exist_ok=True)
    return path


def _namespace(run_id, task_id, digest):
    return f"{run_id}/deliveries/{task_id}/{digest}"


def _prepare_entry(artifacts, run_id, stage, index, spec):
    if not isinstance(spec, dict):
        raise SchemaInvalid("a delivery entry must be an object")
    if ("artifact_ref" in spec) == ("inbox_path" in spec):
        raise SchemaInvalid("give exactly one of inbox_path and artifact_ref for each entry")
    if "artifact_ref" in spec:
        row = artifacts.describe(spec["artifact_ref"])
        if row["run_id"] != run_id:
            raise SchemaInvalid("a cited artifact must belong to the same run")
        return {key: row[key] for key in ("artifact_ref", "sha256", "bytes", "kind", "media_type")}
    kind = spec.get("kind")
    media_type = spec.get("media_type")
    if not isinstance(kind, str) or not _SAFE_KIND.fullmatch(kind):
        raise SchemaInvalid("a new delivery entry needs a valid artifact kind")
    if not isinstance(media_type, str) or "/" not in media_type or len(media_type) > 100:
        raise SchemaInvalid("a new delivery entry needs a media type")
    source = artifacts._resolve_inbox(run_id, spec["inbox_path"])
    target = stage / f"entry-{index}"
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, 1 << 20)
        writer.flush()
        os.fsync(writer.fileno())
    matches = credentials_in(target)
    if matches:
        raise PolicyScreenBlocked("credential patterns matched in an inbox file", details={"matched": matches})
    return {"kind": kind, "media_type": media_type, "sha256": ids.sha256_of_file(target),
            "bytes": target.stat().st_size}


def prepare_manifest(artifacts: ArtifactService, *, run_id: str, task_id: str, attempt: int,
                     entries, provenance: dict) -> str:
    """Freeze inbox outputs or existing artifact refs. Neither consumes the inbox nor writes DB rows."""
    if artifacts.conn.in_transaction:
        raise RuntimeError("prepare delivery outside the writer transaction")
    task = artifacts.conn.execute("SELECT run_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None or task["run_id"] != run_id:
        raise SchemaInvalid("delivery task must belong to its run")
    if type(attempt) is not int or attempt < 1 or not isinstance(provenance, dict):
        raise SchemaInvalid("delivery needs an attempt number and a provenance object")
    if not isinstance(entries, (list, tuple)) or not entries:
        raise SchemaInvalid("delivery requires at least one original")
    staging = _staging(artifacts)
    with _directory_fd(staging) as staging_fd:
        # GC holds the exclusive lock, so even a slow copy remains live.
        fcntl.flock(staging_fd, fcntl.LOCK_SH)
        stage = Path(tempfile.mkdtemp(prefix="pending-", dir=staging))
        try:
            outputs = [_prepare_entry(artifacts, run_id, stage, index, entry)
                       for index, entry in enumerate(entries)]
            manifest = dict(schema=SCHEMA, run_id=run_id, task_id=task_id, attempt=attempt,
                            entries=outputs, provenance=provenance)
            digest = ids.sha256_of_obj(manifest)
            with (stage / "manifest.json").open("xb") as writer:
                writer.write(ids.canonical_json(manifest).encode())
                writer.flush()
                os.fsync(writer.fileno())
            with _directory_fd(stage) as fd:
                os.fsync(fd)
            target = artifacts.namespace_root(_namespace(run_id, task_id, digest))
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(stage, target)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                if not (target / "manifest.json").is_file() or ids.sha256_of_file(target / "manifest.json") != digest:
                    raise ArtifactHashMismatch("a previous delivery has an invalid manifest") from exc
            sync_directory_chain(target.parent, artifacts.base)
            return "sha256:" + digest
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def load_manifest(artifacts: ArtifactService, manifest_ref: str, *, run_id: str, task_id: str,
                  attempt: int) -> PreparedManifest:
    if artifacts.conn.in_transaction:
        raise RuntimeError("verify delivery outside the writer transaction")
    if isinstance(manifest_ref, str) and ids.kind_of(manifest_ref) == "artifact":
        desc = artifacts.describe(manifest_ref)
        if desc["run_id"] != run_id or desc["task_id"] != task_id:
            raise SchemaInvalid("a single-product reference must be published for this run and task")
        if not artifacts.verify(manifest_ref)["ok"]:
            raise ArtifactHashMismatch("single-product original is missing or corrupt")
        row = artifacts._row(manifest_ref)
        return PreparedManifest(manifest_ref, run_id, task_id, attempt,
                                store.loads(row, "provenance_json", {}),
                                (dict(desc, existing=True),))
    if not isinstance(manifest_ref, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", manifest_ref):
        raise SchemaInvalid("manifest_ref must be the sha256 reference returned during preparation")
    digest = manifest_ref[7:]
    namespace = _namespace(run_id, task_id, digest)
    path = artifacts.resolve_path(namespace, "manifest.json")
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ArtifactNotFound("delivery manifest is unavailable for this run and task") from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ArtifactHashMismatch("delivery manifest differs from its reference")
    manifest = json.loads(raw)
    if (manifest["schema"], manifest["run_id"], manifest["task_id"], manifest["attempt"]) != (
            SCHEMA, run_id, task_id, attempt):
        raise SchemaInvalid("manifest is for another run, task, attempt or schema")
    entries = []
    for index, entry in enumerate(manifest["entries"]):
        if "artifact_ref" in entry:
            desc = artifacts.describe(entry["artifact_ref"])
            if desc["run_id"] != run_id or any(desc[k] != v for k, v in entry.items()):
                raise ArtifactHashMismatch("cited original differs from its manifest pin")
            if not artifacts.verify(desc["artifact_ref"])["ok"]:
                raise ArtifactHashMismatch("cited original is missing or corrupt")
            entries.append(dict(desc, existing=True))
            continue
        name = f"entry-{index}"
        original = artifacts.resolve_path(namespace, name)
        if not original.is_file() or original.stat().st_size != entry["bytes"] or ids.sha256_of_file(original) != entry["sha256"]:
            raise ArtifactHashMismatch("delivery file is missing or differs from its manifest pin",
                                       details={"manifest_ref": manifest_ref, "entry": index})
        artifact_ref = "gxa_" + ids.sha256_of_obj([digest, index])
        entries.append(dict(entry, artifact_ref=artifact_ref, namespace=namespace, rel_path=name,
                            run_id=run_id, task_id=task_id, existing=False, path=str(original)))
    return PreparedManifest(manifest_ref, run_id, task_id, attempt, manifest["provenance"], tuple(entries))


def register_manifest(artifacts: ArtifactService, prepared: PreparedManifest, *, actor: str) -> list[str]:
    """Install prepared rows; the owner commits the formal submission or rolls it all back."""
    if not artifacts.conn.in_transaction:
        raise RuntimeError("register delivery inside the finish transaction")
    if prepared.manifest_ref.startswith("sha256:"):
        namespace = _namespace(prepared.run_id, prepared.task_id, prepared.manifest_ref[7:])
        if not artifacts.resolve_path(namespace, "manifest.json").is_file():
            raise ArtifactNotFound("delivery manifest was removed before registration")
    for desc in prepared.entries:
        if desc["existing"]:
            continue
        registered = artifacts.conn.execute("SELECT sha256,namespace,rel_path FROM artifacts WHERE artifact_ref=?",
                                            (desc["artifact_ref"],)).fetchone()
        if registered is not None:
            if tuple(registered) != (desc["sha256"], desc["namespace"], desc["rel_path"]):
                raise ArtifactHashMismatch("a prepared artifact conflicts with its registered reference")
            continue
        fields = ("artifact_ref", "namespace", "rel_path", "kind", "sha256", "bytes", "media_type", "path")
        item = PublishedArtifact(**{key: desc[key] for key in fields}, created_at=clock.now_iso())
        artifacts.register_materialized(
            item, run_id=prepared.run_id, task_id=prepared.task_id, actor=actor, origin="HOST_PUT",
            provenance={**prepared.provenance, "attempt": prepared.attempt, "manifest_ref": prepared.manifest_ref},
        )
    return [entry["artifact_ref"] for entry in prepared.entries]


def discard_orphan(artifacts: ArtifactService, *, run_id: str, task_id: str, attempt: int,
                   manifest_ref: str) -> bool:
    """Explicitly release a complete, unregistered delivery from a retired attempt.

    The rename is serialized with finish. Reclaiming a large directory happens
    after releasing SQLite; if cleanup stops mid-delete, staging GC can resume it.
    """
    if artifacts.conn.in_transaction:
        raise RuntimeError("discard a retired delivery outside an existing transaction")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", manifest_ref):
        raise SchemaInvalid("discard requires a prepared delivery reference")
    namespace = _namespace(run_id, task_id, manifest_ref[7:])
    folder = artifacts.namespace_root(namespace)
    staging = _staging(artifacts)
    with _directory_fd(staging) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if not folder.is_dir():
            return False
        raw = (folder / "manifest.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest_ref[7:]:
            raise ArtifactHashMismatch("cannot discard a delivery with an inconsistent manifest")
        metadata = json.loads(raw)
        if (run_id, task_id, attempt) != (metadata["run_id"], metadata["task_id"], metadata["attempt"]):
            raise SchemaInvalid("discard must identify the original run, task and attempt")
        with store.transaction(artifacts.conn) as conn:
            task = conn.execute("SELECT attempt,status FROM tasks WHERE run_id=? AND task_id=?",
                                (run_id, task_id)).fetchone()
            if task is None or (task["attempt"] <= attempt and not machines.is_terminal("task", task["status"])):
                return False
            if conn.execute("SELECT 1 FROM artifacts WHERE namespace=? LIMIT 1", (namespace,)).fetchone():
                return False
            if conn.execute("SELECT 1 FROM submissions WHERE task_id=? AND attempt=? LIMIT 1",
                            (task_id, attempt)).fetchone():
                return False
            abandoned = Path(tempfile.mkdtemp(prefix="pending-discard-", dir=staging))
            os.rename(folder, abandoned / "retired")
        shutil.rmtree(abandoned)
    return True


def clean_orphans(artifacts: ArtifactService, *, min_age_seconds: float = 86400) -> list[str]:
    """Remove abandoned incomplete writes only. Completed manifest directories live with the run."""
    if min_age_seconds < 0:
        raise ValueError("minimum age must be nonnegative")
    staging = _staging(artifacts)
    removed = []
    cutoff = time.time() - min_age_seconds
    with _directory_fd(staging) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        for path in staging.glob("pending-*"):
            if path.is_dir() and not path.is_symlink() and path.stat().st_mtime <= cutoff:
                shutil.rmtree(path)
                removed.append(path.name)
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="freeze task outputs")
    prep.add_argument("--run", required=True)
    prep.add_argument("--task", required=True)
    prep.add_argument("--attempt", required=True, type=int)
    prep.add_argument("--spec", required=True, type=Path, help="JSON with entries and provenance")
    gc = sub.add_parser("gc", help="remove abandoned incomplete writes")
    gc.add_argument("--min-age-seconds", type=float, default=86400)
    discard = sub.add_parser("discard", help="discard an unregistered delivery of a retired attempt")
    discard.add_argument("--run", required=True)
    discard.add_argument("--task", required=True)
    discard.add_argument("--attempt", required=True, type=int)
    discard.add_argument("--manifest", required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("GX database does not exist")
    conn = store.open_store(args.db)
    try:
        artifacts = ArtifactService(conn)
        if args.command == "gc":
            result = {"removed": clean_orphans(artifacts, min_age_seconds=args.min_age_seconds)}
        elif args.command == "discard":
            result = {"discarded": discard_orphan(artifacts, run_id=args.run, task_id=args.task,
                                                  attempt=args.attempt, manifest_ref=args.manifest)}
        else:
            spec = json.loads(args.spec.read_text())
            result = {"manifest_ref": prepare_manifest(
                artifacts, run_id=args.run, task_id=args.task, attempt=args.attempt,
                entries=spec["entries"], provenance=spec["provenance"])}
        print(json.dumps(result))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
