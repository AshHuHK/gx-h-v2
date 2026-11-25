"""Read entity identity and declared chains for the exact frozen search union."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlencode

from archive import fetch, write

FIELDS = """
rcsb_id rcsb_accession_info {initial_release_date}
struct {title} exptl {method}
polymer_entities {
 rcsb_id rcsb_polymer_entity {pdbx_description}
 rcsb_entity_source_organism {ncbi_taxonomy_id}
 rcsb_polymer_entity_container_identifiers {entity_id asym_ids reference_sequence_identifiers {database_name database_accession}}
 entity_poly {pdbx_seq_one_letter_code_can}
}
"""


def run(work):
    found = sorted(json.loads((work/"search.json").read_bytes())["candidates"])
    all_entries = {}
    for offset in range(0, len(found), 24):
        ids = found[offset:offset+24]
        q = "{entries(entry_ids:" + json.dumps(ids) + "){" + FIELDS + "}}"
        url = "https://data.rcsb.org/graphql?" + urlencode({"query":q})
        raw = fetch(work, f"entries-{offset:03d}.json", url)
        result = json.loads(raw.read_bytes())
        if result.get("errors"):
            raise ValueError(result["errors"])
        for entry in result["data"]["entries"]:
            all_entries[entry["rcsb_id"]] = entry
    if set(all_entries) != set(found):
        raise ValueError("Public entry metadata incomplete; preserve missing entries for follow-up")
    write(work/"entries.json", all_entries)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    run(parser.parse_args().work)
