"""A static scan of a bundled script for what it could reach.

``scope_scan`` (``contracts/gates.json``) looks at every script an asset bundles for network
access, subprocesses, ``eval``/``exec`` and dynamic imports, unpickling, writes and
deletions that are not confined to a declared output directory, and reads of the paths this
workspace forbids. It parses; it never imports or runs the script.

A static scan cannot prove a script harmless, and it does not try to. Each thing it finds is
a *finding* with a fingerprint — the same construct in the same bytes always has the same
fingerprint — and the check hands the findings to a reviewer as diagnostics. An adapter that
starts a subprocess to launch its binary is normal; one that does it in a decision skill's
helper is not. That judgement is a person's or an independent reviewer's, recorded against
the fingerprint.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

NETWORK_MODULES = frozenset({
    "socket", "ssl", "urllib", "urllib2", "urllib3", "http", "httplib", "httpx", "requests", "aiohttp",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc", "websocket", "websockets",
    "paramiko", "pycurl", "grpc", "boto3", "botocore", "socketserver", "asyncssh", "fsspec",
    "huggingface_hub", "wget", "gdown",
})
PROCESS_MODULES = frozenset({"subprocess", "pty", "multiprocessing", "pexpect", "sh", "commands"})
PICKLE_MODULES = frozenset({"pickle", "cPickle", "_pickle", "dill", "cloudpickle", "shelve", "marshal", "joblib"})

PROCESS_CALLS = frozenset({
    "os.system", "os.popen", "os.execl", "os.execle", "os.execlp", "os.execv", "os.execve", "os.execvp",
    "os.execvpe", "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnv", "os.spawnve", "os.spawnvp",
    "os.fork", "os.forkpty", "os.posix_spawn", "os.startfile",
})
DYNAMIC_CALLS = frozenset({"eval", "exec", "compile", "__import__", "importlib.import_module",
                           "importlib.reload", "runpy.run_path", "runpy.run_module"})
UNPICKLE_CALLS = frozenset({
    "pickle.load", "pickle.loads", "pickle.Unpickler", "cPickle.load", "cPickle.loads", "dill.load",
    "dill.loads", "cloudpickle.load", "cloudpickle.loads", "joblib.load", "marshal.load", "marshal.loads",
    "shelve.open", "torch.load", "pandas.read_pickle", "pd.read_pickle", "yaml.load", "yaml.unsafe_load",
})
DELETE_CALLS = frozenset({
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.renames", "os.replace",
    "os.truncate", "os.chmod", "os.chown", "os.symlink", "os.link", "shutil.rmtree", "shutil.move",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
})
#: Method names that write or delete whatever object they are called on (pathlib and the like).
#: ``replace`` and ``rename`` are left out on purpose: on a string or a data frame they are
#: everywhere and write nothing, and ``os.replace`` / ``os.rename`` are caught by name above.
WRITE_METHODS = frozenset({"write_text", "write_bytes", "unlink", "rmdir", "touch", "symlink_to",
                           "hardlink_to", "chmod", "mkdir"})

CATEGORIES = ("network", "subprocess", "dynamic_code", "unpickling", "filesystem_write", "forbidden_path")


@dataclass(frozen=True)
class Finding:
    category: str
    line: int
    what: str
    source: str

    def fingerprint(self, script_sha256: str) -> str:
        material = "\0".join((script_sha256, self.category, self.what, " ".join(self.source.split())))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def as_diagnostic(self, *, path: str, script_sha256: str) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint(script_sha256), "script": path, "script_sha256": script_sha256,
            "category": self.category, "line": self.line, "what": self.what, "source": self.source[:240],
        }


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _segment(source_lines: list[str], node: ast.AST) -> str:
    line = getattr(node, "lineno", 0)
    return source_lines[line - 1].strip() if 0 < line <= len(source_lines) else ""


def _write_mode(call: ast.Call) -> str | None:
    """The mode of an ``open`` call when it writes, appends or creates; None when it only reads."""
    mode: ast.AST | None = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return None
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value if any(flag in mode.value for flag in "wax+") else None
    return "<computed>"


def _strings(tree: ast.AST) -> Iterable[tuple[ast.Constant, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, node.value
        elif isinstance(node, ast.JoinedStr):
            literal = "".join(part.value for part in node.values
                              if isinstance(part, ast.Constant) and isinstance(part.value, str))
            if literal:
                yield node, literal  # type: ignore[misc]


def scan_python(source: str, *, forbidden_patterns: Iterable[str] = ()) -> list[Finding]:
    """Every finding in one Python source. Raises ``SyntaxError`` when it does not parse."""
    tree = ast.parse(source)
    lines = source.splitlines()
    findings: list[Finding] = []
    aliases: dict[str, str] = {}

    def add(category: str, node: ast.AST, what: str) -> None:
        findings.append(Finding(category, getattr(node, "lineno", 0), what, _segment(lines, node)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                aliases[(alias.asname or alias.name).split(".")[0]] = alias.name if alias.asname else root
                _import(add, node, root, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name
            _import(add, node, root, module)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        resolved = name
        if name:
            head, _, rest = name.partition(".")
            if head in aliases:
                resolved = aliases[head] + ("." + rest if rest else "")
        for candidate in {name, resolved} - {None}:
            if candidate in PROCESS_CALLS or candidate.split(".")[0] in PROCESS_MODULES:
                add("subprocess", node, f"call {candidate}")
                break
            if candidate in DYNAMIC_CALLS:
                add("dynamic_code", node, f"call {candidate}")
                break
            if candidate in UNPICKLE_CALLS:
                add("unpickling", node, f"call {candidate}")
                break
            if candidate in DELETE_CALLS:
                add("filesystem_write", node, f"call {candidate}")
                break
            if candidate.split(".")[0] in NETWORK_MODULES:
                add("network", node, f"call {candidate}")
                break
        else:
            if name in ("open", "io.open", "builtins.open") or (name or "").endswith(".open"):
                mode = _write_mode(node)
                if mode is not None:
                    add("filesystem_write", node, f"open(..., mode={mode!r})")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_METHODS:
                add("filesystem_write", node, f"method .{node.func.attr}()")
            elif name in ("numpy.load", "np.load"):
                for keyword in node.keywords:
                    if keyword.arg == "allow_pickle" and not (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                    ):
                        add("unpickling", node, "numpy.load(allow_pickle=…)")

    patterns = list(forbidden_patterns)
    if patterns:
        for node, text in _strings(tree):
            if len(text) > 400 or "\n" in text:
                continue
            base = text.rsplit("/", 1)[-1]
            for pattern in patterns:
                if fnmatch.fnmatch(text, pattern) or fnmatch.fnmatch(base, pattern.rsplit("/", 1)[-1]):
                    add("forbidden_path", node, f"string matches forbidden pattern {pattern!r}")
                    break

    unique: dict[tuple[str, int, str], Finding] = {}
    for finding in findings:
        unique.setdefault((finding.category, finding.line, finding.what), finding)
    return sorted(unique.values(), key=lambda f: (f.line, f.category, f.what))


def _import(add: Any, node: ast.AST, root: str, full: str) -> None:
    if root in NETWORK_MODULES:
        add("network", node, f"import {full}")
    elif root in PROCESS_MODULES:
        add("subprocess", node, f"import {full}")
    elif root in PICKLE_MODULES:
        add("unpickling", node, f"import {full}")
    elif root == "ctypes":
        add("dynamic_code", node, f"import {full}")
