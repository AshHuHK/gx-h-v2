"""Turn a verified local build result into a source-bound CPU method in the same Goal."""
from .template.coverage import FIELDS, validate_fields

BUILDER = "derive-record-coverage@1"
LIMITATIONS = ("Coverage of unique matching identifiers in these supplied record snapshots. Missing evidence or an "
               "unmatched identifier is not a negative biological observation. Linking across identifier namespaces "
               "requires an explicit mapping upstream. Coverage does not measure annotation quality or independence.")


def install(ctx, args):
    ref = args["build_ref"]
    ctx.job_output(ref, BUILDER, "method-spec.json")
    spec = ctx.read_json(ref)
    method, version = spec["method"].split("@")
    package_id = "org.gx." + method
    base = "{harness}/plugins/installed/" + package_id + "/1.0.0/"
    args_template = ["{env.python}", "-I", "-B", "{entry_script}", "--cohort", "{in.cohort}",
                     "--evidence", "{in.evidence}", "--spec", "{data.0}", "--out", "{out.dir}"]
    denominator = "Distinct cohort IDs after outer whitespace normalization, case-sensitive; all cohort IDs without a located evidence value remain in scope."
    card = {"family": "record_evidence_coverage", "mechanism": "Join cohort." + spec["fields"]["cohort_id"] +
            " to evidence." + spec["fields"]["evidence_id"] + " and count each ID with a nonempty " +
            spec["fields"]["evidence_value"] + " once.", "objective": "How many declared cohort IDs have evidence?",
            "estimator": denominator, "training_data_lineage": "Source SHA256 and exact line/quote in method-spec.json; independent local arithmetic."}
    scope = {"cohort_sha256": "/cohort_sha256", "evidence_sha256": "/evidence_sha256"}
    coverage = {"reader": "json/1", "op": "ratio/1", "numerator": "/numerator", "denominator": "/denominator",
                "unit": "1", "places": 3, "bounds": [0, 1], "scope": scope}
    manifest = {"capability_id": method, "version": version, "kind": "SCIENTIFIC_TOOL",
                "env_id": "control-plane-python", "entry": {"argv_template": args_template},
                "weights": [], "required_data": [base + "method-spec.json"], "method_card": card,
                "source": {"path": base + "coverage.py", "sha256": spec["engine_sha256"]},
                "licence": "GX release implementation", "licence_basis": "Standard-library set arithmetic implemented in this release.",
                "limitations": LIMITATIONS, "resources": {"cpu_threads": 1, "wall_seconds": 15},
                "outputs": [{"name": "coverage.json", "kind": "file:record_coverage", "nature": "analysis",
                             "source_roles": ["cohort", "evidence"], "media_type": "application/json",
                             "required": True, "metric_specs": {"coverage": coverage}}]}
    package = {"id": package_id, "version": "1.0.0", "api_version": "1.0", "kind": "bundle",
               "entrypoint": "plugin:register", "gx_compat": ">=3.0,<4.0", "dependencies": [],
               "extensions": [{"id": method, "contract": "capability", "version": "1"}]}
    result = ctx.install_analysis_template(build_ref=ref, capability=spec["method"],
                 scripts={"coverage.py": "template/coverage.py", "plugin.py": "template/registration.py"},
                 documents={"gx_plugin.json": package, "method.json": {"manifest": manifest, "entry_script": "coverage.py"},
                            "method-spec.json": spec}, expected_revision=args["expected_revision"])
    return dict(result, calibration=spec["calibration"], source=spec["source"])


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Provide one UTF-8 method source artifact")
    if set(parameters) != {*FIELDS, "source_quote"}:
        raise ValueError("Supply the record field mapping and a unique exact source_quote")
    validate_fields({key: parameters[key] for key in FIELDS})
    quote = parameters["source_quote"]
    if not isinstance(quote, str) or not 12 <= len(quote.strip()) <= 240:
        raise ValueError("source_quote requires 12–240 characters from the cited text")


def register(ctx):
    ctx.capabilities.add_manifest("methods/builder.json")
    ctx.goal_methods.register(BUILDER, validate=validate)
    ctx.goal_tools.register(name="gx_install_record_method", capability=BUILDER, handler=install,
        description="Install a calibrated method produced by a completed derive-record-coverage@1 Job in this Goal. "
                    "Pass its named method-spec.json artifact (not the node summary) and the current capability view revision. "
                    "Use the new method in a following decision with cohort and evidence inputs.",
        schema={"type": "object", "additionalProperties": False, "required": ["build_ref", "expected_revision"],
                "properties": {"build_ref": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 0}}})
