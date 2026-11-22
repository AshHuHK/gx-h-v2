"""Local read-only access to diagnostic files in the original Job output directory."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
from .runner import MAX_LISTED_FILES


@contextmanager
def _directory(root, parts):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _parts(row):
    ref = Path(row["staging_ref"] or "")
    if ref.parent.parts != ("artifacts", ".staging", "jobs", row["job_id"]) or not ref.name.startswith("stg_"):
        raise ValueError("unrecognized job staging reference")
    return ref.parts


def remove_staging(root, row):
    """Remove only this Job's staging directory, even if a parent was replaced by a link."""
    if not row["staging_ref"]:
        return
    try:
        parts = _parts(row)
        with _directory(root, parts[:-1]) as parent:
            shutil.rmtree(parts[-1], dir_fd=parent, ignore_errors=True)
    except (OSError, ValueError):
        pass  # Same best-effort cleanup as the original Job owner.


def inventory(root, row, intent, outcome):
    """Bound the preview, retain all original bytes, and never traverse links."""
    files = []
    complete = True
    runner_files = outcome.get("files")
    observed = {}
    if isinstance(runner_files, list):
        observed = {item["path"]: item["sha256"] for item in runner_files
                    if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("sha256")}
    opened = False
    try:
        with _directory(root, _parts(row)) as output:
            opened = True
            def unreadable(error):
                raise error

            for folder, _, names, fd in os.fwalk(".", dir_fd=output, onerror=unreadable):
                for name in sorted(names):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        if len(files) == MAX_LISTED_FILES:
                            complete = False
                            break
                        path = str(Path(folder) / name)
                        item = {"path": path, "bytes": info.st_size}
                        if path in observed:
                            item["sha256_at_runner_exit"] = observed[path]
                        files.append(item)
                if not complete:
                    break
    except FileNotFoundError:
        if not opened:
            return None
        complete = False
    except (NotADirectoryError, ValueError):
        if not opened:
            return None  # An invalid reference or a link replacing the owned staging root.
        complete = False
    except (OSError, RecursionError):
        complete = False  # An unreadable directory is not evidence that it is empty.
    if not files and complete:
        return None
    return {"schema": "gx.job_recovery/1", "validation": "UNVERIFIED", "job_id": row["job_id"],
            "run_id": row["run_id"], "staging_ref": row["staging_ref"],
            "input_manifest_ref": row["input_manifest_ref"], "input_manifest_sha256": intent.get("input_manifest_sha256"),
            "capability_version": intent.get("capability_version"), "manifest_sha256": intent.get("manifest_sha256"),
            "regular_files": files, "listing_complete": complete}


def open_file(root, row, name):
    """Return a read-only binary stream to a regular file under the admitted directory."""
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or ".." in parts:
        raise ValueError("recovery requires a relative output filename")
    with _directory(root, (*_parts(row), *parts[:-1])) as parent:
        if not stat.S_ISREG(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
            raise ValueError("recovery requires a regular file")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("recovery requires a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
