"""Bounded access to the original 2RE9 source and explicit worker product contracts."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.persistence import store
from workflows.tl1a_geometry import domain


SOURCE_GUIDANCE = ("Start from the verified coordinate-quality summary, source digest, and the deposited examples in "
                   "source_guide. The raw mmCIF is available via gx_read_artifact with byte offset. For a question "
                   "unanswered by the summary or the independent fits, select the relevant locator and request that page. "
                   "The reviewer tool reads the complete original source for each independent fit.")
TEXT = {"type": "string", "minLength": 1, "pattern": r"\S"}
TEXTS = {"type": "array", "minItems": 1, "items": TEXT}
LIMITATIONS = {"oneOf": [TEXT, TEXTS], "description": "One nonblank scientific limitation or a nonempty list of nonblank limitations."}


def source_guide(conn, ref):
    assets = ArtifactService(conn)
    raw = assets.read_bytes(ref)
    locators = []
    atom_fields = []
    sampled = set()
    fields = {b"_entry.id": "PDB entry", b"_exptl.method": "Experimental method",
              b"_refine.ls_d_res_high": "Resolution", b"_atom_site.group_PDB": "Atom-site columns"}
    offset = 0
    for line in raw.splitlines(keepends=True):
        columns = line.split()
        label = fields.get(columns[0]) if columns else None
        if line.startswith(b"_atom_site."):
            atom_fields.append(columns[0].split(b".", 1)[1])
        elif line.startswith(b"ATOM ") and len(sampled) < 3:
            chain = columns[atom_fields.index(b"label_asym_id")].decode("ascii")
            if chain in ("A", "B", "C") and chain not in sampled:
                label = f"First modeled ATOM record for label_asym_id {chain}"
                sampled.add(chain)
        if label:
            locators.append({"label": label, "offset": offset, "excerpt": line.rstrip().decode("ascii")})
        offset += len(line)
    return {"structure_ref": ref, "sha256": assets.describe(ref)["sha256"], "bytes": len(raw), "locators": locators}


def schema_for(kind, plan=None):
    refs = {"proposal_ref": TEXT, "evidence_ref": TEXT}
    if kind == "geometry_plan":
        properties = {"source_ref": TEXT, "protocols": {"type": "array", "minItems": 2, "uniqueItems": True,
                      "items": {"enum": list(domain.PROTOCOLS)}}, "rationale": TEXT,
                      "hypotheses": {"type": "array", "minItems": 2, "items": {"type": "object",
                      "required": ["id", "prediction", "expected"], "properties": {
                      "id": TEXT, "prediction": TEXT, "expected": {"enum": ["stable", "sensitive"]}}}},
                      "criteria": {"type": "object", "required": list(domain.SECTIONS),
                                   "description": "Use the exact fields and units of criteria_template."}}
    elif kind == "geometry_assessment":
        names = [h["id"] for h in plan["hypotheses"]]
        canonical = [h["hypothesis_id"] for h in plan["hypotheses"]]
        finding = {"type": "object", "properties": {"supported": {"type": ["boolean", "null"]}, "reason": TEXT},
                   "required": ["supported", "reason"]}
        properties = {**refs, "status": {"enum": ["stable", "sensitive", "insufficient"]},
                      "checks": {"type": "object", "additionalProperties": TEXT, "description": "Protocol to reviewer check reference."},
                      "findings": {"type": "object", "properties": dict.fromkeys([*names, *canonical], finding),
                                   "additionalProperties": False, "allOf": [{"oneOf": [{"required": [short]},
                                   {"required": [long]}]} for short, long in zip(names, canonical)],
                                   "description": "For each hypothesis use its short ID or its corresponding canonical ID, once."},
                      "limitations": LIMITATIONS}
    else:
        properties = {**refs, "assessment_ref": TEXT, "reason": TEXT, "counterevidence": TEXT,
                      "uncertainty": TEXT, "next_protocols": {"type": "array", "uniqueItems": True,
                      "items": {"enum": list(domain.PROTOCOLS)}},
                      "stop_reason": {"type": ["string", "null"], "description": "A nonblank reason when stopping."}}
    required = [key for key in properties if key != "stop_reason"]
    return {"type": "object", "properties": properties, "required": required}


def add_to_new_tasks(ctx, result):
    if not result.interrupt:
        return result
    assets = ArtifactService(ctx.conn)
    for tid in result.interrupt.tasks:
        row = ctx.conn.execute("SELECT input_json FROM tasks WHERE task_id=? AND status='READY'", (tid,)).fetchone()
        if not row:
            continue
        inputs = store.loads(row, "input_json")
        if "product_schema" in inputs:
            continue
        kind = inputs["output_kind"]
        plan = assets.read_json(inputs["proposal_ref"]) if kind == "geometry_assessment" else None
        inputs["product_schema"] = schema_for(kind, plan)
        inputs["source_guide"] = assets.read_json(inputs["quality_summary_ref"])["source_guide"]
        inputs["instruction"] = SOURCE_GUIDANCE + " " + inputs["instruction"]
        if kind == "geometry_assessment":
            inputs["instruction"] += (" The product_schema lists the short and canonical keys for each hypothesis. "
                                      "Use one of the two keys for each hypothesis exactly once. limitations accepts a string or an array of text.")
        ctx.conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                         (ids.canonical_json(inputs), tid))
    return result
