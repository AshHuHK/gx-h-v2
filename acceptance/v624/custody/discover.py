"""Capture public search snapshots and the complete union before inspecting coordinate eligibility."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from archive import fetch, now, sha, write

UNIPROT = "https://rest.uniprot.org/uniprotkb/search?"
SEARCH = "https://search.rcsb.org/rcsbsearch/v2/query?"


def run(work):
    seal = json.loads((work / "00-freeze.json").read_bytes())
    if seal["protocol_sha256"] != sha(Path(__file__).with_name("screening_protocol.json")):
        raise ValueError("Screening rules changed after registration")
    sources = {}
    def query(name, url):
        try:
            value = fetch(work, name + ".json", url)
        except RuntimeError:
            sources[name] = {"status": "UNAVAILABLE", "url": url}
            return {}
        sources[name] = {"status": "OK", "url": url, "sha256": sha(value)}
        return json.loads(value.read_bytes())

    data = query("uniprot-target", UNIPROT + urlencode({"query":"(gene_exact:TNFSF15) AND (organism_id:9606) AND reviewed:true",
                                                       "format":"json","fields":"accession,gene_names,xref_pdb"}))
    accessions = {"Q8NFE9"}
    found = {}
    def add(id, source):
        found.setdefault(id.upper(), set()).add(source)
    for entity in data.get("results", []):
        accessions.add(entity["primaryAccession"])
        for ref in entity.get("uniProtKBCrossReferences", []):
            if ref["database"] == "PDB":
                add(ref["id"], "uniprot-target")
    for acc in sorted(accessions):
        route = "pdbe-" + acc
        records = query(route, "https://www.ebi.ac.uk/pdbe/api/mappings/best_structures/" + acc)
        for row in records.get(acc, []):
            add(row["pdb_id"],route)
    for term in ("TL1A","TNFSF15","tumor necrosis factor ligand superfamily member 15"):
        route = "search-" + term.replace(" ", "_")
        req = {"query":{"type":"terminal","service":"full_text","parameters":{"value":term}},
               "return_type":"entry", "request_options":{"return_all_hits":True,"results_content_type":["experimental"]}}
        response = query(route, SEARCH + urlencode({"json":json.dumps(req, separators=(",",":"))}))
        for row in response.get("result_set", []):
            add(row["identifier"], route)
    write(work/"search.json", {"sources":sources,"at":now(),"uniprot":sorted(accessions),
                               "candidates":{k:sorted(v) for k,v in sorted(found.items())}})
    print({k:v["status"] for k,v in sources.items()}, "candidates",len(found))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    run(parser.parse_args().work)
