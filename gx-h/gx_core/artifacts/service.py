"""Artifact storage: staging, hashing, validation, atomic publish, bounded reads.

Two failure modes shape every method here.

**A half-written file read as a finished result.** A process that dies mid-write
leaves a file that parses, looks plausible and is wrong. So nothing is ever written
at its final path: output goes to a staging directory, is hashed and validated there,
and only then becomes the real path in a single ``os.replace``. A reader can never
observe a partial artifact, because the partial bytes never had the artifact's name.

**Bytes that moved under a recorded hash.** An artifact_ref is a promise that these
exact bytes backed a conclusion. :meth:`ArtifactService.get` re-hashes on read and
raises ``ARTIFACT_HASH_MISMATCH`` rather than returning content that no longer
matches, because a conclusion derived from changed bytes is not a conclusion that
was ever checked.

Paths are the third concern. Namespaces isolate one path's outputs from another's,
and the isolation has to hold against ``..``, against absolute paths, and against a
symlink planted inside a namespace that points somewhere else. All three are refused
by :meth:`ArtifactService.resolve_path` with ``PATH_TRAVERSAL_DENIED``.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

from .. import config, contracts
from ..domain import clock, ids
from ..errors import (
    ArtifactHashMismatch,
    ArtifactNotFound,
    GxError,
    ImplementationGap,
    PathTraversalDenied,
    PolicyScreenBlocked,
    SchemaInvalid,
)
from ..persistence import store

#: Directory under the artifact root that holds work in progress. It is not a
#: namespace and can never be published into.
STAGING_DIRNAME = ".staging"

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]*")
_SAFE_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}")

#: The intake screen reads a file this many bytes at a time, keeping the last
#: :data:`SCREEN_OVERLAP` bytes of one read in front of the next, so a credential that
#: straddles two reads is still seen. The overlap is longer than the longest match any
#: entry of ``screens.credentials`` can make: every quantifier there is bounded (the JWT
#: entry's is the longest, about 7,000 bytes).
SCREEN_CHUNK = 1 << 20
SCREEN_OVERLAP = 8192
_SCREEN_CACHE: dict[str, tuple[tuple[str, "re.Pattern[bytes]"], ...]] = {}


def _credential_patterns() -> tuple[tuple[str, "re.Pattern[bytes]"], ...]:
    """``(id, pattern)`` for every ``credentials`` entry of ``config/limits.json`` ``screens``.

    Read on every call and compiled once per content, so a pattern the lead adds applies to
    the next put. A file that lists none is refused: content is never taken in unscreened.
    """
    entries = (config.limits().get("screens") or {}).get("credentials") or []
    key = ids.sha256_of_obj(entries)
    if key not in _SCREEN_CACHE:
        compiled = []
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("id") and entry.get("pattern"):
                compiled.append((str(entry["id"]), re.compile(str(entry["pattern"]).encode("utf-8"))))
        _SCREEN_CACHE[key] = tuple(compiled)
    patterns = _SCREEN_CACHE[key]
    if not patterns:
        raise ImplementationGap(
            "config/limits.json screens lists no credentials entry, so content cannot be "
            "screened and is not taken in",
            details={"screen": "config/limits.json#screens.credentials"},
        )
    return patterns


def credentials_in(path: Path) -> list[str]:
    """The ids of the ``screens.credentials`` entries that match anywhere in a file's bytes.

    Only the ids: the matched text is never returned, so neither a refusal nor a log line
    can repeat a credential.
    """
    patterns = _credential_patterns()
    hits: set[str] = set()
    tail = b""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(SCREEN_CHUNK)
            if not block:
                break
            window = tail + block
            for entry_id, pattern in patterns:
                if entry_id not in hits and pattern.search(window):
                    hits.add(entry_id)
            tail = window[-SCREEN_OVERLAP:]
    return sorted(hits)

#: File extension a put artifact is stored under, by media type. Anything else is .bin.
_EXTENSIONS = {
    "application/json": ".json", "application/x-ndjson": ".jsonl", "application/jsonl": ".jsonl",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
    "text/tab-separated-values": ".tsv", "application/xml": ".xml", "application/yaml": ".yaml",
    "application/x-fasta": ".fasta", "application/pdf": ".pdf",
}

#: Media types whose bytes are returned as text. Everything else is described but
#: never inlined: a response is a bounded summary, not a transport for binaries.
_TEXT_MEDIA_PREFIXES = ("text/",)
_TEXT_MEDIA_TYPES = {
    "application/json", "application/x-ndjson", "application/jsonl",
    "application/xml", "application/yaml", "application/x-yaml",
    "application/x-fasta", "chemical/seq-na-fasta",
    "chemical/x-pdb", "chemical/x-cif", "chemical/x-mmcif", "chemical/pdb", "chemical/mmcif",
}


def sync_directory_chain(directory: Path, base: Path) -> None:
    """Make a directory entry and the path to it durable before citing it in SQLite.

    A concurrent writer may just have created an ancestor, so even an already
    visible directory needs its parent synced. The store directory anchors the tree.
    """
    relative = directory.relative_to(base)
    for _ in range(len(relative.parts) + 1):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = directory.parent


def _j(value: Any) -> str:
    return ids.canonical_json(value)


@dataclass(frozen=True)
class PublishedArtifact:
    artifact_ref: str
    namespace: str
    rel_path: str
    kind: str
    sha256: str
    bytes: int
    media_type: str | None
    path: str
    created_at: str
    reused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_ref": self.artifact_ref,
            "namespace": self.namespace,
            "rel_path": self.rel_path,
            "kind": self.kind,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "media_type": self.media_type,
            "created_at": self.created_at,
            "reused": self.reused,
        }


class ArtifactService:
    """Namespaced artifact store over one SQLite connection and one directory tree.

    With no ``root`` the tree is ``artifacts/`` beside the store file, and the run
    inboxes and outboxes are ``runs/<run_id>/`` beside it too (:func:`store.data_root`).
    A store and the bytes its rows name are one thing and move as one. An explicit
    ``root`` is for a caller that owns its own layout; its inboxes and outboxes then
    live beside that root.
    """

    def __init__(self, conn: sqlite3.Connection, root: str | Path | None = None) -> None:
        self.conn = conn
        if root is not None:
            self.root = Path(root).resolve()
            self.base = self.root.parent
        else:
            self.base = store.data_root(conn)
            self.root = (self.base / "artifacts").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ inbox and outbox

    def _run_dir(self, run_id: str, leaf: str, *, create: bool) -> Path:
        if not isinstance(run_id, str) or not _SAFE_COMPONENT.fullmatch(run_id):
            raise PathTraversalDenied(
                f"{run_id!r} is not a run id", details={"run_id": run_id}
            )
        path = self.base / "runs" / run_id / leaf
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def inbox_dir(self, run_id: str, *, create: bool = True) -> Path:
        """Where a host places a file too large to pass inline, before ``put``."""
        return self._run_dir(run_id, "inbox", create=create)

    def outbox_dir(self, run_id: str, *, create: bool = True) -> Path:
        """Where :meth:`materialize` writes verified bytes for a worker's file tools."""
        return self._run_dir(run_id, "outbox", create=create)

    def wait_dir(self, run_id: str, *, create: bool = True) -> Path:
        """Where a running ``gx wait`` of the run leaves its record, for a host's stop gate
        (:mod:`gx_core.orchestration.wait`). Files beside the store, never rows in it."""
        return self._run_dir(run_id, "wait", create=create)

    # ------------------------------------------------------------------ path safety

    def _check_namespace(self, namespace: str) -> str:
        if not isinstance(namespace, str) or not namespace.strip():
            raise PathTraversalDenied(
                "namespace must be a non-empty string", details={"namespace": namespace}
            )
        if "\x00" in namespace:
            raise PathTraversalDenied("namespace contains a NUL byte", details={})
        if namespace.startswith("/") or (len(namespace) > 1 and namespace[1] == ":"):
            raise PathTraversalDenied(
                f"namespace must be relative, got {namespace!r}",
                details={"namespace": namespace},
            )
        parts = [part for part in namespace.split("/") if part != ""]
        if not parts:
            raise PathTraversalDenied(
                f"namespace {namespace!r} has no usable components",
                details={"namespace": namespace},
            )
        for part in parts:
            if part in (".", "..") or not _SAFE_COMPONENT.fullmatch(part):
                raise PathTraversalDenied(
                    f"namespace component {part!r} is not allowed; components match "
                    f"{_SAFE_COMPONENT.pattern} and never '.' or '..'",
                    details={"namespace": namespace, "component": part},
                )
        if parts[0] == STAGING_DIRNAME:
            raise PathTraversalDenied(
                f"{STAGING_DIRNAME!r} is the staging area, not a namespace",
                details={"namespace": namespace},
            )
        return "/".join(parts)

    def namespace_root(self, namespace: str, *, create: bool = False) -> Path:
        canonical = self._check_namespace(namespace)
        path = self.root.joinpath(*canonical.split("/"))
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_path(self, namespace: str, rel_path: str) -> Path:
        """Absolute path for ``rel_path`` inside ``namespace``, or a refusal.

        Refused: absolute paths, any ``..`` component, NUL bytes, and anything whose
        resolved location falls outside the namespace -- which is how a symlink
        pointing at another namespace or at ``/etc`` gets caught, since
        ``Path.resolve`` follows the links that already exist.
        """
        ns_root = self.namespace_root(namespace)
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise PathTraversalDenied(
                "rel_path must be a non-empty string",
                details={"namespace": namespace, "rel_path": rel_path},
            )
        if "\x00" in rel_path:
            raise PathTraversalDenied(
                "rel_path contains a NUL byte", details={"namespace": namespace}
            )
        if os.path.isabs(rel_path) or rel_path.startswith("/") or rel_path.startswith("\\"):
            raise PathTraversalDenied(
                f"rel_path must be relative to the namespace, got {rel_path!r}; "
                "artifacts are addressed by reference, never by filesystem path",
                details={"namespace": namespace, "rel_path": rel_path},
            )
        parts = [part for part in rel_path.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts:
            raise PathTraversalDenied(
                f"rel_path {rel_path!r} names no file",
                details={"namespace": namespace, "rel_path": rel_path},
            )
        for part in parts:
            if part == "..":
                raise PathTraversalDenied(
                    f"rel_path {rel_path!r} contains '..'; a traversal is refused even "
                    "when it would happen to land inside the namespace",
                    details={"namespace": namespace, "rel_path": rel_path},
                )
            if "\x00" in part:
                raise PathTraversalDenied("rel_path component contains a NUL byte", details={})

        candidate = ns_root.joinpath(*parts)
        resolved = Path(os.path.realpath(candidate))
        ns_resolved = Path(os.path.realpath(ns_root))
        if resolved != ns_resolved and ns_resolved not in resolved.parents:
            raise PathTraversalDenied(
                f"{rel_path!r} resolves to {resolved}, outside namespace "
                f"{namespace!r} at {ns_resolved}; a symlink does not widen a namespace",
                details={
                    "namespace": namespace, "rel_path": rel_path,
                    "resolved": str(resolved), "namespace_root": str(ns_resolved),
                },
            )
        return candidate

    # --------------------------------------------------------------------- staging

    def staging_root(self) -> Path:
        path = self.root / STAGING_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def staging_dir(self, namespace: str) -> Path:
        """A fresh private directory to write into. Nothing here is an artifact yet."""
        canonical = self._check_namespace(namespace)
        parent = self.staging_root().joinpath(*canonical.split("/"))
        parent.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="stg_", dir=str(parent)))

    def _require_staged(self, staged_path: str | Path) -> Path:
        path = Path(staged_path)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        staging = Path(os.path.realpath(self.staging_root()))
        try:
            real = Path(os.path.realpath(path))
        except OSError as exc:  # pragma: no cover - realpath on a weird fs
            raise PathTraversalDenied(
                f"staged path could not be resolved: {exc}", details={"path": str(path)}
            ) from exc
        if staging not in real.parents:
            raise PathTraversalDenied(
                f"{path} is not inside the staging area; publish moves a staged file "
                "into place, it does not import arbitrary filesystem paths",
                details={"path": str(path), "staging_root": str(staging)},
            )
        if path.is_symlink():
            raise PathTraversalDenied(
                f"staged path {path} is a symlink; publishing would register bytes that "
                "live somewhere else",
                details={"path": str(path)},
            )
        if not path.is_file():
            raise ArtifactNotFound(
                f"staged file {path} does not exist", details={"path": str(path)}
            )
        return path

    # --------------------------------------------------------------------- publish

    def publish(
        self,
        *,
        namespace: str,
        rel_path: str,
        kind: str,
        staged_path: str | Path,
        run_id: str | None = None,
        path_id: str | None = None,
        task_id: str | None = None,
        job_id: str | None = None,
        media_type: str | None = None,
        schema_ref: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        expected_sha256: str | None = None,
        validator: Callable[[Path], Any] | None = None,
        actor: str = "backend",
        origin: str | None = "BACKEND",
        put_by: str | None = None,
    ) -> PublishedArtifact:
        """staging -> hash -> validate -> atomic replace -> register. In that order.

        ``expected_sha256`` is the writer's own statement of what it meant to write.
        A short write produces different bytes, so the mismatch is caught here rather
        than by whoever reads the file next week.

        ``validator`` runs against the *staged* file. If it raises, nothing is moved
        and nothing is registered: a file that failed validation never acquires the
        name that would make it look finished.

        ``origin`` says who produced the bytes (``labels.artifact_origin``). It is
        ``BACKEND`` unless the caller says otherwise, and a caller that is passing on
        content a host handed in says ``HOST_PUT``: such content is a claim until a
        validator has judged it.
        """
        if origin is not None:
            contracts.require_label("artifact_origin", origin)
        target = self.resolve_path(namespace, rel_path)
        # This subtree also holds unregistered originals. Regular publishers
        # must respect its ownership even before finish installs the first row.
        try:
            parts = target.resolve().relative_to(self.root).parts
        except ValueError as exc:
            raise PathTraversalDenied("artifact target resolves outside its store") from exc
        if len(parts) >= 4 and parts[1] == "deliveries" and re.fullmatch(r"[0-9a-f]{64}", parts[3]):
            raise SchemaInvalid("a prepared delivery directory is immutable")
        staged = self._require_staged(staged_path)

        sha256 = ids.sha256_of_file(staged)
        size = staged.stat().st_size
        if expected_sha256 and expected_sha256 != sha256:
            raise ArtifactHashMismatch(
                f"staged file hashes to {sha256[:16]}..., writer expected "
                f"{expected_sha256[:16]}...; the bytes are not what was intended",
                details={
                    "namespace": namespace, "rel_path": rel_path,
                    "observed_sha256": sha256, "expected_sha256": expected_sha256,
                    "bytes": size,
                },
            )
        if validator is not None:
            outcome = validator(staged)
            if outcome is False:
                raise SchemaInvalid(
                    f"validator refused staged artifact {namespace}/{rel_path}",
                    details={"namespace": namespace, "rel_path": rel_path,
                             "sha256": sha256},
                )

        with staged.open("rb") as original:
            os.fsync(original.fileno())
        target.parent.mkdir(parents=True, exist_ok=True)
        # Make new parent directory links durable before taking the writer.
        sync_directory_chain(target.parent.parent, self.base)
        # Compare, install and register under one writer. A concurrent publisher of
        # different bytes must never replace a file another writer already registered.
        with store.transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM artifacts WHERE namespace=? AND rel_path=?", (namespace, rel_path)
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != sha256:
                    raise SchemaInvalid(f"{namespace}/{rel_path} already has a different immutable hash")
                if not target.is_file():
                    raise ArtifactNotFound("the previously published file is missing",
                                           details={"artifact_ref": existing["artifact_ref"]})
                staged.unlink(missing_ok=True)
                return PublishedArtifact(
                    artifact_ref=existing["artifact_ref"], namespace=namespace, rel_path=rel_path,
                    kind=existing["kind"], sha256=sha256, bytes=int(existing["bytes"]),
                    media_type=existing["media_type"], path=str(target), created_at=existing["created_at"],
                    reused=True,
                )
            os.replace(staged, target)
            sync_directory_chain(target.parent, target.parent)
            published = PublishedArtifact(
                artifact_ref=ids.new_id("artifact"), namespace=namespace, rel_path=rel_path,
                kind=kind, sha256=sha256, bytes=size, media_type=media_type, path=str(target),
                created_at=clock.now_iso(),
            )
            try:
                self.register_materialized(published, run_id=run_id, path_id=path_id, task_id=task_id,
                                           job_id=job_id, schema_ref=schema_ref, provenance=provenance,
                                           origin=origin, actor=actor, put_by=put_by)
            except Exception:
                # Still inside the writer, so this cannot unlink a concurrent winner.
                target.unlink(missing_ok=True)
                raise
        return published

    def register_materialized(
        self, artifact: PublishedArtifact, *, run_id: str | None, task_id: str | None,
        actor: str, origin: str | None, provenance: Mapping[str, Any] | None = None,
        path_id: str | None = None, job_id: str | None = None, schema_ref: str | None = None,
        put_by: str | None = None,
    ) -> None:
        """Register already prepared immutable bytes in the caller's transaction."""
        if not self.conn.in_transaction:
            raise RuntimeError("artifact registration requires a writer transaction")
        self.conn.execute(
            "INSERT INTO artifacts(artifact_ref, run_id, path_id, task_id, job_id,"
            " kind, namespace, rel_path, media_type, bytes, sha256, schema_ref,"
            " origin, put_by, provenance_json, published, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
            (artifact.artifact_ref, run_id, path_id, task_id, job_id, artifact.kind,
             artifact.namespace, artifact.rel_path, artifact.media_type, artifact.bytes,
             artifact.sha256, schema_ref, origin, put_by or actor, _j(dict(provenance or {})),
             artifact.created_at),
        )
        store.append_event(
            self.conn, kind="artifact.published", actor=actor, run_id=run_id,
            subject_kind="artifact", subject_id=artifact.artifact_ref, evidence_level="EXECUTION_OBSERVED",
            payload={"artifact_ref": artifact.artifact_ref, "namespace": artifact.namespace,
                     "rel_path": artifact.rel_path, "kind": artifact.kind, "sha256": artifact.sha256,
                     "bytes": artifact.bytes, "media_type": artifact.media_type, "origin": origin,
                     "task_id": task_id, **({"source_role": provenance["role"]} if origin == "INGEST" and
                                             provenance and provenance.get("role") else {})},
        )

    def discard_on_rollback(self, published: PublishedArtifact) -> None:
        """Enroll new bytes in the enclosing managed submission transaction."""
        if published.reused:
            return
        target = Path(published.path)
        identity = target.stat()

        def discard():
            # An outer rollback releases SQLite's writer. Another writer may already
            # have adopted this immutable path, so reacquire it before checking/removing.
            acquire = not self.conn.in_transaction
            if acquire:
                self.conn.execute("BEGIN IMMEDIATE")
            try:
                registered = self.conn.execute("SELECT 1 FROM artifacts WHERE namespace=? AND rel_path=?",
                                               (published.namespace, published.rel_path)).fetchone()
                if registered is None and target.exists() and os.path.samestat(target.stat(), identity):
                    target.unlink()
                    sync_directory_chain(target.parent, target.parent)
            finally:
                if acquire:
                    self.conn.rollback()

        store.on_transaction_end(self.conn, on_rollback=discard)

    def publish_bytes(
        self,
        *,
        namespace: str,
        rel_path: str,
        kind: str,
        data: bytes | str,
        **kwargs: Any,
    ) -> PublishedArtifact:
        """Same discipline for in-memory content: still staged, hashed, then moved."""
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        staging = self.staging_dir(namespace)
        staged = staging / Path(rel_path).name
        with open(staged, "wb") as fh:
            fh.write(payload)
        try:
            return self.publish(
                namespace=namespace, rel_path=rel_path, kind=kind, staged_path=staged,
                **kwargs,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ------------------------------------------------------------------------- put

    def put(
        self,
        *,
        run_id: str,
        kind: str,
        media_type: str,
        content: str | bytes | None = None,
        inbox_path: str | None = None,
        task_id: str | None = None,
        origin: str,
        put_by: str,
    ) -> PublishedArtifact:
        """How content enters the store from outside: inline, or a file in the run's inbox.

        Exactly one of ``content`` and ``inbox_path``. Inline content is bounded by
        ``envelope.limits.max_inline_put_bytes``; anything larger is placed in
        :meth:`inbox_dir` and named by its path relative to that directory. Either way
        the bytes are staged, hashed and moved into place under a name that contains
        their hash, so putting the same content twice returns the same artifact and two
        different contents can never share a name.

        What is published is content, not a judgement of it. ``origin`` records who
        produced the bytes and a ``HOST_PUT`` artifact is a claim until a validator has
        judged it: the kind given here says what the host says the bytes are.
        """
        contracts.require_label("artifact_origin", origin)
        if (content is None) == (inbox_path is None):
            raise SchemaInvalid(
                "give exactly one of content and inbox_path: inline content, or the path "
                "of a file already placed in the run's inbox",
                details={"content_given": content is not None, "inbox_path_given": inbox_path is not None},
            )
        if not isinstance(kind, str) or not _SAFE_KIND.fullmatch(kind):
            raise SchemaInvalid(
                f"kind {kind!r} is not a lower-case identifier such as 'evidence_bundle'",
                details={"kind": kind, "pattern": _SAFE_KIND.pattern},
            )
        if not isinstance(media_type, str) or "/" not in media_type or len(media_type) > 100:
            raise SchemaInvalid(
                f"media_type {media_type!r} is not a media type such as 'application/json'",
                details={"media_type": media_type},
            )
        run = self.conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ArtifactNotFound(f"no such run {run_id!r}", details={"run_id": run_id})
        if task_id is not None:
            task = self.conn.execute(
                "SELECT run_id FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or task["run_id"] != run_id:
                raise SchemaInvalid(
                    f"task {task_id!r} is not a task of run {run_id}",
                    details={"task_id": task_id, "run_id": run_id},
                )

        namespace = f"{run_id}/put/{task_id}" if task_id else f"{run_id}/put"
        staging = self.staging_dir(namespace)
        staged = staging / "content"
        consumed: Path | None = None
        source_identity = None
        try:
            if content is not None:
                payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
                ceiling = int(contracts.response_limits()["max_inline_put_bytes"])
                if len(payload) > ceiling:
                    raise SchemaInvalid(
                        f"inline content is {len(payload)} bytes and the limit is {ceiling}; "
                        "place the file in the run's inbox and put it by inbox_path",
                        details={"bytes": len(payload), "max_inline_put_bytes": ceiling,
                                 "inbox": str(self.inbox_dir(run_id))},
                    )
                with open(staged, "wb") as fh:
                    fh.write(payload)
            else:
                consumed = self._resolve_inbox(run_id, inbox_path)
                info = consumed.stat()
                source_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                shutil.copyfile(consumed, staged)
            if origin == "HOST_PUT":
                # What a host hands in is screened before it is published: a credential in a
                # bundle would otherwise be kept by hash and served to every later reader. A
                # refused inbox file is left where it was, for its owner to correct.
                matched = credentials_in(staged)
                if matched:
                    consumed = None
                    raise PolicyScreenBlocked(
                        "the content matches credential pattern(s) " + ", ".join(matched)
                        + " of config/limits.json screens and is not taken in; the matched "
                        "text is not repeated here",
                        details={"reason": "credential_screen", "matched": matched,
                                 "screen": "config/limits.json#screens.credentials"},
                    )
            sha256 = ids.sha256_of_file(staged)
            rel_path = f"{kind}/{sha256[:24]}{_EXTENSIONS.get(media_type, '.bin')}"
            published = self.publish(
                namespace=namespace, rel_path=rel_path, kind=kind, staged_path=staged,
                run_id=run_id, task_id=task_id, media_type=media_type,
                provenance={"via": "inline" if content is not None else "inbox",
                            "inbox_path": inbox_path},
                actor=put_by, origin=origin, put_by=put_by,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if consumed is not None:
            def release_inbox() -> None:
                try:
                    info = consumed.stat()
                    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == source_identity:
                        consumed.unlink()
                except OSError:
                    pass

            if self.conn.in_transaction:
                try:
                    store.on_transaction_end(self.conn, on_commit=release_inbox)
                except SchemaInvalid:
                    # A raw SQLite transaction has no observable commit hook. Its
                    # caller retains the inbox copy for recovery.
                    pass
            else:
                release_inbox()
        return published

    def _resolve_inbox(self, run_id: str, inbox_path: Any) -> Path:
        """A regular file inside the run's inbox, or a refusal. Never a path elsewhere."""
        inbox = self.inbox_dir(run_id)
        if not isinstance(inbox_path, str) or not inbox_path.strip() or "\x00" in inbox_path:
            raise PathTraversalDenied(
                "inbox_path must be a non-empty path relative to the run's inbox",
                details={"inbox": str(inbox)},
            )
        if os.path.isabs(inbox_path) or inbox_path.startswith(("~", "\\")):
            raise PathTraversalDenied(
                f"inbox_path {inbox_path!r} is absolute; it is read relative to the run's "
                "inbox and nowhere else",
                details={"inbox_path": inbox_path, "inbox": str(inbox)},
            )
        parts = [part for part in inbox_path.replace("\\", "/").split("/") if part not in ("", ".")]
        if not parts or ".." in parts:
            raise PathTraversalDenied(
                f"inbox_path {inbox_path!r} leaves the inbox or names no file",
                details={"inbox_path": inbox_path, "inbox": str(inbox)},
            )
        candidate = inbox.joinpath(*parts)
        resolved = Path(os.path.realpath(candidate))
        inbox_resolved = Path(os.path.realpath(inbox))
        if inbox_resolved not in resolved.parents:
            raise PathTraversalDenied(
                f"inbox_path {inbox_path!r} resolves to {resolved}, outside the run's inbox; "
                "a symlink does not widen it",
                details={"inbox_path": inbox_path, "resolved": str(resolved),
                         "inbox": str(inbox_resolved)},
            )
        if candidate.is_symlink():
            raise PathTraversalDenied(
                f"inbox_path {inbox_path!r} is a symlink; putting it would publish bytes that "
                "live somewhere else",
                details={"inbox_path": inbox_path},
            )
        if not candidate.is_file():
            raise SchemaInvalid(
                f"there is no file {inbox_path!r} in the run's inbox {inbox}",
                details={"inbox_path": inbox_path, "inbox": str(inbox)},
            )
        return candidate

    # --------------------------------------------------------------- verified reads

    def read_bytes(self, artifact_ref: str, *, max_bytes: int | None = None,
                   expected_sha256: str | None = None) -> bytes:
        """The artifact's bytes, re-hashed against its record before they are returned.

        Every backend reader goes through here (or :meth:`read_json`), so nothing is
        judged, compiled into a brief or handed to a job from bytes that are no longer
        the bytes that were published. An input manifest may supply its own pin for
        comparison with the same observed digest.
        """
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise SchemaInvalid("an internal artifact read bound must be a positive byte count")
        row = self._row(artifact_ref)
        if max_bytes is not None and int(row["bytes"]) > max_bytes:
            raise ArtifactHashMismatch("artifact exceeds the internal bounded read", details={"artifact_ref": artifact_ref})
        if expected_sha256 is not None:
            # A pinned file that grew after publication must not cause a larger allocation.
            max_bytes = int(row["bytes"])
        path = self.resolve_path(row["namespace"], row["rel_path"])
        if not path.is_file():
            if expected_sha256 is not None:
                raise ArtifactHashMismatch(
                    f"pinned input artifact {artifact_ref} is registered but its file is gone",
                    details={"artifact_ref": artifact_ref, "reason": "file_missing",
                             "rel_path": row["rel_path"], "recorded_sha256": row["sha256"],
                             "observed_sha256": None},
                )
            raise ArtifactNotFound(
                f"artifact {artifact_ref} is registered but its file is gone: {path}",
                details={"artifact_ref": artifact_ref, "rel_path": row["rel_path"]},
            )
        if max_bytes is None:
            data = path.read_bytes()
        else:
            # Preserve the original resolver/integrity authority; no whole-file
            # allocation occurs before enforcing this internal transport bound.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                    raise ArtifactHashMismatch("artifact file exceeds the internal bounded read",
                                               details={"artifact_ref": artifact_ref, "recorded_bytes": int(row["bytes"]),
                                                        "observed_bytes": info.st_size})
                data = handle.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ArtifactHashMismatch("artifact grew beyond the internal bounded read")
        observed = hashlib.sha256(data).hexdigest()
        if observed != row["sha256"] or len(data) != int(row["bytes"]):
            raise ArtifactHashMismatch(
                f"artifact {artifact_ref} hashes to {observed[:16]}... but was registered "
                f"as {row['sha256'][:16]}...; treat every downstream conclusion as invalid "
                "until re-derived",
                details={"artifact_ref": artifact_ref, "recorded_sha256": row["sha256"],
                         "observed_sha256": observed, "rel_path": row["rel_path"]},
            )
        if expected_sha256 is not None and observed != expected_sha256:
            raise ArtifactHashMismatch(
                f"input artifact {artifact_ref} differs from its manifest pin",
                details={"artifact_ref": artifact_ref, "recorded_sha256": row["sha256"],
                         "observed_sha256": observed, "expected_sha256": expected_sha256},
            )
        return data

    def copy_verified(self, artifact_ref: str, output: BinaryIO, *, expected_sha256: str) -> None:
        """Stream a pinned publication to a private staging file and verify that same read."""
        row = self._row(artifact_ref)
        if row["sha256"] != expected_sha256:
            raise ArtifactHashMismatch("Selected artifact differs from its pin", details={"artifact_ref": artifact_ref})
        path = self.resolve_path(row["namespace"], row["rel_path"])
        if not path.is_file():
            raise ArtifactNotFound("Selected artifact file is missing", details={"artifact_ref": artifact_ref})
        self._read_slice(path, row, 0, 0, verify=True, copy_to=output)

    def read_json(self, artifact_ref: str) -> Any:
        """The artifact parsed as JSON, from verified bytes."""
        data = self.read_bytes(artifact_ref)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaInvalid(
                f"artifact {artifact_ref} is not JSON: {exc.__class__.__name__}: {exc}",
                details={"artifact_ref": artifact_ref},
            ) from exc

    def describe(self, artifact_ref: str) -> dict[str, Any]:
        """The artifact's record, without touching its bytes."""
        row = self._row(artifact_ref)
        return {
            "artifact_ref": artifact_ref, "run_id": row["run_id"], "path_id": row["path_id"],
            "task_id": row["task_id"], "job_id": row["job_id"], "kind": row["kind"],
            "namespace": row["namespace"], "rel_path": row["rel_path"],
            "media_type": row["media_type"], "bytes": int(row["bytes"]),
            "sha256": row["sha256"], "origin": row["origin"], "put_by": row["put_by"],
            "created_at": row["created_at"],
        }

    def materialize(self, artifact_ref: str, *, workspace: Path | None = None) -> Path:
        """Write verified bytes to the run outbox or the native project's working directory.

        A worker then reads a long brief or a large product with its own file tools,
        instead of through a tool result that is bounded on purpose. The copy is
        written whole or not at all, and an existing copy is checked rather than
        trusted: a file that no longer hashes to the artifact is replaced.
        """
        row = self._row(artifact_ref)
        source = self.resolve_path(row["namespace"], row["rel_path"])
        if not source.is_file():
            raise ArtifactNotFound(f"artifact {artifact_ref} is registered but its file is gone",
                                   details={"artifact_ref": artifact_ref})
        if not _SAFE_COMPONENT.fullmatch(artifact_ref):
            raise PathTraversalDenied("artifact delivery requires a registered artifact identifier")
        filename = f"{artifact_ref}{Path(row['rel_path']).suffix}"
        with self._copy_directory(row["run_id"], workspace) as (directory, directory_fd):
            def open_copy(name, flags):
                return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)

            try:
                with open(filename, "rb", opener=open_copy) as cached:
                    info = os.fstat(cached.fileno())
                    if (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                            and hashlib.file_digest(cached, "sha256").hexdigest() == row["sha256"]
                            and os.path.samestat(info, os.stat(filename, dir_fd=directory_fd, follow_symlinks=False))):
                        self._read_slice(source, row, 0, 0, verify=True)
                        return directory / filename
            except OSError as exc:
                if exc.errno == errno.EISDIR:
                    raise PathTraversalDenied("the artifact copy filename is occupied by a directory") from exc
                if exc.errno not in (errno.ENOENT, errno.ELOOP):
                    raise

            temporary = ".materialize_" + os.urandom(16).hex()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as output:
                    self._read_slice(source, row, 0, 0, verify=True, copy_to=output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        return directory / filename

    @contextmanager
    def _copy_directory(self, run_id: str | None, workspace: Path | None):
        """Keep the copy attached to its original directory while workers share the workspace."""
        flags = os.O_RDONLY | os.O_DIRECTORY
        with ExitStack() as opened:
            if workspace is None:
                directory = self.outbox_dir(run_id or "shared")
                fd = os.open(directory, flags)
            else:
                if workspace != self.base / "workspace" or workspace.resolve() != workspace:
                    raise PathTraversalDenied("artifact delivery requires the project's owned workspace")
                directory = workspace / ".gx-artifacts"
                try:
                    parent = os.open(workspace, flags | os.O_NOFOLLOW)
                    opened.callback(os.close, parent)
                    try:
                        os.mkdir(directory.name, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    fd = os.open(directory.name, flags | os.O_NOFOLLOW, dir_fd=parent)
                except (NotADirectoryError, FileNotFoundError) as exc:
                    raise PathTraversalDenied("the project's artifact-copy directory must be a local directory") from exc
            opened.callback(os.close, fd)
            yield directory, fd
            if workspace is not None:
                try:
                    current = directory.stat(follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise PathTraversalDenied("the artifact-copy directory moved before delivery") from exc
                if directory.resolve() != directory or not os.path.samestat(os.fstat(fd), current):
                    raise PathTraversalDenied("the artifact-copy directory changed before delivery")

    # ------------------------------------------------------------------------ read

    def _row(self, artifact_ref: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE artifact_ref=?", (artifact_ref,)
        ).fetchone()
        if row is None:
            raise ArtifactNotFound(
                f"no artifact {artifact_ref!r}; a missing artifact is not an empty result",
                details={"artifact_ref": artifact_ref},
            )
        return row

    def path_of(self, artifact_ref: str) -> Path:
        row = self._row(artifact_ref)
        return self.resolve_path(row["namespace"], row["rel_path"])

    def verify(self, artifact_ref: str) -> dict[str, Any]:
        """Re-hash the bytes behind a ref. Reports; does not raise on mismatch."""
        row = self._row(artifact_ref)
        path = self.resolve_path(row["namespace"], row["rel_path"])
        record: dict[str, Any] = {
            "artifact_ref": artifact_ref,
            "namespace": row["namespace"],
            "rel_path": row["rel_path"],
            "recorded_sha256": row["sha256"],
            "recorded_bytes": int(row["bytes"]),
            "checked_at": clock.now_iso(),
        }
        if not path.is_file():
            return {**record, "ok": False, "reason": "file_missing", "observed_sha256": None}
        observed = ids.sha256_of_file(path)
        observed_bytes = path.stat().st_size
        ok = observed == row["sha256"] and observed_bytes == int(row["bytes"])
        return {
            **record,
            "ok": ok,
            "observed_sha256": observed,
            "observed_bytes": observed_bytes,
            "reason": None if ok else "hash_mismatch",
        }

    def get(
        self,
        artifact_ref: str,
        *,
        section: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        verify: bool = True,
        max_bytes: int | None = None,
        max_encoded_bytes: int | None = None,
    ) -> dict[str, Any]:
        """``gx_artifact_get``: bounded content by reference, re-verified on read.

        The size ceiling is ``max_bytes``, by default ``envelope.limits.max_response_bytes``
        from ``contracts/mcp_tools.json``; anything larger comes back truncated with the
        flag set and the offset to continue from, never silently cut. Binary artifacts
        are described and not inlined. A caller that wants all of a large artifact uses
        :meth:`materialize` (``gx get <ref> --out``) and reads the file.
        The public tool supplies its encoded page budget to fit escaped text from the same read.
        """
        row = self._row(artifact_ref)
        path = self.resolve_path(row["namespace"], row["rel_path"])
        if max_bytes is None:
            max_bytes = int(contracts.response_limits()["max_response_bytes"])

        if not path.is_file():
            raise ArtifactNotFound(
                f"artifact {artifact_ref} is registered but its file is gone: {path}",
                details={"artifact_ref": artifact_ref, "rel_path": row["rel_path"]},
            )
        metadata = {
            "artifact_ref": artifact_ref,
            "run_id": row["run_id"],
            "path_id": row["path_id"],
            "task_id": row["task_id"],
            "job_id": row["job_id"],
            "kind": row["kind"],
            "namespace": row["namespace"],
            "rel_path": row["rel_path"],
            "media_type": row["media_type"],
            "bytes": int(row["bytes"]),
            "schema_ref": row["schema_ref"],
            "origin": row["origin"],
            "provenance": store.loads(row, "provenance_json", {}),
            "created_at": row["created_at"],
            "verified": bool(verify),
        }

        start = max(0, int(offset or 0))
        if not self._is_textual(row["media_type"], row["rel_path"]):
            if verify:
                self._read_slice(path, row, 0, 0, verify=True)
            return {
                "metadata": {**metadata, "offset": start, "returned_bytes": 0, "next_offset": None},
                "content": None, "truncated": False, "sha256": row["sha256"],
                "note": "binary artifact: fetch it by path from the executor, not inline",
            }

        requested = int(limit) if limit is not None else max_bytes
        window = max(0, min(requested, max_bytes))
        # One extra byte determines truncation and whether a UTF-8 character crosses the edge.
        if section is None:
            raw = self._read_slice(path, row, start, window + 1, verify=verify)
        else:
            document = self._read_slice(path, row, 0, None, verify=verify)
            raw = self._section_bytes(document, row, section)[start:start + window + 1]
            metadata["section"] = section

        while True:
            end = min(window, len(raw))
            while 0 < end < len(raw) and (raw[end] & 0xC0) == 0x80:
                end -= 1
            truncated = end < len(raw)
            if truncated and not end:
                raise SchemaInvalid("the requested byte window cannot fit the next UTF-8 character; increase limit",
                                    details={"artifact_ref": artifact_ref, "offset": start, "limit": window})
            page = {
                "metadata": {**metadata, "offset": start, "returned_bytes": end,
                             "next_offset": start + end if truncated else None},
                "content": raw[:end].decode("utf-8", errors="replace"),
                "truncated": truncated,
                "sha256": row["sha256"],
            }
            if max_encoded_bytes is None or window <= 256:
                return page
            encoded = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) <= max_encoded_bytes:
                return page
            window = max(256, int(window * 0.6))

    @staticmethod
    def _read_slice(path: Path, row: sqlite3.Row, start: int, length: int | None, *,
                    verify: bool, copy_to: BinaryIO | None = None) -> bytes | bytearray:
        """Hash the full artifact and keep the requested bytes from that same read."""
        with path.open("rb") as stream:
            if not verify:
                size = os.fstat(stream.fileno()).st_size
                if start >= size:
                    return b""
                stream.seek(start)
                return stream.read(-1 if length is None else min(length, size - start))
            digest = hashlib.sha256()
            data = bytearray()
            position = 0
            stop = int(row["bytes"]) if length is None else start + length
            for block in iter(lambda: stream.read(1 << 20), b""):
                if copy_to is not None:
                    copy_to.write(block)
                if position < stop and position + len(block) > start:
                    data.extend(block[max(0, start - position):stop - position])
                digest.update(block)
                position += len(block)
        observed = digest.hexdigest()
        if observed != row["sha256"] or position != int(row["bytes"]):
            raise ArtifactHashMismatch(
                f"artifact {row['artifact_ref']} differs from its registered bytes",
                details={"artifact_ref": row["artifact_ref"], "recorded_sha256": row["sha256"],
                         "observed_sha256": observed, "recorded_bytes": int(row["bytes"]),
                         "observed_bytes": position, "rel_path": row["rel_path"]},
            )
        return data

    @staticmethod
    def _is_textual(media_type: str | None, rel_path: str) -> bool:
        if media_type:
            media_type = media_type.partition(";")[0].strip().lower()
            if media_type in _TEXT_MEDIA_TYPES:
                return True
            if any(media_type.startswith(prefix) for prefix in _TEXT_MEDIA_PREFIXES):
                return True
            return False
        return Path(rel_path).suffix.lower() in {
            ".txt", ".json", ".jsonl", ".ndjson", ".md", ".csv", ".tsv", ".log",
            ".fasta", ".fa", ".ct", ".db", ".dbn", ".yaml", ".yml", ".xml", ".err", ".out",
            ".pdb", ".cif", ".mmcif",
        }

    @staticmethod
    def _section_bytes(raw: bytes | bytearray, row: sqlite3.Row, section: str) -> bytes:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaInvalid(
                f"section={section!r} needs a JSON artifact; {row['rel_path']} is not "
                f"parseable JSON ({exc.__class__.__name__})",
                details={"artifact_ref": row["artifact_ref"], "section": section},
            ) from exc
        if not isinstance(document, dict) or section not in document:
            raise SchemaInvalid(
                f"artifact {row['artifact_ref']} has no top-level section {section!r}",
                details={
                    "section": section,
                    "available": sorted(document) if isinstance(document, dict) else None,
                },
            )
        return ids.canonical_json(document[section]).encode("utf-8")

    # --------------------------------------------------------------- input manifest

    def make_input_manifest(
        self,
        *,
        namespace: str,
        entries: Sequence[Mapping[str, Any] | str],
        rel_path: str | None = None,
        run_id: str | None = None,
        path_id: str | None = None,
        task_id: str | None = None,
        actor: str = "backend",
        extra: Mapping[str, Any] | None = None,
    ) -> PublishedArtifact:
        """Freeze the exact inputs of a job as an artifact of kind ``input_manifest``.

        Every entry is verified *now*: a manifest listing a ref whose bytes already
        moved would hand a job a guarantee nobody checked. The refs and their hashes
        go into the manifest, so a later reconciliation can ask whether the inputs are
        still the inputs.

        A manifest is a statement of content and says nothing about when it was made, so
        making it again for the same inputs, run, path and task is the same bytes under
        the same name, and returns the artifact that is already there. (It carried a
        timestamp once: the second call for the same job then had another hash under the
        same name and was refused.) When one was made is on its artifact row.
        """
        resolved: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if isinstance(entry, str):
                role, ref = f"input_{index}", entry
            else:
                ref = entry.get("artifact_ref")
                role = str(entry.get("role") or f"input_{index}")
                if not ref:
                    raise SchemaInvalid(
                        f"manifest entry {index} has no artifact_ref",
                        details={"index": index},
                    )
            check = self.verify(str(ref))
            if not check["ok"]:
                raise ArtifactHashMismatch(
                    f"input {role!r} ({ref}) failed verification: {check['reason']}",
                    details=check,
                )
            row = self._row(str(ref))
            resolved.append({
                "role": role,
                "artifact_ref": str(ref),
                "sha256": row["sha256"],
                "bytes": int(row["bytes"]),
                "kind": row["kind"],
                "media_type": row["media_type"],
                "rel_path": row["rel_path"],
                "namespace": row["namespace"],
            })

        document = {
            "schema": "gx.input_manifest/1",
            "namespace": namespace,
            "run_id": run_id,
            "path_id": path_id,
            "task_id": task_id,
            "entries": resolved,
            "entry_count": len(resolved),
            "contract_sha256": contracts.contract_digest()["__combined__"],
            **dict(extra or {}),
        }
        name = rel_path or f"manifests/input_{ids.sha256_of_obj(document)[:16]}.json"
        return self.publish_bytes(
            namespace=namespace, rel_path=name, kind="input_manifest",
            data=ids.canonical_json(document), media_type="application/json",
            run_id=run_id, path_id=path_id, task_id=task_id, actor=actor,
            provenance={"entries": [item["artifact_ref"] for item in resolved]},
        )

    def read_input_manifest(self, manifest_ref: str, *, verify_entries: bool = False) -> dict[str, Any]:
        """Read the manifest and, when requested, verify the inputs it names."""
        row = self._row(manifest_ref)
        if row["kind"] != "input_manifest":
            raise SchemaInvalid(
                f"{manifest_ref} is kind {row['kind']!r}, not 'input_manifest'",
                details={"artifact_ref": manifest_ref, "kind": row["kind"]},
            )
        max_bytes = int(contracts.response_limits()["max_response_bytes"])
        if int(row["bytes"]) > max_bytes:
            # Reading only part of a manifest and calling the job's inputs verified
            # would be a verification of a prefix.
            raise SchemaInvalid(
                f"input manifest {manifest_ref} exceeds the response limit and cannot "
                "be verified from a truncated read",
                details={"artifact_ref": manifest_ref, "bytes": int(row["bytes"])},
            )
        raw = self.read_bytes(manifest_ref, max_bytes=max_bytes)
        document = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(document, dict):
            document.setdefault("created_at", row["created_at"])
        if verify_entries:
            self._verify_input_entries(manifest_ref, document.get("entries", []))
        return document

    def _verify_input_entries(self, manifest_ref: str, entries: Sequence[Mapping[str, Any]]) -> None:
        for entry in entries:
            ref = entry.get("artifact_ref")
            result = self.verify(str(ref))
            if not result["ok"]:
                raise ArtifactHashMismatch(
                    f"input {entry.get('role')!r} ({ref}) listed in {manifest_ref} no "
                    f"longer matches its recorded hash: {result['reason']}",
                    details={**result, "manifest_ref": manifest_ref},
                )
            if entry.get("sha256") and entry["sha256"] != result["observed_sha256"]:
                raise ArtifactHashMismatch(
                    f"input {entry.get('role')!r} ({ref}) hashes to "
                    f"{result['observed_sha256'][:16]}... but the manifest recorded "
                    f"{entry['sha256'][:16]}...",
                    details={**result, "manifest_ref": manifest_ref},
                )

    def verify_input_manifest(self, manifest_ref: str) -> dict[str, Any]:
        """Re-verify every input a manifest claims. Raises on the first mismatch."""
        document = self.read_input_manifest(manifest_ref, verify_entries=True)
        checked = [{"role": entry.get("role"), "artifact_ref": entry.get("artifact_ref"), "ok": True}
                   for entry in document.get("entries", [])]
        return {
            "manifest_ref": manifest_ref,
            "entry_count": len(checked),
            "entries": checked,
            "roles": [entry["role"] for entry in checked],
            "verified_at": clock.now_iso(),
        }

    # ----------------------------------------------------------------------- listing

    def list_artifacts(
        self,
        *,
        namespace: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        max_rows = int(contracts.response_limits()["max_rows"])
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("namespace", namespace), ("run_id", run_id), ("job_id", job_id), ("kind", kind)
        ):
            if value is not None:
                where.append(f"{column}=?")
                params.append(value)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(max(1, min(int(limit), max_rows)))
        rows = self.conn.execute(
            f"SELECT * FROM artifacts{clause} ORDER BY created_at, artifact_ref LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "artifact_ref": row["artifact_ref"], "kind": row["kind"],
                "namespace": row["namespace"], "rel_path": row["rel_path"],
                "bytes": int(row["bytes"]), "sha256": row["sha256"],
                "media_type": row["media_type"], "created_at": row["created_at"],
                "job_id": row["job_id"], "run_id": row["run_id"],
            }
            for row in rows
        ]


__all__ = ["ArtifactService", "PublishedArtifact", "STAGING_DIRNAME", "GxError"]
