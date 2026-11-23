"""The frozen assessment contract and inline evaluator for science_grid@3."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math


VERSION = "science_grid@3/assessment"
MAX_INLINE_BYTES = 2 * 1024 * 1024
MAX_INLINE_POINTS = 32_768
MAX_PATHS = 512
MAX_TEXT = 8192
FAILURE_OUTCOMES = {"measurement_failure", "infrastructure_failure", "historical_only"}


class StructuralError(ValueError):
    """The model can repair this payload against the same frozen contract."""

    def __init__(self, pointer, message, contract_digest):
        super().__init__(f"{pointer or '/'}: {message}")
        self.pointer = pointer or "/"
        self.contract_digest = contract_digest


@dataclass(frozen=True)
class ValidationFinding:
    validator: str
    category: str
    message: str
    path_id: str | None = None


class SemanticError(ValueError):
    """A typed issue with evidence, provenance, or scientific interpretation."""

    def __init__(self, finding: ValidationFinding):
        super().__init__(f"{finding.path_id + ': ' if finding.path_id else ''}{finding.message}")
        self.finding = finding


def _require(condition, validator, category, message, path_id=None):
    if not condition:
        raise SemanticError(ValidationFinding(validator, category, message, path_id))


def _number(value):
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT and bool(value.strip())


def _hash(value):
    data = json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class Measurement:
    valid: bool | None
    note: str
    count: int | None = None
    maximum: float | int | None = None
    at_or_above: int | None = None


@dataclass(frozen=True)
class Sample:
    """Only finite, original candidate and observation tuples cross this boundary."""

    candidates: tuple
    observations: tuple
    complete: bool


@dataclass(frozen=True)
class Evaluation:
    input_ref: str
    input_sha256: str
    attempt_id: str
    threshold: float | int
    count: int
    maximum: float | int
    at_or_above: int


@dataclass(frozen=True)
class Path:
    path_id: str
    prediction: str
    expected: bool | None
    input_ref: str
    evidence_ref: str
    observations_ref: str | None
    evaluation_ref: str | None
    eval_id: str | None
    job_id: str
    status: str
    outcome_kind: str
    measurement: Measurement


def validate_path_coverage(proposed: tuple, measured: tuple) -> tuple[ValidationFinding, ...]:
    if (len(proposed) != len(set(proposed)) or len(measured) != len(set(measured))
            or set(proposed) != set(measured)):
        return (ValidationFinding("validate_path_coverage", "provenance",
                                  "Each frozen proposal path needs exactly one matching evidence route."),)
    return ()


def validate_evidence_source_independence(paths: tuple[Path, ...]) -> tuple[ValidationFinding, ...]:
    """A single Job or observation cannot masquerade as different original measurements."""
    for field in ("job_id", "observations_ref", "evaluation_ref", "eval_id"):
        origins = [getattr(path, field) for path in paths if getattr(path, field) is not None]
        if len(origins) != len(set(origins)):
            return (ValidationFinding("validate_evidence_source_independence", "provenance",
                                      f"Two paths cite the same original {field}."),)
    return ()


def validate_measurement_validity(sample: Sample, threshold: int | float) -> Measurement:
    if not sample.complete or not sample.candidates:
        return Measurement(False, "The original grid or measured observations are incomplete or nonfinite.")
    measured = Counter((name, x) for name, x, _score in sample.observations)
    if measured != Counter(sample.candidates):
        return Measurement(False, "The measured candidates do not cover the frozen grid exactly.")
    scores = [score for _name, _x, score in sample.observations]
    return Measurement(True, "The original observations cover the frozen grid.", len(scores),
                       max(scores), sum(score >= threshold for score in scores))


def validate_frozen_evaluation_consistency(summary: Evaluation, measurement: Measurement, *,
        path_id: str, observations_ref: str, observations_digest: str, eval_id: str,
        threshold: float | int) -> Measurement:
    _require(summary.input_ref == observations_ref and summary.input_sha256 == observations_digest
             and summary.attempt_id == eval_id and _number(summary.threshold) and summary.threshold == threshold,
             "validate_frozen_evaluation_consistency", "provenance",
             "The evaluation belongs to a different observation, attempt, digest or frozen threshold.", path_id)
    if measurement.valid and not (
            type(summary.count) is int and summary.count == measurement.count
            and type(summary.at_or_above) is int and summary.at_or_above == measurement.at_or_above
            and _number(summary.maximum) and summary.maximum == measurement.maximum):
        return Measurement(False, "The saved evaluator statistics disagree with the original scores.")
    return measurement


SEMANTIC_VALIDATORS = (validate_path_coverage, validate_evidence_source_independence,
                       validate_measurement_validity, validate_frozen_evaluation_consistency)


def _sample(grid, raw):
    candidates, points = grid.get("candidates"), raw.get("observations")
    if not isinstance(candidates, list) or not isinstance(points, list):
        return Sample((), (), False)
    valid_rows = all(isinstance(row, dict) and _text(row.get("candidate_id")) and _number(row.get("x"))
                     for rows in (candidates, points) for row in rows)
    if not valid_rows or any(not _number(row.get("score")) for row in points):
        return Sample((), (), False)
    return Sample(tuple((c["candidate_id"], c["x"]) for c in candidates),
                  tuple((p["candidate_id"], p["x"], p["score"]) for p in points), True)


def _measure(path, route, artifacts, digests, sizes, deferred, threshold, point_budget):
    path_id = path["id"]
    kind, status = route.get("outcome_kind"), route.get("status")
    if kind in FAILURE_OUTCOMES:
        return Measurement(False, f"The recorded outcome is {kind}."), 0
    if kind is None:
        return Measurement(None, "The route has no frozen measurement-validity verdict."), 0
    _require(kind == "valid_scientific_outcome" and status == "SUCCEEDED"
             and not route.get("evaluation_error") and not route.get("failure"),
             "validate_measurement_validity", "provenance",
             "The recorded scientific outcome conflicts with the Job or evaluation status.", path_id)
    input_ref, obs_ref, eval_ref = path["input_ref"], route.get("observations_ref"), route.get("evaluation_ref")
    references = (input_ref, obs_ref, eval_ref)
    _require(all(_text(ref) and _digest(digests.get(ref)) for ref in references) and _text(route.get("eval_id")),
             "validate_frozen_evaluation_consistency", "provenance",
             "The valid-outcome record lacks an input, original observation, evaluation ID or byte digest.", path_id)
    if any(ref in deferred or sizes.get(ref, 0) > MAX_INLINE_BYTES for ref in references):
        return Measurement(None, "The source exceeds the inline byte limit and requires Job-backed evaluation."), 0
    grid, raw, result = (artifacts.get(ref) for ref in references)
    _require(all(isinstance(doc, dict) for doc in (grid, raw, result)),
             "validate_frozen_evaluation_consistency", "provenance", "A frozen artifact is unavailable.", path_id)
    _require(all(doc.get("method_id") == "fixture-grid" and doc.get("method_version") == "1"
                 and doc.get("synthetic") is True for doc in (grid, raw))
             and raw.get("input_sha256") == digests[input_ref],
             "validate_frozen_evaluation_consistency", "provenance",
             "The original observations do not belong to this synthetic method and frozen grid digest.", path_id)
    arrays = (grid.get("candidates"), raw.get("observations"))
    count = max((len(rows) for rows in arrays if isinstance(rows, list)), default=0)
    if count > point_budget:
        return Measurement(None, "The sample exceeds the inline point limit and requires Job-backed evaluation."), 0
    measurement = validate_measurement_validity(_sample(grid, raw), threshold)
    summary = Evaluation(result.get("input_ref"), result.get("input_sha256"), result.get("attempt_id"),
                         result.get("threshold"), result.get("observations"), result.get("max_score"),
                         result.get("count_at_or_above"))
    return validate_frozen_evaluation_consistency(summary, measurement, path_id=path_id, observations_ref=obs_ref,
               observations_digest=digests[obs_ref], eval_id=route["eval_id"], threshold=threshold), count


def _object(properties, required=None):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties) if required is None else list(required), "properties": properties}


def _string(description):
    return {"type": "string", "minLength": 1, "maxLength": MAX_TEXT, "pattern": r"\S", "description": description}


def _keyed(keys, shape):
    return _object({key: shape for key in keys})


def _pointer(parent, name):
    token = str(name).replace("~", "~0").replace("/", "~1")
    return parent + "/" + token


def _check_shape(value, schema, pointer, digest):
    """Interpret only the three JSON types emitted by this versioned grid contract."""
    def fail(message):
        raise StructuralError(pointer, message, digest)

    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict:
            fail("Expected an object.")
        fields = schema["properties"]
        for name in schema["required"]:
            if name not in value:
                fail(f"Missing required field {name!r}.")
        for name, child in value.items():
            if name not in fields:
                fail(f"Unexpected field {str(name)[:120]!r}.")
            _check_shape(child, fields[name], _pointer(pointer, name), digest)
    elif kind == "array":
        if type(value) is not list or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            fail(f"Expected {schema['minItems']} to {schema['maxItems']} path selections.")
        for index, item in enumerate(value):
            _check_shape(item, schema["items"], _pointer(pointer, index), digest)
        if len(set(value)) != len(value):
            fail("Each selected path may appear once.")
    elif kind == "string":
        if not _text(value):
            fail(f"Expected nonblank text of at most {MAX_TEXT} characters.")
        if "enum" in schema and value not in schema["enum"]:
            fail("Choose a value from the frozen selection.")
    else:
        raise AssertionError(f"Unsupported grid contract type {kind!r}")


def validate_model_schema(product, schema, digest):
    """Check the actual schema saved at dispatch before reading measurement inputs."""
    _check_shape(product, schema, "", digest)


def _exact(actual, expected):
    """Typed equality checks only as much content as the bounded canonical result."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return len(actual) == len(expected) and all(k in actual and _exact(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_exact(a, e) for a, e in zip(actual, expected))
    return actual == expected


