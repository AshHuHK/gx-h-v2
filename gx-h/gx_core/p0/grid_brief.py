"""Small frozen inputs for science_grid@3 worker briefs."""
import json

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from . import compute


PREFIX = "GX task brief\n"
PRELOAD_BYTES = 16 * 1024
GUIDANCE = (
    "The user task brief includes the current Intent, assigned sources and verified Artifact contents "
    "under preloaded_artifacts. Use those contents directly as research data. Read missing originals "
    "with gx_read_artifact, including raw observations and source documents. Use gx_task_context "
    "after an Intent or source notification, or when the brief lacks current context."
)


def applies(run, task_input):
    return (run["workflow_id"] == "science_grid" and run["workflow_version"] == "3"
            and task_input.get("output_kind") in ("science_assessment", "science_decision"))


def includes_artifacts(brief):
    # This is an attempt's stored packet. Runs created before this transport
    # change can still resume their original delivery, including @3 attempts.
    return (brief.startswith(PREFIX) and '"preloaded_artifacts":' in brief
            and "preloaded_artifacts" in json.loads(brief[len(PREFIX):]))


def _source_refs(sources):
    # The document, its individual segments and its provenance are available
    # through the cited original; live context also serves older unbacked notes.
    return [{key: value for key, value in source.items()
             if key not in ("text", "segments", "provenance")} for source in sources]


def render(root, root_key, packet):
    from .roots import _artifact_refs

    inputs = packet["input"].copy()
    packet["input"] = inputs
    packet["supplement_sources"] = _source_refs(packet["supplement_sources"])
    if "supplement_sources" in inputs:
        inputs["supplement_sources"] = _source_refs(inputs["supplement_sources"])
    context = packet.get("workflow_context", {})
    if context.get("instruction") and context["instruction"] in inputs.get("instruction", ""):
        del context["instruction"]
    measurements = context.get("grid_measurements")
    if isinstance(measurements, dict) and all(inputs.get(key) == value for key, value in measurements.items()):
        del context["grid_measurements"]
    if not context:
        packet.pop("workflow_context", None)

    # Roots has already granted references from the assignment and the actual
    # workflow hook's citations. Prioritize direct products before nested ones.
    summaries = {ref: kind for field, kind in (
        ("proposal_ref", "research_proposal"), ("evidence_refs", "research_evidence"),
        ("assessment_ref", "science_assessment"), ("previous_assessment_ref", "science_assessment")
    ) for ref in _artifact_refs(inputs.get(field))}
    other_refs = _artifact_refs([inputs.get("product_contract", {}).get("schema_ref"),
                                 inputs.get("supplement_sources"), packet["supplement_sources"],
                                 inputs.get("intent_items"), packet["intent"]])
    pending = list(dict.fromkeys([*summaries, *other_refs]))
    binding = {"root_key": root_key}
    assets = ArtifactService(root.conn)
    preloaded, lazy = {}, {}
    source_bytes, payload_bytes = 0, 2  # braces of the preloaded_artifacts object
    for ref in pending:
        if ref in preloaded or ref in lazy:
            continue
        try:
            meta = root._artifact_access(binding, ref, metadata=True)
        except ScopeDenied:
            # A citation is visible in its parent, but can name a different
            # Run or a missing record. Never disclose unassigned metadata.
            lazy[ref] = {"reason": "unavailable_to_task"}
            continue
        digest, size = meta["sha256"], meta["bytes"]
        media = (meta["media_type"] or "").partition(";")[0].strip().lower()
        if size > PRELOAD_BYTES:
            reason = "item_byte_limit"
        elif (meta["kind"] != summaries.get(ref) or compute.is_analysis_data(root.conn, ref)
              or not (media == "application/json" or media.endswith("+json"))):
            reason = "original_on_demand"
        elif source_bytes + size > PRELOAD_BYTES:
            reason = "total_byte_limit"
        else:
            data = assets.read_bytes(ref, max_bytes=PRELOAD_BYTES, expected_sha256=digest)
            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SchemaInvalid(f"invalid JSON in assigned Artifact {ref}") from exc
            entry = {"sha256": digest, "content": body}
            added = len(ids.canonical_json({ref: entry}).encode("utf-8")) - 2 + bool(preloaded)
            if payload_bytes + added <= PRELOAD_BYTES:
                preloaded[ref] = entry
                source_bytes += size
                payload_bytes += added
                # Same transitive access as an explicit read of this summary:
                # preserve direct lazy reads of original observations and inputs.
                root._allow_refs(root_key, body)
                if meta["kind"] == "research_evidence" and isinstance(body, dict):
                    for route in body.get("routes", []):
                        if isinstance(route, dict) and isinstance(route.get("evaluation_ref"), str):
                            summaries[route["evaluation_ref"]] = "score_evaluation"
                pending.extend(_artifact_refs(body))
                continue
            reason = "total_byte_limit"
        lazy[ref] = {"sha256": digest, "reason": reason}

    packet["preloaded_artifacts"] = preloaded
    if lazy:
        packet["lazy_artifacts"] = lazy
    return PREFIX + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
