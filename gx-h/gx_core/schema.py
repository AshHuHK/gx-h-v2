"""JSON Schemas generated from ``contracts/mcp_tools.json``, plus a small validator.

Used by the one dispatcher (:mod:`gx_core.api`), so by both transports: the command-line
client and the MCP server validate a call against the same generated schema.

Two rules this module exists to keep.

**The schema is generated, never written twice.** A hand-written copy of the tool table
drifts from the contract, and the drift is invisible until a client is rejected for
sending exactly what the contract documents. Every schema here is derived from
``contracts/mcp_tools.json`` at call time.

**A format error is never reported as a scientific failure.** Anything that fails
validation returns ``SCHEMA_INVALID`` with the offending JSON pointer, so a client can
tell "you sent the wrong shape" from "the experiment did not work".

The contract writes its input types in a compact mini-language (``"str"``, ``"int?"``,
``"array<PathSpec>"``, ``"object?"``, ``"str|null"``). :func:`parse_type` is the single
place that interprets it.

Undeclared arguments
--------------------
Extra keys are **accepted, dropped and reported**, not rejected. That is deliberate.
A client that sends ``approved: true`` must not get a schema error — a schema error
reads like "send it differently". It must get a normal response in which the field had
no effect whatsoever, because ``approved`` is not an input to anything here: the backend
decides approval from its own store (§13.4). Dropping undeclared keys before dispatch
means a handler cannot read one even by accident.
"""

from __future__ import annotations

from typing import Any

from . import contracts

#: Undeclared keys that look like an attempt to assert authority. They are dropped like
#: any other undeclared key; the separate warning exists so the attempt is visible in
#: the response and in the server log rather than silently swallowed.
AUTHORITY_CLAIM_KEYS = frozenset({
    "approved", "approval", "approval_granted", "auto_approve",
    "role", "roles", "worker_scope", "scope", "scopes",
    "allowed_actions", "permissions", "privileged", "privilege",
    "admin", "is_admin", "operator", "trusted", "bypass", "force",
    "principal", "identity", "auth", "authorization", "token", "api_key",
})

_SCALARS = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "num": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
    "null": "null",
    "true": "boolean",  # the contract writes a literal `"bound": true` in one output
}


def parse_type(decl: Any) -> tuple[dict[str, Any], bool]:
    """``(json_schema_fragment, optional)`` for one contract type declaration."""
    if isinstance(decl, bool):
        return {"type": "boolean"}, False
    if isinstance(decl, list):
        # e.g. ["str"] -- an array of that type
        item, _ = parse_type(decl[0]) if decl else ({}, False)
        return {"type": "array", "items": item}, False
    if isinstance(decl, dict):
        props = {}
        required = []
        for key, sub in decl.items():
            fragment, optional = parse_type(sub)
            props[key] = fragment
            if not optional:
                required.append(key)
        return {"type": "object", "properties": props, "required": sorted(required)}, False
    if not isinstance(decl, str):
        raise ValueError(f"cannot interpret contract type declaration {decl!r}")

    text = decl.strip()
    optional = text.endswith("?")
    if optional:
        text = text[:-1].strip()

    if "|" in text:
        members = [parse_type(part)[0] for part in text.split("|")]
        types: list[str] = []
        for member in members:
            kind = member.get("type")
            if isinstance(kind, str) and kind not in types:
                types.append(kind)
        return ({"type": types} if len(types) > 1 else members[0]), optional

    if text.startswith("array<") and text.endswith(">"):
        inner = text[len("array<"):-1].strip()
        return {"type": "array", "items": _named_or_scalar(inner)}, optional

    return _named_or_scalar(text), optional


def _named_or_scalar(text: str) -> dict[str, Any]:
    lowered = text.lower()
    if lowered in _SCALARS:
        return {"type": _SCALARS[lowered]}
    if text.startswith("enum:"):
        # A value of one label axis (contracts/labels.json): a string limited to the axis's values.
        # An axis the labels do not define raises KeyError: contract drift, never a free string.
        return {"type": "string", "enum": contracts.label_values(text[len("enum:"):].strip())}
    if text.startswith("{") or text.startswith("<"):
        return {"type": "object", "x-gx-contract-type": text}
    # A named contract type such as ``PathSpec``. Its field-level schema lives with the
    # service that owns it; at the protocol boundary it is an object. Saying so, rather
    # than inventing the fields here, keeps the two from disagreeing.
    return {"type": "object", "x-gx-contract-type": text}


