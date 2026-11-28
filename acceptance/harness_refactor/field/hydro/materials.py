"""Reproduce the USGS recording windows, portable inputs and independent volume checks."""
import argparse
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

HERE = Path(__file__).resolve().parent
PLAN = json.loads((HERE / "study.json").read_bytes())


def write(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_url(case):
    definition = PLAN[case]
    return "https://waterservices.usgs.gov/nwis/dv/?" + urlencode({
        "format": "json", "sites": ",".join(definition["sites"]), "parameterCd": "00060",
        "startDT": definition["start"], "endDT": definition["end"], "statCd": "00003", "siteStatus": "all"})


def prepare(case, fetch):
    definition, case_dir = PLAN[case], HERE/case
    raw = case_dir/"usgs.json"
    if fetch and not raw.exists():
        proxy = "http://127.0.0.1:7890"
        for name in ("no_proxy", "NO_PROXY"):
            if any(item.strip() not in {"", "127.0.0.1", "::1", "localhost"} for item in os.getenv(name, "").split(",")):
                raise RuntimeError("Only loopback addresses may bypass the proxy")
        for port in (7890, 7891):
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open(source_url(case), timeout=30) as response:
            content = response.read()
        json.loads(content)
        raw.write_bytes(content)
    raw_doc = json.loads(raw.read_bytes())
    d0, d1 = date.fromisoformat(definition["start"]), date.fromisoformat(definition["end"])
    days = (d1-d0).days + 1
    rates = {}
    intervals = []
    names = {}
    scale = Decimal(PLAN["unit_conversion_L_per_ft3"])
    stress = Decimal(PLAN["sensitivity_bound"])
    for series in raw_doc["value"]["timeSeries"]:
        site = series["sourceInfo"]["siteCode"][0]["value"]
        assert site in definition["sites"] and site not in rates
        v = series["variable"]
        assert v["variableCode"][0]["value"] == "00060" and v["unit"]["unitCode"] == "ft3/s"
        assert v["options"]["option"][0]["optionCode"] == "00003"
        names[site] = series["sourceInfo"]["siteName"]
        dates = rates[site] = {}
        for row in series["values"][0]["value"]:
            day = date.fromisoformat(row["dateTime"][:10])
            assert day not in dates and d0 <= day <= d1
            flow = Decimal(row["value"])
            if "A" not in row["qualifiers"] or flow < 0:
                continue
            dates[day] = flow
            start = (day-d0).days*1440
            intervals.append({"channel": site,"start": start,"end": start+1440,"valid": True,
                              "q_L_s": float(flow*scale),"err_L_s": float(flow*scale*stress)})
    first, second = definition["sites"]
    shared = sorted(set(rates[first]) & set(rates[second]))
    totals = [sum((rates[s][day]*86400 for day in shared), Decimal(0))*scale for s in (first,second)]
    gap = totals[0]-totals[1]
    uncertainty = stress*sum(totals)
    record = {"schema":"gx.hydrometry/1","source":source_url(case),"raw_sha256":digest(raw),
              "trial":"USGS " + "/".join(definition["sites"]) + " " + definition["start"] + ".." + definition["end"],
              "window_dates":[str(d0),str(d1)],"window_minutes":[0,days*1440],
              "channels":definition["sites"],"station_names":names,
              "units":{"time":"minutes from start of first reported day","flow":"litres per second"},
              "calibration":"Independent bounds of plus or minus 25% on the approved daily means are the editor's sensitivity scenario. These are not USGS measurement-error estimates.",
              "intervals":intervals}
    write(case_dir/"discharge.json",record)
    write(case_dir/"reference.json",{"input":digest(raw),"reported_unit":"ft3/s","source":source_url(case),
        "shared_dates":[str(d) for d in shared],"volumes_L":list(map(str,totals)),"lower_L":str(gap-uncertainty),
        "upper_L":str(gap+uncertainty),"rounding_relative_tolerance":"1e-9","rounding_absolute_tolerance_L":"0.01"})
    inputs = [{"role":role,"path":path.name,"sha256":digest(path)}
              for role,path in (("record",case_dir/"discharge.json"),("upstream_original",raw))]
    write(case_dir/"goal.json",{"project":"river-archive-comparability","research_effort":"deep",
        "budget_profile":"oauth_research","input_files":inputs})


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("a","b"))
    parser.add_argument("--fetch-missing", action="store_true")
    args=parser.parse_args()
    prepare(args.case,args.fetch_missing)
