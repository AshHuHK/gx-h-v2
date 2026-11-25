"""Rebuild the qualification from frozen public records in a new temporary directory."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile

from archive import fetch, sha, write
from qualify import run

HERE = Path(__file__).resolve().parent


def verify(work, acquire=False):
    lock = json.loads((HERE/"receipts/archives.json").read_bytes())
    proof = json.loads((HERE/"receipts/screening.json").read_bytes())
    if lock["registration"] != proof["freeze"] or sha(HERE/"screening_protocol.json") != proof["freeze"]["protocol_sha256"]:
        raise ValueError("Published freeze does not match rule bytes")
    captured = {}
    entries = {}
    with tempfile.TemporaryDirectory(prefix="gx-custody-review-") as temp, ZipFile(HERE/"receipts/search-snapshot.zip") as meta:
        copy = Path(temp)
        (copy/"snapshots").mkdir()
        write(copy/"00-freeze.json", lock["registration"])
        write(copy/"search.json", lock["search_receipt"])
        for name, source in lock["files"].items():
            p = work/"snapshots"/name
            dest = copy/"snapshots"/name
            if not p.is_file() and name in meta.namelist() and name.endswith(".json"):
                dest.write_bytes(meta.read(name))
                p = dest
            elif acquire and not p.is_file():
                p = fetch(work,name,source["url"])
            if not p.is_file() or sha(p) != source["sha256"] or p.stat().st_size != source["bytes"]:
                raise ValueError("Retrieve frozen public revision or supply original capture: "+name)
            if not source["at"] > lock["registration"]["sealed_at"]:
                raise ValueError("Source predates the screening freeze")
            if p != dest:
                shutil.copy2(p,dest)
            write(copy/"snapshots"/(name+".http.json"),source)
            if name.startswith("entries-"):
                for e in json.loads(p.read_bytes())["data"]["entries"]:
                    if e["rcsb_id"] in entries:
                        raise ValueError("A polymer entry was duplicated")
                    entries[e["rcsb_id"]] = e
            elif name.endswith(".json"):
                captured[name] = json.loads(p.read_bytes())
        sources={}
        for k,s in lock["search_receipt"]["sources"].items():
            if s["status"]!="OK":
                continue
            name = k+".json"
            if lock["files"][name]["url"]!=s["url"] or lock["files"][name]["sha256"] != s["sha256"]:
                raise ValueError("Frozen query does not match its original record")
            result=captured[name]
            if k.startswith("search-"):
                ids=[a["identifier"] for a in result.get("result_set",[])]
                if len(ids) != result.get("total_count",0):
                    raise ValueError("Incomplete public entry search response")
            elif k.startswith("pdbe-"):
                ids=[a["pdb_id"] for a in result.get(k[5:],[])]
            else:
                ids=[ref["id"] for r in result["results"] for ref in r.get("uniProtKBCrossReferences",[])
                     if ref["database"]=="PDB"]
            for id in ids:
                sources.setdefault(id.upper(),set()).add(k)
        if {k:sorted(v) for k,v in sources.items()}!=lock["search_receipt"]["candidates"]:
            raise ValueError("Input set differs from complete archived search union")
        write(copy/"entries.json",entries)
        run(copy)
        target_rows = json.loads((copy/"screening.json").read_bytes())["target_entries"]
        pdbe_chains = {}
        for row in captured["pdbe-O95150.json"]["O95150"]:
            pdbe_chains.setdefault(row["pdb_id"].upper(), set()).add(row["chain_id"])
        if {k:len(v) for k,v in pdbe_chains.items()} != {r["entry"]:r["actual_modelled_copy_count"] for r in target_rows}:
            raise ValueError("Target copy counts disagree with the independent archive mapping")
        for k in ("eligibility","screening"):
            if sha(copy/(k+".json"))!=lock[k+"_sha256"] or (copy/(k+".json")).read_bytes()!=(HERE/"receipts"/(k+".json")).read_bytes():
                raise ValueError("Reconstructed qualification differs: "+k)
        print("Verified frozen search, actual modeled chains and signed-off exclusion scope:",sha(copy/"eligibility.json"))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True,help="Custody directory containing original snapshots or a fresh retrieval cache")
    parser.add_argument("--acquire",action="store_true",help="Retrieve missing original records over Clash. A revision mismatch stops replay.")
    args=parser.parse_args()
    verify(args.work.resolve(),args.acquire)
