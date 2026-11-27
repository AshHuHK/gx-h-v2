"""Verify the 24-hour RMSE comparison independently against the original JATS table."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from xml.etree import ElementTree

from gx_core.artifacts import ArtifactService


def read(database):
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        material = db.execute("""SELECT blob_artifact_ref,blob_sha256,provenance_json FROM sources
            WHERE json_extract(provenance_json,'$.external_source.accession')='PMC13660415'
              AND json_extract(provenance_json,'$.source_kind')='job_acquired'
            ORDER BY rowid LIMIT 1""").fetchone()
        original = ArtifactService(db).read_bytes(material['blob_artifact_ref'], expected_sha256=material['blob_sha256'])
    jats = ElementTree.fromstring(original)
    table = jats.find(".//table-wrap[@id='tbl3']")
    rows = [[" ".join("".join(cell.itertext()).split()) for cell in row] for row in table.findall(".//tr")]
    if rows[0][:3] != ["method", "RMSE", "MBE"] or rows[1][:3] != ["all","old","new"]:
        raise ValueError("Archived table header does not identify RMSE/all")
    selected = {r[0]: r[1] for r in rows if r[0] in ("pm2_5_cf_mean_24 h", "BJ_cf_mean_PARH_24 h")}
    raw = Decimal(selected["pm2_5_cf_mean_24 h"])
    corrected = Decimal(selected["BJ_cf_mean_PARH_24 h"])
    section = jats.find(".//sec[@id='sec3.2']")
    passages = [" ".join("".join(p.itertext()).split()) for p in section.findall("./p")]
    target, = [p for p in passages if "BJ_atm_mean_PARH" in p and "West Philadelphia" in p and "Bias in the raw" in p]
    mod = re.search(r"BJ_atm_mean_PARH had the highest accuracy.*?RMSE = ([\d.]+) [μµ]g/m3", target)
    uncorrected = re.search(r"Bias in the raw.*?RMSE scores of ([\d.]+) [μµ]g/m3", target)
    if not (mod and uncorrected):
        raise ValueError("West Philadelphia passage has no matched raw/corrected pair")
    return {"run_published_input":material['blob_artifact_ref'],"doi":"10.1021/acsestair.5c00443",
            "pmcid":"PMC13660415","sha256":material['blob_sha256'],"table":"tbl3",
            "population":"Villanova collocation, 24-hour means, all periods in the indicated rows",
            "units":"micrograms per cubic metre","metric":"Root mean square error relative to the collocated comparator",
            "column":"RMSE/all","rows":selected,"absolute_reduction":str(raw-corrected),
            "independent_check_of_leads_result":{
                "location":"JATS body/sec3.2, West Philadelphia results; GX paragraph p469",
                "conditions":"Summer 2024 and spring 2025, daily optical observations against collocated gravimetric filters",
                "metric":"RMSE in micrograms per cubic metre","corrected_method":"BJ_atm_mean_PARH",
                "raw":uncorrected.group(1),"corrected":mod.group(1),
                "absolute_reduction":str(Decimal(uncorrected.group(1))-Decimal(mod.group(1)))},
            "measurement_scope":"Independent table reading and arithmetic. The paper does not establish the sign "
                                "of a daily concentration difference between the proposed coastal and inland sites."}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database",type=Path)
    print(json.dumps(read(parser.parse_args().database),ensure_ascii=False,indent=2))
