"""Reconcile the seven historical GX locator tables against a named local mount."""
import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

try:
    from .selection import archive_selections
    from .read_operations import NATIVE_RELATIONS, ARXIV_RELATIONS
except ImportError:
    from selection import archive_selections
    from read_operations import NATIVE_RELATIONS, ARXIV_RELATIONS


# Primary locator, historical identity columns, archive domain vocabulary.
TABLES = {
    "research": ("implementation", ("line", "resource")),
    "mcp-servers": ("catalog_or_registry", ("server_id",)),
    "mcp-tools": ("schema_file", ("server_id", "tool_name")),
    "workflows": ("source_file", ("family", "kind", "id")),
    "methods": ("manifest_file", ("line", "id", "version", "status_or_parent")),
    "data": ("source_path", ("collection", "id")),
    "skills": ("skill_file", ("collection", "module_or_batch", "skill_id")),
}
DOMAIN_TERMS = {
    "antibody": r"antibod|ofantibody|immunoglobulin|anarci|imgt|sabdab|abnativ|(?:^|[^a-z])(?:fab|vhh|cdr|fv)(?:[^a-z]|$)|抗[体體]",
    "cyclic-peptide": r"cyclic.?pept|cyclic[_ -]|cycpep|rfpept|macrocyc|(?:环|環)肽|cpsea",
    "peptide": r"peptid|rfpeptid|多肽",
    "enzyme": r"enzym|zyme|酶|brenda|sabio|horizyn|rhea|cataly|swiss.?prot|kinetics|hydrolase|bglb",
    "rna": r"(?:rna|rfam|rmsa|nucleic|ribo|核酸|eternafold|mxfold|bpfold|ufold|linearfold)",
    "protein": r"protein|蛋白|pdb|mmcif|uniprot|sifts|alphafold|protenix|pcna|esmfold|rosetta|mpnn|rfdiffusion|alphaprotein|proteina",
    "research": r"literature|journal|pubmed|arxiv|pmc|crossref|学术|學術|文献|文獻|文章|openalex|academic|literatur|publications",
}
SMALL_SOURCE = 4 * 1024 * 1024


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def mounted(root, value):
    relative = Path(value)
    if not value or relative.is_absolute() or ".." in relative.parts:
        return None
    return root / relative


@lru_cache(maxsize=None)
def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@lru_cache(maxsize=None)
def parsed(path):
    return json.loads(path.read_bytes())


