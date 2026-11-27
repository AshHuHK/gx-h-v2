"""Opt-in scientific input domains of a method and its concrete packaging."""
import re

from ..errors import ArtifactHashMismatch, ArtifactNotFound, SchemaInvalid

VERSION = "gx.science-domain/1"


def declarations(method, packaging):
    return {scope: info for scope, info in (("method", method), ("provider", packaging))
            if isinstance(info, dict) and info.get("schema") == VERSION}


def _strings(values):
    return isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values)


def problems(document, roles, parameters):
    """Other historical applicable_domain objects have no executable contract."""
    if not isinstance(document, dict) or document.get("schema") != VERSION:
        return []
    errors = []
    if set(document) - {"schema", "inputs", "randomness"}:
        errors.append("unexpected scientific domain field")
    if "randomness" in document:
        random = document["randomness"]
        if (not isinstance(random, dict) or set(random) - {"mode", "seed_parameter", "fixed_seed"}
                or random.get("mode") not in ("deterministic", "stochastic")
                or ("seed_parameter" in random and (not isinstance(random["seed_parameter"], str)
                                                     or random["seed_parameter"] not in parameters))
                or ("fixed_seed" in random and type(random["fixed_seed"]) is not int)
                or ("fixed_seed" in random and "seed_parameter" in random)):
            errors.append("invalid mode or seed declaration")
    rules = document.get("inputs")
    if not isinstance(rules, dict) or not rules or set(rules) - set(roles):
        return [*errors, "input rules require declared input roles"]
    for role, rule in rules.items():
        if not isinstance(rule, dict) or not rule or set(rule) - {"sha256", "media_types", "json_fields"}:
            errors.append(f"{role} requires content digests, media types or JSON field conditions")
            continue
        if "sha256" in rule and (not _strings(rule["sha256"]) or not rule["sha256"] or
                                 not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in rule["sha256"])):
            errors.append(f"{role} has invalid fixed input SHA-256")
        if "media_types" in rule and (not _strings(rule["media_types"]) or not rule["media_types"]):
            errors.append(f"{role} has invalid media types")
        if "json_fields" not in rule:
            continue
        fields = rule["json_fields"]
        if not isinstance(fields, dict) or not fields:
            errors.append(f"{role} needs explicit properties of the JSON input")
            continue
        for key, limits in fields.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(limits, dict) or not limits:
                errors.append(f"{role} requires named, bounded properties")
                continue
            if set(limits) in ({"one_of"}, {"subset_of"}):
                if not _strings(next(iter(limits.values()))) or limits.get("one_of") == []:
                    errors.append(f"{role}.{key} has invalid categories")
            elif (set(limits) - {"min", "max"} or any(type(v) is not int for v in limits.values())
                  or "min" in limits and "max" in limits and limits["min"] > limits["max"]):
                errors.append(f"{role}.{key} has invalid integer bounds")
    return errors


def _matches(value, limits):
    if "one_of" in limits:
        return isinstance(value, str) and value in limits["one_of"]
    if "subset_of" in limits:
        return isinstance(value, list) and all(isinstance(v, str) and v in limits["subset_of"] for v in value)
    return type(value) is int and limits.get("min", value) <= value <= limits.get("max", value)


class InputFacts:
    """Request-local facts from the actual artifacts visible in this Run."""
    def __init__(self, artifacts, run_id, *, preview=False):
        self.artifacts = artifacts
        self.run_id = run_id
        self.preview = preview
        self.metadata = {}
        self.documents = {}

    def describe(self, ref):
        if ref not in self.metadata:
            try:
                meta = self.artifacts.describe(ref)
            except ArtifactNotFound:
                if not self.preview:
                    raise
                meta = None
            if meta and meta["run_id"] != self.run_id:
                if not self.preview:
                    raise SchemaInvalid("Scientific domain input belongs to another Run")
                meta = None
            self.metadata[ref] = meta
        return self.metadata[ref]

    def fields(self, ref, meta):
        # Keep broad catalog queries cheap. A concrete binding will inspect the entire assigned JSON.
        if self.preview and meta["bytes"] > 1024 * 1024:
            return {}
        if ref not in self.documents:
            try:
                value = self.artifacts.read_json(ref)
            except SchemaInvalid:
                value = None
            except (ArtifactHashMismatch, ArtifactNotFound):
                if not self.preview:
                    raise
                value = None
            self.documents[ref] = value if isinstance(value, dict) else {}
        return self.documents[ref]


def assess(domains, inputs, facts):
    if not domains:
        return {"status": "unknown", "reason": "no_versioned_domain"}
    missing = []
    excluded = []
    for scope, domain in domains.items():
        for role, rule in domain["inputs"].items():
            label = f"{scope}.{role}"
            ref = inputs.get(role)
            if not ref:
                missing.append(label)
                continue
            meta = facts.describe(ref)
            if meta is None:
                missing.append(label)
                continue
            if "sha256" in rule and meta["sha256"] not in rule["sha256"]:
                excluded.append(label + ".sha256")
            if "media_types" in rule:
                mime = meta.get("media_type")
                if mime not in rule["media_types"]:
                    if mime in (None, "application/octet-stream"):
                        missing.append(label + ".media_type")
                    else:
                        excluded.append(label + ".media_type")
            if "json_fields" in rule:
                observed = facts.fields(ref, meta)
                for key, limits in rule["json_fields"].items():
                    if observed.get(key) is None:
                        missing.append(f"{label}.{key}")
                    elif not _matches(observed[key], limits):
                        excluded.append(f"{label}.{key}")
    return {"status": "unsupported" if excluded else "unknown" if missing else "supported",
            "missing": missing, "excluded": excluded}


def require(domains, inputs, facts, *, resolved):
    result = assess(domains, inputs, facts)
    if result["status"] == "unsupported" or (resolved and result["status"] == "unknown"):
        raise SchemaInvalid("Input does not establish the declared scientific domain", details={
            "reason": "SCIENTIFIC_INPUT_DOMAIN", **result})
