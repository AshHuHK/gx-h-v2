"""Writing an ACTIVE asset's host files, and saying what the files of a role intent are.

``contracts/gates.json`` gives two transitions the effect ``project_to_hosts``: a decision
skill and a role intent that enter ACTIVE are written where a host loads them. v1 had no
such step, so an asset could be ACTIVE and no session anywhere read it.

The division of work:

* a **projector** per kind says what the files are (``gx_core.skills.projection`` for
  ``DECISION_SKILL``; :func:`role_files` here for ``ROLE_INTENT``);
* the **effect** (:func:`project_to_hosts`, every kind) calls the projector, writes each
  file and returns what it wrote: ``{"written": [{"host", "path", "abs_path", "sha256",
  "installed"}], "skipped": [{"host", "reason"}]}``. The promotion stores that return, and
  the ``projection_parity`` check later re-reads each ``abs_path``, so "installed" is read
  from a record and compared with bytes, never assumed.

**Where a file goes.** Each host has one root. If the host adapter was installed into a
project (``adapters/hosts/install.py`` records the project in ``hosts/install.json`` beside
the store), the root is that project, and an ACTIVE skill is in the session's skill
directory the moment it is promoted. Otherwise the root is ``hosts/staged/<host>/`` beside
the store: the files exist, are verified, and wait there for an installation. Nothing is
ever written into a project nobody installed into.

**What may be written.** Only under the directories a host loads skills and roles from,
only under names this backend owns (``gxd-…`` for skills, ``gx-…``/``gx_…`` for roles), and
never over a file that does not say it was generated from the same asset id. A file
somebody else put there stops the promotion; it is not replaced.

**Leaving ACTIVE** removes what the effect wrote, if the file still carries that version's
stamp. When a new version replaced the file in the same transaction the stamp is the new
one's and the file stays.

**Failure and recovery.** Every target is preflighted before the first file changes. A
checksummed undo journal is fsynced before publication; a database event in the asset's
transaction identifies a committed publication. Transaction completion hooks compensate
in reverse order on savepoint/outer rollback, including COMMIT failure and a later effect
or submission failure. The same mechanism restores a withdrawal. Normal store opening
after providers load recovers an interrupted process before serving the store. A corrupt
journal or a file changed outside the publication is retained and fails closed with
OUTCOME_UNKNOWN; another person's bytes are never overwritten by compensation. Filesystem
readers can observe an in-flight group before its SQL commit; this is recoverable local
publication, not a simultaneous multi-file filesystem commit.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .. import contracts
from ..context import SCOPE_OPERATOR, Ctx
from ..errors import (GxError, ImplementationGap, OutcomeUnknown, PathTraversalDenied,
                      ProjectionTargetOccupied, SchemaInvalid)
from ..governance import assets, effects, manifests, subjects
from ..governance.effects import effect, on_transition, projector
from ..persistence import store
from ..registry import BundleVerdict, RecordVerdict, bundle_validator
from . import policy, roles, codex_roles

EFFECT = "project_to_hosts"
ACTIVE = assets.ACTIVE
HOSTS_DIR = "hosts"
INSTALL_STATE = "install.json"
STAGED_DIR = "staged"
#: host -> the directories of its project tree a projection may write under.
PROJECTION_DIRS: dict[str, tuple[str, ...]] = {
    "claude_code": (".claude/skills", ".claude/agents"),
    "codex": (".agents/skills", ".codex/agents"),
}
for _host in PROJECTION_DIRS:
    contracts.require_label("host_kind", _host)
#: The first name under such a directory must begin with one of these: they are this backend's.
OWN_PREFIXES = ("gxd-", "gx-", "gx_")
#: The kinds whose files this module removes when a version leaves ACTIVE.
PROJECTED_KINDS = tuple(contracts.check_spec("projection_parity")["applies_to"])

# Until govern routes ROLE_INTENT to the contract's role_bundle (its r5 KIND_BUNDLE), this declaration is
# what describes a role intent's content; from then on governance records it as superseded and reads the
# contract's table, which is the same one (the tests check that the two are equal).
manifests.declare(roles.KIND, fields=roles.FIELDS, manifest_fields=roles.MANIFEST_FIELDS, owner=__name__)

#: The bundle a role intent is handed in as (contract r5, ``bundles.json#role_bundle``).
ROLE_BUNDLE = "role_bundle"
contracts.bundle_spec(ROLE_BUNDLE)


def _role_refusal(message: str, *, stage: str, code: str = "SCHEMA_INVALID") -> RecordVerdict:
    return RecordVerdict(index=-1, ok=False, code=code, message=message, details={"stage": stage})


def _supersedes_problem(conn: sqlite3.Connection, manifest: Mapping[str, Any]) -> str | None:
    """Why ``supersedes`` names nothing it may name; None when it is absent or names a registered role intent.

    It counts only when it names another registered version of a role intent and, where it
    pins a sha256, those bytes (as governance reads it since 2026-11-12T22:48:52Z): a
    reference to nothing, to another kind, to other bytes or to the version itself is refused
    here, at intake, rather than read later as "retires nothing".
    """
    ref = manifest.get("supersedes")
    if not ref:
        return None
    if not isinstance(ref, Mapping):
        return "supersedes is an asset reference"
    asset_id, version = ref.get("asset_id") or manifest.get("role_id"), ref.get("version")
    if asset_id == manifest.get("role_id") and str(version) == str(manifest.get("version")):
        return "supersedes names this very version"
    row = conn.execute("SELECT kind, content_sha256 FROM assets WHERE asset_id=? AND version=?",
                       (asset_id, version)).fetchone()
    if row is None or row["kind"] != roles.KIND:
        return f"supersedes names {asset_id}@{version}, which is not a registered role intent"
    if ref.get("sha256") and ref.get("sha256") != row["content_sha256"]:
        return (f"supersedes pins {asset_id}@{version} at sha256 {str(ref.get('sha256'))[:16]}… and it is "
                f"registered at {str(row['content_sha256'])[:16]}…")
    return None


@bundle_validator(ROLE_BUNDLE)
def judge_role_intent(ctx: Ctx, task: Mapping[str, Any] | None, bundle: dict, *, commit: bool) -> BundleVerdict:
    """Judge a role intent handed in as a bundle and, when committing an accepted one, register it.

    A role intent is one object: it is registered whole or not at all. Refused here is what
    would make registering it pointless, because no host could ever be given the role file it
    describes: an id that is not its role's, a class other than the policy gives the role, a
    tool list that lets a worker reach the network around the proxy or leaves out the shell,
    a title or description that would break out of a role file (``roles.problems_in``), the
    version the tree's own shipped intents carry, and a ``supersedes`` that names nothing.
    The gate's checks (``manifest_schema``, ``byte_binding``, then ``projection_parity``) run
    on the registered version as for every kind; projection still asks ``problems_in`` again
    when it writes the files, because the policy may have moved in between.
    """
    task = dict(task or {})
    conn = ctx.conn
    records: list[RecordVerdict] = []
    dropped = int(task.get("refused_on_shape") or 0)
    if dropped:
        records.append(_role_refusal(
            f"{dropped} record(s) of this bundle were refused on shape; a role intent is registered as one "
            "object, so it is not registered with part of it missing", stage="shape"))
    manifest = bundle.get("manifest") if isinstance(bundle, Mapping) else None
    if not isinstance(manifest, Mapping):
        records.append(_role_refusal("a role intent is an object with a manifest", stage="manifest"))
    else:
        try:
            assets._identify(roles.KIND, manifest)
        except GxError as exc:
            records.append(_role_refusal(exc.message, stage="manifest", code=exc.code))
        if str(manifest.get("version")) in (roles.VERSION_SHIPPED, codex_roles.VERSION):
            records.append(_role_refusal(
                f"version {str(manifest.get('version'))!r} names the intents this tree ships; an intent handed in has a "
                "version of its own", stage="manifest"))
        superseded = _supersedes_problem(conn, manifest)
        if superseded:
            records.append(_role_refusal(superseded, stage="manifest"))
        records += [_role_refusal(problem, stage="role")
                    for problem in roles.problems_in(bundle, policy.agent_policy()[0])]
    accepted = not records
    created: dict[str, list[str]] = {}
    if not accepted:
        summary = f"{len(records)} thing(s) keep this role intent from being registered"
    elif not commit:
        summary = "well formed; the role's file could be written for every host it names"
        records.append(RecordVerdict(index=-1, ok=True, message=summary))
    else:
        content_ref = assets.publish_content(ctx, bundle)
        out = assets.submit_asset(
            ctx, kind=roles.KIND, manifest=manifest, content_ref=content_ref,
            origin="AUTHORED" if ctx.principal.scope == SCOPE_OPERATOR else "GENERATED",
            run_id=task.get("run_id"), task_id=task.get("task_id"),
        )
        ref = out["asset_ref"]
        created["assets"] = [f"{ref['asset_id']}@{ref['version']}"]
        summary = f"registered {ref['asset_id']}@{ref['version']} as {out['status']} at sha256 {ref['sha256'][:16]}…"
        records.append(RecordVerdict(index=-1, ok=True, message=summary, ref=created["assets"][0],
                                     details={"asset_ref": ref, "status": out["status"],
                                              "already_registered": not out["created"]}))
    return BundleVerdict(bundle_kind=ROLE_BUNDLE, records=records, accepted=accepted, created=created, summary=summary)


# -------------------------------------------------------------------------------- roots

def hosts_dir(conn: sqlite3.Connection) -> Path:
    """Where this store keeps what it knows about hosts: beside the database, like its artifacts."""
    return store.data_root(conn) / HOSTS_DIR


def install_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the installer recorded: ``{"hosts": {host: {"root": <project>, ...}}}``; empty when nothing is installed."""
    try:
        document = json.loads((hosts_dir(conn) / INSTALL_STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) and isinstance(document.get("hosts"), dict) else {}


def host_root(conn: sqlite3.Connection, host: str) -> tuple[Path, bool]:
    """``(root, installed)``: the project the host adapter was installed into, else the staging tree."""
    entry = (install_state(conn).get("hosts") or {}).get(host)
    if isinstance(entry, Mapping) and entry.get("projections", True):
        root = Path(str(entry.get("root") or ""))
        if root.is_absolute() and root.is_dir():
            return root.resolve(), True
    return (hosts_dir(conn) / STAGED_DIR / host).resolve(), False


def target_of(root: Path, host: str, path: Any) -> Path:
    """The file a projection entry names, or a refusal if it is not this backend's to write."""
    details = {"host": host, "path": str(path)[:200]}
    if host not in PROJECTION_DIRS:
        raise PathTraversalDenied(f"no host tree is defined for {host!r}", details=details)
    if not isinstance(path, str) or not path or path.startswith(("/", "~")) or "\\" in path or "\0" in path:
        raise PathTraversalDenied("a projection names a relative path inside the host's tree", details=details)
    parts = PurePosixPath(path).parts
    if ".." in parts or "." in parts:
        raise PathTraversalDenied("a projection path may not climb out of the host's tree", details=details)
    home = next((prefix for prefix in PROJECTION_DIRS[host]
                 if parts[:len(PurePosixPath(prefix).parts)] == PurePosixPath(prefix).parts), None)
    rest = parts[len(PurePosixPath(home).parts):] if home else ()
    if home is None or not rest or not rest[0].startswith(OWN_PREFIXES):
        raise PathTraversalDenied(
            f"a projection for {host} is written under {' or '.join(PROJECTION_DIRS[host])}, in a name "
            f"beginning with {' or '.join(OWN_PREFIXES)}; {path!r} is not", details=details)
    target = root.joinpath(*parts)
    resolved_parent = target.parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise PathTraversalDenied("the projection path leaves the host's tree through a link", details=details)
    if target.is_symlink():
        raise PathTraversalDenied("the projection target is a link", details=details)
    return target


def _preflight(root: Path, host: str, path: str, content: bytes, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect a target without changing it. A whole publication is inspected first."""
    target = target_of(root, host, path)
    digest = hashlib.sha256(content).hexdigest()
    changed = True
    existing = None
    if target.exists():
        if not target.is_file():
            raise ProjectionTargetOccupied(
                f"{target} exists and is not a file; a projection never replaces what it did not write",
                details={"host": host, "path": path, "abs_path": str(target)})
        existing = target.read_bytes()
        if existing == content:
            changed = False
        else:
            owner = roles.read_stamp(existing.decode("utf-8", "replace"))["gx_asset_id"]
            if owner != asset["asset_id"]:
                raise ProjectionTargetOccupied(
                    f"{target} exists and was not generated from {asset['asset_id']} "
                    f"({'it names ' + owner if owner else 'it names no asset'}); a projection never "
                    "replaces a file it did not write. Move the file away or give the asset another id",
                    details={"host": host, "path": path, "abs_path": str(target), "names": owner,
                             "asset_id": asset["asset_id"]})
    for parent in target.parents:
        if parent.exists() and not parent.is_dir():
            raise ProjectionTargetOccupied("a projection's parent exists and is not a directory",
                                           details={"host": host, "path": path, "abs_path": str(parent)})
        if parent == root:
            break
    return {"host": host, "path": path, "abs_path": str(target), "sha256": digest, "changed": changed,
            "root": str(root), "before": existing,
            "before_mode": target.stat().st_mode & 0o777 if existing is not None else None,
            "after": content}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_bytes(target: Path, content: bytes, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, scratch = tempfile.mkstemp(prefix=f".{target.name}.gx-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(scratch, target)
        _fsync_directory(target.parent)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def _public_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("host", "path", "abs_path", "sha256", "changed")}


def write_projection(root: Path, host: str, path: str, content: bytes, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Write a single file. Asset lifecycle effects use the durable group publisher below."""
    entry = _preflight(root, host, path, content, asset)
    if entry["changed"]:
        _replace_bytes(Path(entry["abs_path"]), content, entry["before_mode"] or 0o644)
    return _public_entry(entry)


# The database remains authority. Each journal is durable *before* any host file changes.
# Its event is committed in the same transaction as the asset/effects. A restart can thus
# distinguish an interrupted publication from a committed one without guessing from files.
PUBLICATION_EVENT = "asset.projection_publication"
JOURNAL_DIR = "projection-journal"
_IN_FLIGHT: set[str] = set()


def _journal_dir(conn: sqlite3.Connection) -> Path:
    directory = hosts_dir(conn) / JOURNAL_DIR
    if directory.is_symlink() or hosts_dir(conn).is_symlink():
        raise PathTraversalDenied("the projection journal directory is a link")
    return directory


def _encoded(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode("ascii") if data is not None else None


def _journal_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema") != 1 or not isinstance(payload.get("entries"), list) or not payload.get("publication_id"):
        raise ValueError("invalid projection journal schema")
    entries = []
    for raw in payload["entries"]:
        entry = dict(raw)
        root = Path(entry["root"])
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("the journal's original root moved through a link")
        target = target_of(root, entry["host"], entry["path"])
        if str(target) != entry["abs_path"]:
            raise ValueError("journal target differs from its host-relative path")
        for key in ("before", "after"):
            entry[key] = base64.b64decode(entry[key], validate=True) if entry[key] is not None else None
            if entry[key] is not None and hashlib.sha256(entry[key]).hexdigest() != entry[key + "_sha256"]:
                raise ValueError("journal content hash mismatch")
        mode = entry.get("before_mode")
        if mode is not None and (not isinstance(mode, int) or not 0 <= mode <= 0o777):
            raise ValueError("invalid previous file mode")
        entries.append(entry)
    if len({entry["abs_path"] for entry in entries}) != len(entries):
        raise ValueError("duplicate journal target")
    return entries


def _remove_journal(journal: Path) -> None:
    journal.unlink(missing_ok=True)
    _fsync_directory(journal.parent)


def _restore_journal(journal: Path, payload: Mapping[str, Any]) -> None:
    entries = _journal_entries(payload)  # validate the *entire* record before changing a file
    failures = []
    for entry in reversed(entries):
        try:
            target = target_of(Path(entry["root"]), entry["host"], entry["path"])
            if target.exists() and not target.is_file():
                raise ValueError("target is no longer a file")
            current = target.read_bytes() if target.exists() else None
            if current == entry["before"]:
                continue
            if current != entry["after"]:
                raise ValueError("target changed outside this publication; it was left untouched")
            if entry["before"] is None:
                target.unlink()
                _fsync_directory(target.parent)
            else:
                _replace_bytes(target, entry["before"], entry.get("before_mode"))
        except Exception as exc:
            failures.append({"path": entry["abs_path"], "error": str(exc)[:500]})
    if failures:
        raise OutcomeUnknown("projection compensation needs reconciliation; external changes were not overwritten",
                             details={"journal": str(journal), "failures": failures})
    _remove_journal(journal)


def recover_publications(conn: sqlite3.Connection) -> dict[str, int]:
    """Reconcile interrupted local publications under the store's write lock.

    Corrupt journals fail closed and are retained. A user's changed file is never
    overwritten by compensation. This is also callable by an operator after a restart.
    """
    result = {"committed": 0, "restored": 0}
    directory = _journal_dir(conn)
    if not directory.exists() or not any(directory.glob("*.json")):
        return result  # ordinary store opening stays read-only, with no write lock
    with store.transaction(conn):
        directory = _journal_dir(conn)
        if not directory.exists():
            return result
        pending = []
        for journal in directory.glob("*.json"):
            if str(journal) in _IN_FLIGHT:
                continue
            try:
                if journal.is_symlink() or not journal.is_file():
                    raise ValueError("journal is not a regular file")
                envelope = json.loads(journal.read_text(encoding="utf-8"))
                payload = envelope["payload"]
                canonical = store._j(payload).encode("utf-8")
                if hashlib.sha256(canonical).hexdigest() != envelope["sha256"]:
                    raise ValueError("journal checksum mismatch")
                _journal_entries(payload)
                if not isinstance(payload.get("sequence"), int):
                    raise ValueError("journal has no publication ordering anchor")
            except Exception as exc:
                raise OutcomeUnknown("a projection journal is corrupt or its paths changed; nothing was overwritten",
                                     details={"journal": str(journal), "error": str(exc)[:500]}) from exc
            pending.append((journal, payload))
        # Several publications in one outer transaction can replace the same file.
        # Undo the last one first, including after a process dies before COMMIT.
        for journal, payload in sorted(pending, key=lambda item: item[1]["sequence"], reverse=True):
            committed = conn.execute(
                "SELECT 1 FROM events WHERE kind=? AND json_extract(payload_json, '$.publication_id')=? LIMIT 1",
                (PUBLICATION_EVENT, payload["publication_id"])).fetchone()
            if committed:
                _remove_journal(journal)
                result["committed"] += 1
            else:
                _restore_journal(journal, payload)
                result["restored"] += 1
    return result


def _publish(ctx: Ctx, asset: Mapping[str, Any], entries: list[dict[str, Any]], *, operation: str) -> None:
    changes = [entry for entry in entries if entry.get("changed", True)]
    if not changes:
        return
    publication_id = uuid.uuid4().hex
    sequence = int(ctx.conn.execute("SELECT COALESCE(MAX(event_id), 0) + 1 FROM events").fetchone()[0])
    payload = {"schema": 1, "publication_id": publication_id, "operation": operation, "sequence": sequence,
               "asset": assets.asset_ref(asset), "entries": [
                   {**{key: value for key, value in entry.items() if key not in ("before", "after")},
                    **{key: _encoded(entry[key]) for key in ("before", "after")},
                    **{key + "_sha256": hashlib.sha256(entry[key]).hexdigest() if entry[key] is not None else None
                       for key in ("before", "after")}} for entry in changes]}
    directory = _journal_dir(ctx.conn)
    directory.mkdir(parents=True, exist_ok=True)
    _fsync_directory(directory.parent)
    _fsync_directory(directory.parent.parent)
    journal = directory / f"{publication_id}.json"
    envelope = {"payload": payload, "sha256": hashlib.sha256(store._j(payload).encode("utf-8")).hexdigest()}
    _replace_bytes(journal, (store._j(envelope) + "\n").encode("utf-8"))

    def complete() -> None:
        try:
            _remove_journal(journal)
        finally:
            _IN_FLIGHT.discard(str(journal))

    def compensate() -> None:
        try:
            _restore_journal(journal, payload)
        finally:
            _IN_FLIGHT.discard(str(journal))

    store.on_transaction_end(ctx.conn, on_commit=complete, on_rollback=compensate)
    _IN_FLIGHT.add(str(journal))
    for entry in changes:
        target = target_of(Path(entry["root"]), entry["host"], entry["path"])
        current = target.read_bytes() if target.exists() and target.is_file() else None
        if current != entry["before"] or (target.exists() and not target.is_file()):
            raise ProjectionTargetOccupied("a projection target changed after preflight",
                                           details={"abs_path": str(target)})
        if entry["after"] is None:
            target.unlink()
            _fsync_directory(target.parent)
        else:
            _replace_bytes(target, entry["after"], entry.get("before_mode") or 0o644)
    store.append_event(ctx.conn, kind=PUBLICATION_EVENT, actor=ctx.actor, subject_kind=subjects.ASSET,
                       subject_id=asset["asset_id"], evidence_level="EXECUTION_OBSERVED",
                       payload={"publication_id": publication_id, "version": asset["version"],
                                "sha256": asset["sha256"], "operation": operation})


# ------------------------------------------------------------------------------- effect

@effect(EFFECT)
def project_to_hosts(ctx: Ctx, asset: Mapping[str, Any]) -> dict[str, Any]:
    """Write the host files of an asset that has just entered ACTIVE and report them."""
    with store.transaction(ctx.conn):
        recover_publications(ctx.conn)
        return _project_to_hosts(ctx, asset)


def _project_to_hosts(ctx: Ctx, asset: Mapping[str, Any]) -> dict[str, Any]:
    make = effects.projector_for(asset["kind"])
    if make is None:
        raise ImplementationGap(
            f"no projector is published for {asset['kind']}: what its host files are, nobody has said",
            details={"kind": asset["kind"], "asset_id": asset["asset_id"]})
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    for entry in make(ctx, asset):
        host = entry.get("host")
        if "content" not in entry:
            skipped.append({"host": host, "reason": str(entry.get("skipped") or "no reason given")})
            continue
        body = entry["content"]
        if not isinstance(body, (bytes, bytearray)):
            raise SchemaInvalid(f"the projector of {asset['kind']} gave content that is not bytes",
                                details={"host": host, "path": str(entry.get("path"))[:200]})
        root, installed = host_root(ctx.conn, str(host))
        done = _preflight(root, str(host), entry.get("path"), bytes(body), asset)
        done["installed"] = installed
        planned.append(done)
        written.append({**_public_entry(done), "installed": installed})
    if len({entry["abs_path"] for entry in planned}) != len(planned):
        raise SchemaInvalid("a projector names a target more than once")
    _publish(ctx, asset, planned, operation="publish")
    return {"written": written, "skipped": skipped}


def written_for(conn: sqlite3.Connection, asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the effect reported writing for exactly these bytes (the last report on record)."""
    found: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT payload_json FROM events WHERE kind='asset.effects' AND subject_kind=? AND subject_id=? "
        "ORDER BY event_id", (subjects.ASSET, asset["asset_id"]),
    ):
        payload = store.loads(row, "payload_json", {}) or {}
        if payload.get("sha256") != asset["sha256"]:
            continue
        result = (payload.get("effects") or {}).get(EFFECT)
        if result is not None:
            found = [dict(item) for item in result.get("written") or [] if isinstance(item, Mapping)]
    return found


def withdraw(ctx: Ctx, asset: Mapping[str, Any], prev_state: str, new_state: str) -> None:
    """Remove the files of a version that has left ACTIVE, if they are still that version's."""
    if prev_state != ACTIVE or new_state == ACTIVE:
        return
    with store.transaction(ctx.conn):
        recover_publications(ctx.conn)
        _withdraw(ctx, asset, prev_state, new_state)


def _withdraw(ctx: Ctx, asset: Mapping[str, Any], prev_state: str, new_state: str) -> None:
    removed: list[str] = []
    planned: list[dict[str, Any]] = []
    for item in written_for(ctx.conn, asset):
        target = Path(str(item.get("abs_path") or ""))
        if not target.is_absolute() or not target.is_file() or target.is_symlink():
            continue
        before = target.read_bytes()
        carried = roles.read_stamp(before.decode("utf-8", "replace"))
        if carried != roles.stamp(asset["asset_id"], asset["version"], asset["sha256"]):
            continue  # a newer version's file now, or one somebody changed: not this version's to remove
        parts = PurePosixPath(item["path"]).parts
        root = target.parents[len(parts) - 1]
        if target_of(root, item["host"], item["path"]) != target:
            raise PathTraversalDenied("withdrawal does not name its original host file")
        planned.append({"root": str(root), "host": item["host"], "path": item["path"],
                        "abs_path": str(target), "before": before, "after": None,
                        "before_mode": target.stat().st_mode & 0o777})
        removed.append(str(target))
    _publish(ctx, asset, planned, operation="withdraw")
    if removed:
        store.append_event(
            ctx.conn, kind="asset.projection_withdrawn", actor=ctx.actor, subject_kind=subjects.ASSET,
            subject_id=asset["asset_id"], prev_state=prev_state, new_state=new_state,
            evidence_level="EXECUTION_OBSERVED",
            payload={"version": asset["version"], "sha256": asset["sha256"], "removed": removed},
        )


for _kind in PROJECTED_KINDS:
    on_transition(_kind)(withdraw)


# ------------------------------------------------------------------------- role intents

@projector(roles.KIND)
def role_files(ctx: Ctx, asset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The role files of a role intent that is an asset: one per host it lists."""
    content = assets.content(ctx.conn, asset)
    policy_doc = policy.agent_policy()[0]
    problems = roles.problems_in(content, policy_doc)
    manifest = content.get("manifest") if isinstance(content, Mapping) else None
    if isinstance(manifest, Mapping):
        if manifest.get("role_id") != asset["asset_id"] or str(manifest.get("version")) != str(asset["version"]):
            problems.append(
                f"the content names {manifest.get('role_id')}@{manifest.get('version')} and is registered "
                f"as {asset['asset_id']}@{asset['version']}")
        if str(manifest.get("version")) in (roles.VERSION_SHIPPED, codex_roles.VERSION):
            problems.append(f"version {str(manifest.get('version'))!r} names the intents this tree ships; an "
                            "intent that takes the gate has a version of its own")
    if problems:
        raise SchemaInvalid(
            "this role intent may not be projected: " + "; ".join(problems[:6]),
            details={"asset_id": asset["asset_id"], "version": asset["version"], "problems": problems[:20]})
    return roles.files(content, asset_id=asset["asset_id"], version=asset["version"], sha256=asset["sha256"],
                       policy=policy_doc)


store.register_recovery("host_projection_publications", recover_publications)


__all__ = [
    "EFFECT", "PROJECTION_DIRS", "host_root", "hosts_dir", "install_state", "project_to_hosts", "role_files",
    "recover_publications", "target_of", "withdraw", "write_projection", "written_for",
]