def tool_input_schema(tool: str, *, native_workspace: bool = False) -> dict[str, Any]:
    """JSON Schema for one tool's arguments, generated from the contract.

    Local native threads can also advertise whole-file delivery for scoped artifact reads.
    The ordinary worker, operator and host catalogs retain their scientific contracts.
    """
    spec = contracts.any_tool_spec(tool)
    declared = spec.get("input", {}) or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, decl in declared.items():
        fragment, optional = parse_type(decl)
        properties[name] = fragment
        if not optional:
            required.append(name)
    if native_workspace and tool == "gx_artifact_get":
        # A delivery option of the local Codex connection. The scientific read contract
        # and the schemas installed in earlier native threads retain their original pins.
        properties["materialize"] = {"type": "boolean", "description":
            "Return a verified whole-file copy in the project workspace. Omit section, offset and limit."}
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        # Undeclared keys are accepted by the schema and then dropped before dispatch;
        # see the module docstring. ``false`` here would turn a privilege-escalation
        # attempt into a formatting complaint.
        "additionalProperties": True,
        "x-gx-undeclared-arguments": "accepted, dropped before dispatch, reported in warnings",
    }


def tool_output_shape(tool: str) -> dict[str, Any]:
    """The contract's declared output shape, for documentation and client hints."""
    spec = contracts.any_tool_spec(tool)
    declared = spec.get("output", {}) or {}
    properties = {}
    for name, decl in declared.items():
        properties[name] = parse_type(decl)[0]
    return {"type": "object", "properties": properties}


# ----------------------------------------------------------------------- validation

def validate(instance: Any, schema: dict[str, Any], *, pointer: str = "") -> list[str]:
    """Validate against the subset of JSON Schema this contract uses.

    Supported: ``type`` (single or union), ``properties``, ``required``, ``items``,
    ``additionalProperties: false``, ``enum``. Anything else in a schema is ignored
    rather than silently treated as satisfied-by-default in a way that hides it: the
    generator above only emits the supported subset, and
    :func:`unsupported_schema_keywords` fails a contract test if that stops being true.
    """
    errors: list[str] = []
    where = pointer or "$"

    expected = schema.get("type")
    if expected is not None and not _type_matches(instance, expected):
        errors.append(f"{where}: expected {_describe(expected)}, got {_json_type(instance)}")
        return errors

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} is not one of {schema['enum']}")

    if _json_type(instance) == "object":
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{where}: missing required property {name!r}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                errors.extend(validate(value, properties[name], pointer=f"{where}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{where}: property {name!r} is not allowed")

    if _json_type(instance) == "array" and "items" in schema:
        for index, value in enumerate(instance):
            errors.extend(validate(value, schema["items"], pointer=f"{where}[{index}]"))

    return errors


_SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "items", "additionalProperties", "enum",
})


def unsupported_schema_keywords(schema: dict[str, Any]) -> set[str]:
    """Keywords present in a schema that :func:`validate` would not enforce."""
    found: set[str] = set()
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("x-") or key in ("$schema", "description", "title"):
                    continue
                if key not in _SUPPORTED_KEYWORDS:
                    found.add(key)
                if key in ("properties",) and isinstance(value, dict):
                    stack.extend(value.values())
                elif key == "items":
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    actual = _json_type(value)
    if expected == actual:
        return True
    # An integer is an acceptable number; a float that happens to be whole is not an
    # integer, because a count of 3.0 usually means the caller computed where it should
    # have counted.
    return expected == "number" and actual == "integer"


def _describe(expected: Any) -> str:
    if isinstance(expected, list):
        return " or ".join(str(item) for item in expected)
    return str(expected)


# ------------------------------------------------------------------ argument intake

class ArgumentCheck:
    """Result of validating one tool call's arguments."""

    __slots__ = ("tool", "clean", "errors", "dropped", "authority_claims")

    def __init__(
        self,
        tool: str,
        clean: dict[str, Any],
        errors: list[str],
        dropped: list[str],
        authority_claims: list[str],
    ) -> None:
        self.tool = tool
        self.clean = clean
        self.errors = errors
        self.dropped = dropped
        self.authority_claims = authority_claims

    @property
    def ok(self) -> bool:
        return not self.errors

    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.authority_claims:
            out.append(
                "ignored client-supplied authority claim(s) "
                f"{sorted(self.authority_claims)}: this server decides identity, scope, "
                "budget and approval from its own state; a client field never grants anything"
            )
        plain = sorted(set(self.dropped) - set(self.authority_claims))
        if plain:
            out.append(f"ignored undeclared argument(s) {plain} for {self.tool}")
        return out


def check_arguments(tool: str, arguments: Any, *, native_workspace: bool = False) -> ArgumentCheck:
    """Validate and clean one tool call's arguments against the generated schema."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return ArgumentCheck(
            tool, {}, [f"$: arguments must be an object, got {_json_type(arguments)}"], [], []
        )

    schema = tool_input_schema(tool, native_workspace=native_workspace)
    declared = set(schema["properties"])
    required = set(schema["required"])
    dropped = [key for key in arguments if key not in declared]
    claims = [key for key in dropped if key.lower() in AUTHORITY_CLAIM_KEYS]
    # An optional argument given as null is an argument not given: a client that
    # serialises every field of a request object sends "limit": null for no limit.
    clean = {
        key: value for key, value in arguments.items()
        if key in declared and not (value is None and key not in required)
    }
    errors = validate(clean, schema)
    return ArgumentCheck(tool, clean, errors, dropped, claims)
