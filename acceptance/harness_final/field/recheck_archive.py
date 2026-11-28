"""Independent SQL cross-check of the yeast records; accepts a mounted snapshot directory."""
import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3


def read(path):
    con = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return closing(con)


def recheck(root):
    with ExitStack() as stack:
        dbs = {name:stack.enter_context(read(root / rel)) for name, rel in {
            "names":"indexes/reaction-names/rhea139-142.sqlite",
            "rx":"indexes/rhea142-sprot2026_03/lookup.sqlite",
            "enz":"indexes/enzyme-food-20260128-sprot2026_03/lookup.sqlite",
            "assay":"kinetics/brenda2026.1-sabio20260920-v1.sqlite"}.items()}
        result = {}
        for substrate, master, ec_panel in [("maltose",68796,"3.2.1.20"),("trehalose",32675,"3.2.1.28")]:
            reaction = dbs["names"].execute("SELECT master,definition,ec FROM reaction WHERE master=?", (master,)).fetchone()
            direct = dbs["rx"].execute("""SELECT DISTINCT p.accession,a.source_line FROM association a
                JOIN protein p USING(accession) WHERE a.master=? AND p.taxid=559292 ORDER BY 1,2""",(master,)).fetchall()
            group = dbs["enz"].execute("""SELECT DISTINCT p.accession,d.source_line FROM dr d
                JOIN alias b ON b.alias=d.source_ac JOIN protein p USING(accession)
                WHERE d.ec=? AND p.taxid=559292 ORDER BY 1,2""",(ec_panel,)).fetchall()
            accessions = set(r["accession"] for r in [*direct,*group])
            assay = dbs["assay"].execute("""SELECT source,entry_id,
                json_extract(body,'$.parameter_field') field,json_extract(body,'$.original.value') original_value,
                json_extract(body,'$.accessions') accessions FROM assays
                WHERE ec=? AND EXISTS (SELECT 1 FROM json_each(assays.body,'$.accessions') a
                    WHERE a.value IN (SELECT value FROM json_each(?))) ORDER BY 1,2""",
                (ec_panel,json.dumps(sorted(accessions)))).fetchall()
            result[substrate] = {"taxid":559292,"reaction":dict(reaction),"panel_ec":ec_panel,
                                 "exact":list(map(dict,direct)),"class":list(map(dict,group)),
                                 "archive_assays":list(map(dict,assay))}
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive",type=Path)
    print(json.dumps(recheck(p.parse_args().archive),ensure_ascii=False,indent=2))
