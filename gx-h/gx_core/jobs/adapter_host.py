"""Run one function of one adapter module, in a process of its own.

    python -I -B host.py <request.json> <response.json>

An adapter module is code somebody wrote — after this build, code an agent wrote during
a run. The backend therefore never imports one. When it needs an adapter to say what its
smoke input is, whether an input is acceptable, or what a tool's output means, it starts
this program with a scrubbed environment and a time limit, and reads the answer from a
file. One request, one function call, one process.

request   {"module_path", "sha256", "function", "args": [...], "lib_dir"}
response  {"ok": true, "result": ...}
          {"ok": false, "error_type", "error", "missing_function": bool}

The module is re-hashed here, immediately before it is loaded: the caller checked the
same digest, and the file could have changed in between.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import traceback


def _respond(path: str, document: dict) -> None:
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: host.py <request.json> <response.json>", file=sys.stderr)
        return 2
    response_path = argv[2]
    try:
        with open(argv[1], encoding="utf-8") as handle:
            request = json.load(handle)
        module_path = request["module_path"]
        with open(module_path, "rb") as handle:
            source = handle.read()
        observed = hashlib.sha256(source).hexdigest()
        if observed != request["sha256"]:
            _respond(response_path, {
                "ok": False, "error_type": "ContentHashMismatch", "missing_function": False,
                "error": f"{module_path} hashes to {observed[:16]}..., the capability was "
                         f"registered with {str(request['sha256'])[:16]}...",
            })
            return 0
        lib_dir = request.get("lib_dir")
        if lib_dir:
            sys.path.insert(0, lib_dir)
        spec = importlib.util.spec_from_file_location("gx_adapter_module", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        function = getattr(module, request["function"], None)
        if not callable(function):
            _respond(response_path, {
                "ok": False, "error_type": "AttributeError", "missing_function": True,
                "error": f"the adapter module defines no function {request['function']!r}",
            })
            return 0
        result = function(*request.get("args", []))
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            _respond(response_path, {
                "ok": False, "error_type": "TypeError", "missing_function": False,
                "error": f"{request['function']} returned something that is not JSON: {exc}",
            })
            return 0
        _respond(response_path, {"ok": True, "result": result})
        return 0
    except BaseException as exc:  # noqa: BLE001 - every failure is an answer, in the file
        _respond(response_path, {
            "ok": False, "error_type": type(exc).__name__, "missing_function": False,
            "error": str(exc)[:2000], "traceback": traceback.format_exc()[-3000:],
        })
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
