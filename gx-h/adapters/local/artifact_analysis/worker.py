"""Evaluate a bounded numeric expression on explicitly staged JSON artifacts (Linux)."""
import argparse
import ast
import ctypes
import errno
import json
import math
import operator
import os
import random
import re
import resource
import socket
import statistics
import sys
from pathlib import Path

INPUT_BYTES = 1 << 20
SPEC_BYTES = 16384
OUTPUT_BYTES = 32768
MEMORY_BYTES = 128 << 20
ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,30}\Z")
SCALARS = {"number", "integer", "boolean", "string"}
OPERATIONS = {"count", "sum", "mean", "median", "stdev", "min", "max", "column", "sample", "abs", "sqrt"}
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e200:
        raise ValueError("expected a finite bounded number")
    return value


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) - {"script", "parameters", "output_schema", "seed"}:
        raise ValueError("supply script, output_schema, optional parameters and seed")
    script, schema = spec.get("script"), spec.get("output_schema")
    if not isinstance(script, str) or not 0 < len(script) <= 4096:
        raise ValueError("script must be one expression, at most 4096 characters")
    if not isinstance(spec.get("parameters", {}), dict) or (
            "seed" in spec and (type(spec["seed"]) is not int or not 0 <= spec["seed"] < 2**32)):
        raise ValueError("expected a parameter object and optional unsigned 32-bit integer seed")
    if not isinstance(schema, dict) or set(schema) != {"type", "properties", "required", "additionalProperties"}:
        raise ValueError("output_schema must declare an exact JSON object")
    properties = schema["properties"]
    if (schema["type"] != "object" or schema["additionalProperties"] is not False or
            not isinstance(properties, dict) or not 1 <= len(properties) <= 32 or
            any(not ALIAS.fullmatch(k) for k in properties) or not isinstance(schema["required"], list) or
            not all(isinstance(k, str) for k in schema["required"]) or sorted(schema["required"]) != sorted(properties)):
        raise ValueError("all output properties must be required, with additionalProperties false")
    for prop in properties.values():
        if not isinstance(prop, dict):
            raise ValueError("invalid output schema property")
        if set(prop) == {"type"} and isinstance(prop["type"], str) and prop["type"] in SCALARS:
            continue
        items = prop.get("items")
        if (set(prop) != {"type", "items", "maxItems"} or prop["type"] != "array" or
                not isinstance(items, dict) or set(items) != {"type"} or not isinstance(items["type"], str)
                or items["type"] not in SCALARS or type(prop["maxItems"]) is not int or not 0 <= prop["maxItems"] <= 4096):
            raise ValueError("arrays require scalar items and bounded maxItems")
    tree = ast.parse(script, mode="eval")
    allowed = (ast.Expression, ast.Dict, ast.List, ast.Constant, ast.Name, ast.Load, ast.Subscript,
               ast.Call, ast.UnaryOp, ast.UAdd, ast.USub, ast.BinOp, *BINARY)
    nodes = list(ast.walk(tree))
    if len(nodes) > 256 or not isinstance(tree.body, ast.Dict):
        raise ValueError("script must return an object in at most 256 syntax nodes")
    for node in nodes:
        if type(node) not in allowed:
            raise ValueError("unsupported expression: " + type(node).__name__)
        if isinstance(node, ast.Name) and node.id not in {"data", "params"} | OPERATIONS:
            raise ValueError("unavailable name: " + node.id)
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in OPERATIONS or node.keywords):
            raise ValueError("only listed numeric operations are callable")
        if isinstance(node, ast.Dict):
            keys = [key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None for key in node.keys]
            if any(key is None or not ALIAS.fullmatch(key) for key in keys) or len(set(keys)) != len(keys):
                raise ValueError("object keys must be distinct literal identifiers")
        if isinstance(node, ast.Constant) and type(node.value) not in (str, float, int, bool, type(None)):
            raise ValueError("unsupported literal")
    return tree.body


def evaluate(spec, data):
    tree = validate_spec(spec)
    def operation(name, args):
        if name in ("sqrt", "abs") and len(args) == 1:
            return number((math.sqrt if name == "sqrt" else abs)(number(args[0])))
        if not args or not isinstance(args[0], list):
            raise ValueError(name + " requires a JSON array")
        rows = args[0]
        if name == "column" and len(args) == 2 and isinstance(args[1], str):
            return [row[args[1]] for row in rows]
        if name == "sample" and len(args) == 2 and "seed" in spec and type(args[1]) is int and 0 <= args[1] <= 4096:
            return random.Random(spec["seed"]).sample(rows, args[1])
        if len(args) != 1 or name not in ("sum", "mean", "median", "stdev", "min", "max", "count"):
            raise ValueError("invalid arguments to " + name)
        if name == "count":
            return len(rows)
        values = [number(x) for x in rows]
        function = {"sum": math.fsum, "mean": statistics.fmean, "median": statistics.median,
                    "stdev": statistics.stdev, "min": min, "max": max}[name]
        return number(function(values))

    def expr(node):
        if isinstance(node, ast.Dict):
            return {k.value: expr(v) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.List):
            return [expr(v) for v in node.elts]
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return {"params": spec.get("parameters", {}), "data": data}[node.id]
        if isinstance(node, ast.Subscript):
            value, key = expr(node.value), expr(node.slice)
            if isinstance(value, dict) and isinstance(key, str) or isinstance(value, list) and type(key) is int:
                return value[key]
            raise ValueError("indexing requires a JSON array or object")
        if isinstance(node, ast.Call):
            return operation(node.func.id, [expr(v) for v in node.args])
        if isinstance(node, ast.UnaryOp):
            value = number(expr(node.operand))
            return -value if isinstance(node.op, ast.USub) else value
        return number(BINARY[type(node.op)](number(expr(node.left)), number(expr(node.right))))

    result = expr(tree)
    props = spec["output_schema"]["properties"]
    if set(result) != set(props):
        raise ValueError("result fields disagree with output_schema")
    def check(value, kind):
        valid = {"integer": type(value) is int, "boolean": type(value) is bool, "string": type(value) is str,
                 "number": type(value) in (int, float)}[kind]
        if not valid or kind in ("number", "integer") and number(value) != value:
            raise ValueError("result type disagrees with output_schema")
    for name, value in result.items():
        prop = props[name]
        if prop["type"] == "array":
            if not isinstance(value, list) or len(value) > prop["maxItems"]:
                raise ValueError("result array exceeds output_schema")
            for item in value:
                check(item, prop["items"]["type"])
        else:
            check(value, prop["type"])
    return result


