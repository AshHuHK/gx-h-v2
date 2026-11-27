"""Report presentation recorded by Main and frozen with the enacted closing decision."""
import json
import re
from collections import Counter

from ..errors import SchemaInvalid

LANGUAGES = {"zh": "Chinese", "en": "English"}
HAN = re.compile(r"[\u3400-\u9fff]")
REPORT = re.compile(r"报告|報告|\breports?\b", re.I)
NAMES = {"zh": re.compile(r"中文|汉语|漢語|\bChinese\b", re.I),
         "en": re.compile(r"英文|英语|英語|\bEnglish\b", re.I)}

NEGATION = re.compile(r"不要|别用|不用|不使用|不想|不需要|不再|別用|別再|\b(?:not|never|avoid|instead of)\b", re.I)


def requested(quote, selected=None):
    """Main classifies the preference. Interpret unambiguous older preference receipts too."""
    if REPORT.search(quote):
        mentioned = [lang for lang, name in NAMES.items() if name.search(quote)]
        if selected in mentioned:
            clauses = re.split(r"[,，；;。]|\bbut\b", quote, flags=re.I)
            if any(NAMES[selected].search(part) and not NEGATION.search(part) for part in clauses):
                return selected
        elif selected is None and len(mentioned) == 1 and not NEGATION.search(quote):
            return mentioned[0]
    if selected is not None:
        raise SchemaInvalid("Select the report language explicitly requested in this PI clause")
    return None


def current(conn, rid):
    """Project-conversation preferences persist. A single report request belongs to its original Run."""
    rows = conn.execute("""SELECT c.value,e.subject_id,e.run_id FROM events e JOIN runs r USING(run_id)
        JOIN p0_operator_messages m ON m.run_id=e.run_id AND m.message_id=e.subject_id
        JOIN json_each(e.payload_json,'$.clauses') c
        WHERE e.kind='pi.message.routed' AND r.project_ref=(SELECT project_ref FROM runs WHERE run_id=?)
        AND json_extract(c.value,'$.kind')='preference' AND json_extract(c.value,'$.status')='recorded'
        AND (json_extract(c.value,'$.persistence')='conversation' OR
             (e.run_id=? AND json_extract(c.value,'$.goal_version')=(SELECT goal_version FROM v6_goals WHERE run_id=?)))
        ORDER BY m.rowid DESC,c.key DESC""", (rid, rid, rid))
    for raw, mid, source_run in rows:
        clause = json.loads(raw)
        if clause.get("branch_id") not in (None, "run"):
            continue
        lang = clause.get("report_language") or requested(clause["quote"])
        if lang:
            return {"language": lang, "source_run_id": source_run, "source_message_id": mid}
    return None


def matches(text, lang):
    # A metric selector or GX reference by itself provides no human-readable judgment.
    prose = re.sub(r"\{\{.*?\}\}|(?:gx[a-z]+_|gap:)[a-zA-Z0-9_-]+", "", text)
    if lang == "zh":
        return bool(HAN.search(prose))
    return bool(re.search(r"[A-Za-z]{2,}", prose)) and not HAN.search(prose)


def require_close(preference, decision, op):
    if not preference:
        return
    language = preference["language"]
    fields = [("rationale", decision["rationale"]), ("ops[0].reason", op["reason"])]
    fields.extend((f"ops[0].unresolved[{index}]", item) for index, item in enumerate(op.get("unresolved", [])))
    for field, prose in fields:
        if not matches(prose, language):
            raise SchemaInvalid(f"The PI requested the report in {LANGUAGES[language]}. "
                                f"Write {field} in that language, retaining original evidence references and units.",
                                details={"field": field, "reason": "report_language"})


# Translate the explanatory prose. Keep numerical values, units, named references and literal
# measurement selectors in their original fields. GX binds the underlying decision/evidence itself.
_ATOM = re.compile(r"(?:gx[a-z]+_|gap:)[a-zA-Z0-9_-]+|\{\{metric:[^{}]+\}\}|"
                   r"[+−-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?:/\d+(?:\.\d+)?)?"
                   r"(?:\s*(?:Å[²³]?|°(?:C|F)?|%|[µμmunpkMGT]?(?:Pa|Hz|mol|eV|Da|[VAgmsKL]))"
                   r"(?![A-Za-z]))?")


def verify_reader(source, reader, language):
    if not isinstance(reader, dict) or set(reader) != {"reason", "rationale", "unresolved"}:
        raise SchemaInvalid("Translate the closing reason, rationale and each unresolved item")
    if not isinstance(reader["unresolved"], list) or len(reader["unresolved"]) != len(source["unresolved"]):
        raise SchemaInvalid("Preserve the closing decision's complete list of limitations")
    named = {v["display"] for v in source.get("measurements", []) if v.get("display")}
    for before, after in zip([source["reason"], source["rationale"], *source["unresolved"]],
                             [reader["reason"], reader["rationale"], *reader["unresolved"]]):
        if not isinstance(after, str) or not matches(after, language):
            raise SchemaInvalid(f"Translate the entire field into {LANGUAGES[language]}")
        if (any(before.count(value) != after.count(value) for value in named)
                or Counter(_ATOM.findall(before)) != Counter(_ATOM.findall(after))):
            raise SchemaInvalid("Preserve each original field's numbers, scientific units and references in the reader")