@dataclass(frozen=True)
class ProductContract:
    proposal_ref: str
    evidence_refs: tuple[str, ...]
    threshold: float | int
    paths: tuple[Path, ...]
    sources: tuple[tuple[str, str], ...]
    intents: tuple[tuple[str, int], ...]
    requires_interpretation: bool
    digest: str

    @classmethod
    def freeze(cls, proposal_ref, proposal, evidence_by_ref, artifacts_by_ref, *, artifact_digests,
               validator_digest, supplement_sources=(), intent_items=None, requires_interpretation=True,
               artifact_sizes=None, deferred_refs=()):
        """Adapt already-authorized, hash-verified JSON. Retain only immutable facts."""
        def bound(ok, message):
            _require(ok, "validate_path_coverage", "provenance", message)

        bound(_text(proposal_ref) and _digest(validator_digest), "A proposal and pinned validator digest are required.")
        bound(isinstance(proposal, dict) and proposal.get("method") == "fixture-grid@1"
              and _number(proposal.get("threshold")), "The frozen grid requires its method and finite cutoff.")
        bound(type(requires_interpretation) is bool, "Declare the scope of the frozen grid assessment.")
        entries = proposal.get("paths")
        bound(isinstance(entries, list) and 0 < len(entries) <= MAX_PATHS, "The proposal exceeds the grid path bounds or is empty.")
        bound(all(isinstance(p, dict) and _text(p.get("id")) and _text(p.get("input_ref"))
                  and _text(p.get("prediction")) and type(p.get("expected_reaches_threshold")) in (bool, type(None))
                  for p in entries), "The frozen path identifiers, predictions or expectations are malformed.")
        bound(isinstance(evidence_by_ref, dict) and 0 < len(evidence_by_ref) <= len(entries),
              "The assignment must bind at least one evidence record and at most one per route.")
        routes = []
        for ref, document in evidence_by_ref.items():
            bound(_text(ref) and isinstance(document, dict) and document.get("proposal_ref") == proposal_ref,
                  "The evidence cites another proposal.")
            rows = document.get("routes")
            bound(isinstance(rows, list) and rows and len(routes) + len(rows) <= len(entries)
                  and all(isinstance(r, dict) and _text(r.get("path_id")) for r in rows),
                  "The evidence must bind each frozen route exactly once.")
            routes.extend((r["path_id"], ref, r) for r in rows)
        issues = validate_path_coverage(tuple(p["id"] for p in entries), tuple(name for name, _, _ in routes))
        if issues:
            raise SemanticError(issues[0])
        by_path = {name: (ref, route) for name, ref, route in routes}
        paths, remaining = [], MAX_INLINE_POINTS
        for p in entries:
            ref, route = by_path[p["id"]]
            bound(route.get("input_ref") == p["input_ref"] and _text(route.get("job_id"))
                  and _text(route.get("status")) and all(route.get(k) is None or _text(route[k])
                  for k in ("evaluation_ref", "observations_ref", "eval_id", "outcome_kind")),
                  "The route does not bind the proposal input and original Job.")
            measured, count = _measure(p, route, artifacts_by_ref, artifact_digests, artifact_sizes or {},
                                       deferred_refs, proposal["threshold"], remaining)
            remaining -= count
            paths.append(Path(p["id"], p["prediction"], p.get("expected_reaches_threshold"), p["input_ref"],
                              ref, route.get("observations_ref"), route.get("evaluation_ref"), route.get("eval_id"),
                              route["job_id"], route["status"], route.get("outcome_kind") or "unverified", measured))
        frozen_paths = tuple(paths)
        issues = validate_evidence_source_independence(frozen_paths)
        if issues:
            raise SemanticError(issues[0])
        bound(isinstance(supplement_sources, (tuple, list)) and len(supplement_sources) <= MAX_PATHS
              and all(isinstance(s, dict) and _text(s.get("source_id")) and _text(s.get("artifact_ref"))
                      for s in supplement_sources), "The assigned supplementary sources need identifiers and artifacts.")
        sources = tuple(sorted((s["source_id"], s["artifact_ref"]) for s in supplement_sources))
        bound(len(dict(sources)) == len(sources), "The assigned supplement contains a duplicate source ID.")
        items = intent_items or {}
        bound(isinstance(items, dict) and len(items) <= MAX_PATHS and all(
            _text(key) and isinstance(row, dict) and row.get("item_id", key) == key
            and type(row.get("version")) is int and row["version"] > 0 for key, row in items.items()),
            "The review needs the assigned advisory item IDs and versions.")
        intents = tuple(sorted((key, row["version"]) for key, row in items.items()))
        bindings = tuple((asdict(p), tuple(artifact_digests.get(r) for r in
                         (p.evidence_ref, p.input_ref, p.observations_ref, p.evaluation_ref))) for p in frozen_paths)
        validators = tuple(fn.__qualname__ for fn in SEMANTIC_VALIDATORS)
        digest = _hash((VERSION, validators, validator_digest, proposal_ref, proposal["threshold"], bindings,
                        sources, intents, requires_interpretation))
        return cls(proposal_ref, tuple(evidence_by_ref), proposal["threshold"], frozen_paths,
                   sources, intents, requires_interpretation, digest)

    @property
    def requires_review(self):
        return bool(self.requires_interpretation or self.sources or self.intents
                    or any(p.measurement.valid is not True or p.expected is None for p in self.paths)
                    or len({p.input_ref for p in self.paths}) < len(self.paths))

    def schema(self):
        ids = [p.path_id for p in self.paths]
        fields = {"reason": _string("Interpret the original measurement against this prediction."),
                  "risk": _string("Explain a limitation or uncertainty in this interpretation.")}
        if self.requires_interpretation:
            fields["unresolved_question"] = _string("When a scientific question prevents interpretation, identify it here.")
        finding = _object(fields, ("reason", "risk"))
        shape = {"findings_by_path": _keyed(ids, finding)}
        if self.sources:
            selects = {"type": "array", "items": {"type": "string", "enum": ids},
                       "uniqueItems": True, "maxItems": len(ids)}
            review = _object({
                "disposition": {"type": "string", "enum": ["accepted", "pending", "rejected"]},
                "reason": _string("Explain this source's bearing on the compared measurements."),
                "compared_paths": {**selects, "minItems": 1}, "qualifies_paths": {**selects, "minItems": 0}})
            shape["source_reviews_by_id"] = _keyed((key for key, _ in self.sources), review)
        if self.intents:
            shape["intent_reasons_by_id"] = _keyed((key for key, _ in self.intents),
                              _string("Explain how this advisory requirement was applied."))
        return _object(shape)

    def brief(self):
        shape = self.schema()["properties"]
        findings = shape["findings_by_path"]["properties"]
        fields = next(iter(findings.values()))["properties"]
        lines = ["Submit findings_by_path keyed by these exact path IDs: " + json.dumps(list(findings), ensure_ascii=False),
                 " ".join(f"{name}: {spec['description']}" for name, spec in fields.items()),
                 "GX binds the original references and evaluates the finite-grid threshold on valid measurements."]
        if "source_reviews_by_id" in shape:
            lines.append("For source_reviews_by_id cover " + json.dumps(list(dict(self.sources)), ensure_ascii=False)
                         + ". Give disposition, reason, compared_paths and qualifies_paths. Select the measured paths"
                           " you compared. Mark every compared path for which an accepted or pending source prevents"
                           " a supported interpretation. A rejected source has no qualifying paths.")
        if "intent_reasons_by_id" in shape:
            lines.append("For intent_reasons_by_id explain each assigned item: "
                         + json.dumps(list(dict(self.intents)), ensure_ascii=False))
        return " ".join(lines)

    def validate_structure(self, product):
        validate_model_schema(product, self.schema(), self.digest)

    def evaluate(self, product=None):
        if product is None:
            _require(not self.requires_review, "evaluate", "scientific_judgment",
                     "This assessment requires an independent scientific reviewer.")
        else:
            self.validate_structure(product)
        reviews, qualifications = [], {p.path_id: [] for p in self.paths}
        by_path = {p.path_id: p for p in self.paths}
        for key, ref in self.sources:
            review = product["source_reviews_by_id"][key]
            compared, qualifying = review["compared_paths"], review["qualifies_paths"]
            _require(set(qualifying) <= set(compared) and (not qualifying or review["disposition"] != "rejected"),
                     "evaluate", "scientific_judgment", "Only an accepted or pending source can qualify a compared path.")
            refs = dict.fromkeys(ref for path in compared for ref in
                    (by_path[path].evidence_ref, by_path[path].observations_ref, by_path[path].evaluation_ref) if ref)
            reviews.append({"source_id": key, "artifact_ref": ref, "evidence_refs": list(refs),
                            **review, "compared_paths": list(compared), "qualifies_paths": list(qualifying)})
            for path_id in qualifying:
                qualifications[path_id].append(key)
        findings = []
        for p in self.paths:
            measured = p.measurement
            reaches = measured.at_or_above > 0 if measured.valid else None
            if product is None:
                model = {"reason": f"{measured.at_or_above} of {measured.count} sampled scores reached "
                                   f"the frozen threshold {self.threshold:g}.",
                         "risk": "This finding applies only to the measured synthetic grid."}
            else:
                model = product["findings_by_path"][p.path_id]
            supported = reaches == p.expected if (
                reaches is not None and p.expected is not None and not qualifications[p.path_id]
                and "unresolved_question" not in model) else None
            findings.append({"path_id": p.path_id, "proposal_ref": self.proposal_ref, "evidence_ref": p.evidence_ref,
                             "input_ref": p.input_ref, "observations_ref": p.observations_ref,
                             "evaluation_ref": p.evaluation_ref, "job_id": p.job_id, "outcome_kind": p.outcome_kind,
                             "measurement_valid": measured.valid, "measurement_note": measured.note,
                             "reaches_threshold": reaches, "supported": supported,
                             "qualifying_source_ids": qualifications[p.path_id], **model})
        intents = [{"item_id": key, "version": version, "reason": product["intent_reasons_by_id"][key]}
                   for key, version in self.intents]
        return {"proposal_ref": self.proposal_ref, "evidence_refs": list(self.evidence_refs),
                "contract_digest": self.digest, "assessment_mode": "independent_review" if product is not None else "deterministic",
                "findings": findings, "source_reviews": reviews, "intent_responses": intents}

    def validate_expanded(self, body):
        """Rebind a submitted artifact from just its model-authored values."""
        def mismatch():
            return SemanticError(ValidationFinding("validate_expanded", "provenance",
                                 "The persisted assessment disagrees with its frozen contract or evaluator."))
        try:
            if not isinstance(body, dict) or any(
                    not isinstance(body.get(k), list) or len(body[k]) != size for k, size in
                    (("findings", len(self.paths)), ("source_reviews", len(self.sources)), ("intent_responses", len(self.intents)))):
                raise mismatch()
            if body["assessment_mode"] == "deterministic":
                expected = self.evaluate()
            else:
                model = {"findings_by_path": {f["path_id"]: {k: f[k] for k in ("reason", "risk", "unresolved_question") if k in f}
                                              for f in body["findings"]}}
                if self.sources:
                    model["source_reviews_by_id"] = {r["source_id"]: {key: r[key] for key in
                        ("disposition", "reason", "compared_paths", "qualifies_paths")} for r in body["source_reviews"]}
                if self.intents:
                    model["intent_reasons_by_id"] = {r["item_id"]: r["reason"] for r in body["intent_responses"]}
                expected = self.evaluate(model)
            if not _exact(body, expected):
                raise mismatch()
        except (KeyError, TypeError, StructuralError, OverflowError, RecursionError) as exc:
            raise mismatch() from exc