def restrict_process():
    """Default-deny kernel filter installed before parsing any supplied program or data."""
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    policy = lib.seccomp_init(0x00050000 | errno.EPERM)
    if not policy:
        raise RuntimeError("seccomp policy unavailable")
    try:
        # Input bytes are already in memory. The only live descriptor is the result pipe.
        for name in ("read", "write", "close", "brk", "mmap", "munmap", "mremap", "futex",
                     "rt_sigreturn", "exit", "exit_group"):
            nr = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if nr < 0 or lib.seccomp_rule_add(policy, 0x7fff0000, nr, 0):
                raise RuntimeError("seccomp could not build the required syscall policy")
        if lib.seccomp_load(policy):
            raise RuntimeError("seccomp could not enter the required isolation")
    finally:
        lib.seccomp_release(policy)


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def child(spec_bytes, files, result_fd):
    # Close inherited stdin/stdout/stderr and any service descriptors before invoking the evaluator.
    os.dup2(result_fd, 3)
    os.closerange(0, 3)
    for descriptor in os.listdir("/proc/self/fd"):
        if int(descriptor) > 3:
            try:
                os.close(int(descriptor))
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
    memory = min(MEMORY_BYTES, int(os.environ.get("GX_JOB_MEMORY_MIB", 128)) << 20)
    os.environ.clear()
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    cpu_limit = resource.getrlimit(resource.RLIMIT_CPU)[0]
    if cpu_limit < 1 or cpu_limit > 10 or resource.getrlimit(resource.RLIMIT_AS)[1] != memory or resource.getrlimit(resource.RLIMIT_FSIZE)[1] != 0:
        raise RuntimeError("required Job CPU, address-space or disk bound missing")
    restrict_process()
    # Positive proof of kernel enforcement uses a harmless local device and an unconnected socket.
    for probe in (lambda: os.open(os.devnull, os.O_RDONLY), lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)):
        try:
            probe()
        except PermissionError as error:
            if error.errno != errno.EPERM:
                raise
        else:
            os._exit(71)
    try:
        spec = json.loads(spec_bytes)
        data = {name: json.loads(raw) for name, raw in files.items()}
        payload = encoded(evaluate(spec, data))
        if len(payload) > OUTPUT_BYTES:
            raise ValueError("encoded result exceeds 32768 bytes")
        message = b"+" + payload
    except (ValueError, LookupError, TypeError, SyntaxError, ArithmeticError, RecursionError, MemoryError) as exc:
        message = b"-" + (str(exc) or type(exc).__name__)[:200].encode("ascii", "replace")
    view = memoryview(message)
    while view:
        view = view[os.write(3, view):]
    os._exit(0)


def run(inputs, out):
    if "spec" not in inputs or not 2 <= len(inputs) <= 9:
        raise ValueError("one specification and one to eight named JSON artifacts required")
    raw, total = {}, 0
    for role, path in inputs.items():
        if role != "spec" and (not role.startswith("input_") or not ALIAS.fullmatch(role[6:])):
            raise ValueError("invalid artifact input alias")
        limit = SPEC_BYTES if role == "spec" else INPUT_BYTES - total
        with open(path, "rb") as f:
            content = f.read(limit+1)
        if len(content) > limit:
            raise ValueError("input exceeds declared byte limit")
        if role == "spec":
            spec = content
        else:
            raw[role[6:]] = content
            total += len(content)
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            child(spec, raw, wfd)
        finally:
            os._exit(70)
    os.close(wfd)
    with os.fdopen(rfd, "rb") as read:
        answer = read.read(OUTPUT_BYTES + 2)
    _, status = os.waitpid(pid, 0)
    if status or len(answer) > OUTPUT_BYTES + 1 or not answer.startswith(b"+"):
        detail = answer[1:201].decode("ascii", "replace") if answer.startswith(b"-") else "isolation or resource limit"
        raise ValueError("analysis refused: " + detail)
    (Path(out) / "result.json").write_bytes(answer[1:])
    print(json.dumps({"kernel_filter": "libseccomp-default-EPERM", "socket_probe":"EPERM", "open_probe":"EPERM",
                      "output_bytes":len(answer)-1, "hard_fsize_bytes":0,
                      "hard_address_space_bytes":min(MEMORY_BYTES, int(os.environ.get("GX_JOB_MEMORY_MIB",128)) << 20),
                      "cpu_seconds":resource.getrlimit(resource.RLIMIT_CPU)[0], "visible_input_aliases":sorted(raw)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--spec", required=True, help="Job manifest role for the program")
    options = parser.parse_args()
    try:
        run(json.loads(options.inputs), options.out)
    except (ValueError, OSError) as error:
        sys.exit(str(error))