def pointer(value, text):
    for part in text[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def inspect(root, table, row, selected):
    path = mounted(root, row[TABLES[table][0]])
    if not path:
        return {"status":"invalid_locator"}
    try:
        if path.is_dir():
            next(path.iterdir(), None)
            return {"status":"directory_opened"}
        if not path.is_file():
            return {"status":"missing_source"}
        size = path.stat().st_size
        result = {"status":"file_opened", "bytes":size}
        with path.open("rb") as source:
            source.read(1)
        if size <= SMALL_SOURCE or selected:
            result["sha256"] = file_hash(path)
        if table == "mcp-servers":
            try:
                document = parsed(path)
                if not isinstance(document, dict):
                    return dict(result,status="identity_mismatch")
                tools = document.get("tools")
                if isinstance(tools, list):
                    matches = document.get("server") == row["server_id"] and str(len(tools)) == row.get("n_tools")
                    return dict(result,status="server_tool_count_verified" if matches else "identity_mismatch")
                if row["server_id"] in document.get("servers", {}) and row.get("n_tools") in {"","0"}:
                    return dict(result,status="registry_only_server")
                return dict(result,status="identity_mismatch")
            except (ValueError, TypeError):
                return dict(result,status="unreadable_catalog")
        ptr = row.get("json_pointer")
        if ptr:
            try:
                value = pointer(parsed(path), ptr)
            except (KeyError, IndexError, ValueError, TypeError):
                return dict(result, status="unresolved_pointer")
            result.update(status="json_pointer_verified")
            # For indexed operations compare the identity at the location as well as its existence.
            if table == "mcp-tools":
                if (not isinstance(value, dict) or not isinstance(parsed(path), dict)
                        or value.get("name") != row["tool_name"] or parsed(path).get("server") != row["server_id"]):
                    result["status"] = "identity_mismatch"
                elif selected:
                    input_schema = value.get("inputSchema") or {}
                    output_schema = value.get("outputSchema") or {}
                    result.update(operation_sha256=digest(encoded(value)),
                                  input_properties=list(input_schema.get("properties",{})),
                                  required_inputs=input_schema.get("required",[]),
                                  output_properties=list(output_schema.get("properties",{})),
                                  output_schema_declared=bool(output_schema))
                    revision = parsed(path).get("commit")
                    if isinstance(revision,str) and re.fullmatch(r"[a-f0-9]{40}",revision):
                        result["revision"] = revision
            elif table == "methods" and isinstance(value, dict) and row["version"]:
                identity = value.get("capability_id", value.get("id"))
                if identity is not None and str(identity) != row["id"]:
                    result["status"] = "identity_mismatch"
            if isinstance(value, dict) and re.fullmatch(r"[a-f0-9]{40}", str(value.get("commit", ""))):
                result["revision"] = value["commit"]
        return result
    except (OSError, UnicodeError) as exc:
        return {"status":"unreadable_source","error":type(exc).__name__}


@lru_cache(maxsize=2048)
def source_terms(path, ptr):
    """Classify using descriptive source fields; execution settings stay in the old deployment."""
    if path.suffix.lower() == ".json":
        try:
            if not ptr and path.stat().st_size > SMALL_SOURCE:
                return ""
            document = parsed(path)
            value = pointer(document, ptr) if ptr else document
            fields = []
            if isinstance(value, dict):
                fields += [value.get(k) for k in ("description", "title", "scope", "purpose", "limitations")]
            if isinstance(document, dict) and isinstance(document.get("server_info"), dict):
                fields.append(document["server_info"].get("name"))
            return " ".join(text for text in fields if isinstance(text,str))
        except (ValueError, TypeError, KeyError, IndexError, OSError):
            return ""
    if path.name == "SKILL.md":
        try:
            with path.open("r", encoding="utf-8",errors="replace") as infile:
                opening = infile.read(3500)
            title = re.search(r"^# [^\n]+",opening,re.M)
            description = re.search(r"^description: [^\n]+",opening,re.M)
            return " ".join(m.group(0) for m in (title,description) if m)
        except OSError:
            return ""
    return ""


def disposition(table, domains, selected):
    if selected:
        return "catalog_reference"
    if "rna" in domains and len(domains)==1:
        return "rna_index_deferred"
    if table in {"mcp-tools", "mcp-servers"}:
        return "historical_operation_unqualified"
    if table == "research" or domains == ["research"]:
        return "archival_cross_reference"
    return "indexed_only"


def disposition_reason(table, row, status, source_state):
    if status == "catalog_reference":
        return "reviewed_archive_description"
    if source_state in {"unreadable_source","missing_source","identity_mismatch","unresolved_pointer",
                        "operation_schema_changed","unreadable_catalog","invalid_locator"}:
        return "origin_or_operation_qualification_incomplete"
    if table == "mcp-servers":
        return "component_metadata_not_an_operation"
    if table == "mcp-tools":
        tool = row["tool_name"].lower()
        if tool == "parse_and_execute" or "shell" in tool or "execute_code" in tool:
            return "general_command_execution_outside_inventory"
        if tool in {"cancel_job","list_jobs","get_job_status","get_job_log","get_server_info","get_job_result"}:
            return "service_lifecycle_not_a_scientific_measurement"
        if tool.startswith(("submit_", "create_", "register_", "save_", "upload_", "delete_", "resume_")):
            return "changes_runtime_state_outside_read_only_import"
        if row.get("category") in {"database","literature"}:
            return "specific_operation_or_source_scope_not_curated"
        return "historical_tool_without_reviewed_read_only_operation"
    if status == "rna_index_deferred":
        return "rna_workstream_deferred"
    if table == "skills":
        return "historical_document_without_this_release_skill_binding"
    if table == "workflows":
        if "defer" in row.get("history_status","").lower() or "fail" in row.get("history_status","").lower():
            return "historical_deferred_or_failed_route"
        if row.get("kind") in {"stage","scenario","cases"}:
            return "historical_stage_or_fixture"
        return "workflow_only_indexed_in_this_transfer"
    if table == "data":
        return "directory_lacks_verified_scope_or_release" if source_state=="directory_opened" else "data_scope_or_query_binding_not_curated"
    if table == "methods":
        return "separate_provider_and_scope_review_needed_for_this_transfer"
    return "historical_source_route_only"


def native_relation(spec, product_root):
    if product_root is None:
        return None
    canonical = spec["canonical_method_id"]
    # These sources cover human tissue or interaction records. The current S288C
    # UniProt adapter cannot look up their archive records.
    if canonical=="curated-protein-description" and spec["binding"].startswith(("ProteinAtlas-", "STRING-db-")):
        return None
    rel = NATIVE_RELATIONS.get(canonical)
    if "arxiv" in spec["binding"].lower():
        rel = ARXIV_RELATIONS.get(canonical,rel)
    if not rel:
        return None
    provider, method_id, scope = rel
    registration = product_root / "gx-h/config/workflow_packages.json"
    package = next((item for item in parsed(registration)["plugins"] if item["path"]==provider),None)
    if not package:
        return None
    for manifest in sorted((product_root / "gx-h/plugins/installed" / provider / "methods").glob("*.json")):
        candidate = parsed(manifest).get("manifest", {})
        if str(candidate.get("capability_id")) + "@" + str(candidate.get("version"))==method_id:
            return {"reference":method_id,"provider":provider.replace("/","@"),"package_sha256":package["sha256"],
                    "manifest_sha256":file_hash(manifest),"scope":scope}
    return None


def make_public(spec, source, table_hash, line, product_root):
    if source["status"] not in {"json_pointer_verified", "directory_opened", "file_opened"}:
        return None
    revision = spec["version"] or source.get("revision")
    origin = {"workspace":"historical-science-inventory", "path":spec["table"]+".tsv",
              "line":line, "sha256":table_hash, "verification":source["status"]}
    if "sha256" in source:
        origin["primary_sha256"] = source["sha256"]
    old_binding = {"id":spec["binding"], "revision":revision, "historical":True}
    card = {k:spec[k] for k in ("reference","canonical_method_id","kind","title","purpose","aliases",
                                 "inputs","outputs","limitations")}
    card.update(category=spec["kind"], domain=spec["domains"], provider_binding=old_binding, source=origin)
    if "operation_sha256" in source:
        card["archived_operation"] = {k:source[k] for k in (
            "operation_sha256","input_properties","required_inputs","output_properties","output_schema_declared")}
        if spec.get("operation_sha256"):
            card["inputs"] = source["input_properties"]
            card["outputs"] = source["output_properties"]
            if not card["outputs"]:
                card["limitations"] += " No named output fields declared in this source schema."
    relation = native_relation(spec,product_root)
    if relation:
        card["native_relation"] = relation
        card["related_methods"] = relation["reference"] + " • " + relation["scope"]
    if spec.get("license_basis"):
        card["license_basis"] = spec["license_basis"]
    card["digest"] = digest(encoded(card))
    return card


def reconcile(root, table_dir, select=None, *, product_root=None):
    if select is None:
        select = archive_selections()
    file_hash.cache_clear()
    parsed.cache_clear()
    source_terms.cache_clear()
    ledger, cards, summaries, warnings = [], [], {}, []
    for table, (source_col, identity_cols) in TABLES.items():
        path = table_dir / (table + ".tsv")
        try:
            with path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source, delimiter="\t")
                required = {source_col,*identity_cols}
                if not required.issubset(reader.fieldnames or ()):
                    raise ValueError("Missing columns " + repr(sorted(required - set(reader.fieldnames or ()))))
                table_hash = file_hash(path)
                records = []
                while True:
                    first_line = reader.line_num + 1
                    row = next(reader, None)
                    if row is None:
                        break
                    if None in row or None in row.values():
                        raise ValueError(f"Malformed row at line {first_line}")
                    records.append((first_line, row))
        except (OSError, ValueError, UnicodeError) as exc:
            summaries[table] = {"status":"unavailable", "rows":None, "reason":str(exc).replace(str(table_dir),"{inventory}")}
            continue

        keys = Counter()
        reasons = Counter()
        row_hashes, source_keys, statuses, domain_records, decisions = set(), set(), Counter(), Counter(), Counter()
        for line, row in records:
            identity = [row[k] for k in identity_cols]
            key = digest(encoded([table,*identity]))
            keys[key] += 1
            row_sha = digest(encoded(row))
            row_hashes.add(row_sha)
            source_keys.add((row[source_col], row.get("json_pointer","")))
            specs = [s for s in select if s["table"] == table and all(row.get(k)==v for k,v in s["match"].items())]
            if len(specs)>1:
                raise ValueError(f"Conflicting catalogue selection at {table}:{line}")
            spec = specs[0] if specs else None
            scan_fields = ("category", "aliases", "title_or_purpose", "scope_or_review",
                           "scope", "access", "source_groups", "kind")
            anchor = mounted(root, row[source_col])
            details = source_terms(anchor,row.get("json_pointer","")) if anchor else ""
            scan_text = " ".join([*identity, details, *(row.get(k, "") for k in scan_fields)])
            domains = spec["domains"] if spec else [d for d, rx in DOMAIN_TERMS.items() if re.search(rx,scan_text,re.I)]
            domains = domains or ["unclassified"]
            fact = inspect(root, table, row, bool(spec))
            if spec and spec.get("operation_sha256") and fact.get("status")=="json_pointer_verified":
                if fact.get("operation_sha256") != spec["operation_sha256"]:
                    fact["status"] = "operation_schema_changed"
            public = make_public(spec,fact,table_hash,line,product_root) if spec else None
            if public:
                cards.append(public)
            elif spec:
                warnings.append({"reference":spec["reference"],"reason":fact["status"]})
            status = disposition(table,domains,public)
            reason = disposition_reason(table,row,status,fact["status"])
            reasons[reason] += 1
            domain_records.update(domains)
            decisions[status] += 1
            statuses[fact["status"]] += 1
            # Opaque identifiers keep unreviewed runtime paths, commands and personal records out of the ledger.
            entry = {"table":table,"line":line,"row_sha256":row_sha,"identity_sha256":key,"domains":domains,
                     "classification":"curated" if spec else "locator_terms","source_check":fact["status"],
                     "disposition":status, "reason":reason, "canonical_method_id": None,
                     "historical_binding_id": key}
            if spec:
                entry.update(canonical_method_id=spec["canonical_method_id"], provider_binding=spec["binding"])
            if public:
                entry["catalog_ref"] = public["reference"]
            ledger.append(entry)
        summaries[table] = {"status":"read", "sha256":table_hash, "rows":len(records),
                            "distinct_complete_rows":len(row_hashes), "identity_columns":identity_cols,
                            "distinct_historical_keys":len(keys), "distinct_primary_locators":len(source_keys),
                            "source_checks":dict(statuses),"domains":dict(domain_records),"dispositions":dict(decisions),"reasons":dict(reasons)}
    refs = [c["reference"] for c in cards]
    if len(set(refs)) != len(refs):
        raise ValueError("A catalogue selection matched multiple historical rows")
    for spec in select:
        if spec["reference"] not in refs and not any(w["reference"]==spec["reference"] for w in warnings):
            warnings.append({"reference":spec["reference"],"reason":"source_row_unavailable"})
    return {"tables":summaries,"reference_coverage":len(cards),"incomplete_selections":warnings}, ledger, {"cards":cards}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)+"\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True, help="Physical root for the historical ash_proj mount")
    parser.add_argument("--table-root", type=Path, required=True, help="Physical root for the historical-science-inventory mount")
    parser.add_argument("--output", type=Path, required=True, help="Directory for summary.json and ledger.jsonl")
    parser.add_argument("--cards", type=Path, required=True, help="Destination of the portable catalogue")
    args = parser.parse_args()
    summary, ledger, public = reconcile(args.asset_root, args.table_root,product_root=Path(__file__).resolve().parents[3])
    args.output.mkdir(parents=True,exist_ok=True)
    write_json(args.output / "summary.json",summary)
    with (args.output / "ledger.jsonl").open("wb") as f:
        for record in ledger:
            f.write(encoded(record)+b"\n")
    write_json(args.cards,public)
