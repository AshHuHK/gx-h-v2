"""Project USGS daily values for the two installed hydrometric methods, and derive the independent reference."""
import csv
from datetime import date
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITES = ['01646500','01651000']
URL = "https://waterservices.usgs.gov/nwis/dv/?format=json&sites=01646500,01651000&parameterCd=00060&startDT=2023-05-01&endDT=2023-05-31&statCd=00003&siteStatus=all"
SITE_URL = "https://waterservices.usgs.gov/nwis/site/?format=rdb&sites=01646500,01651000&siteOutput=expanded&siteStatus=all"

def write(name,doc):
    (HERE/name).write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n')

def build():
    raw=json.loads((HERE/'daily.json').read_bytes())
    rows=list(csv.DictReader((l for l in (HERE/'sites.rdb').read_text().splitlines() if not l.startswith('#')),delimiter='\t'))
    area={x['site_no']:D(x['drain_area_va']) for x in rows if x['site_no'] in SITES}
    rates,intervals={},[]
    for station in raw['value']['timeSeries']:
        sid=station['sourceInfo']['siteCode'][0]['value']
        assert station['variable']['variableCode'][0]['value']=='00060' and station['variable']['unit']['unitCode']=='ft3/s'
        byday=rates[sid]={}
        for row in station['values'][0]['value']:
            day=date.fromisoformat(row['dateTime'][:10])
            assert day not in byday and day.month==5 and day.year==2023
            assert 'A' in row['qualifiers']
            val=byday[day]=D(row['value']); lit=val*D('28.316846592')
            intervals.append(dict(channel=sid,start=(day.day-1)*1440,end=day.day*1440,valid=True,
                                  q_L_s=float(lit),err_L_s=float(lit*D('.05'))))
    days=sorted(rates[SITES[0]].keys() & rates[SITES[1]].keys())
    assert len(days)==31 and set(area)==set(SITES)
    write('record.json',dict(schema='gx.hydrometry/1',trial='USGS May 2023 daily archive',source=URL,
        raw_sha256=hashlib.sha256((HERE/'daily.json').read_bytes()).hexdigest(),channels=SITES,
        window_minutes=[0,31*1440],units={'flow':'litres per second','time':'minutes'},
        calibration="Desk's independent ±5 percent sensitivity on each observed flow. One cubic foot equals 28.316846592 litres.",intervals=intervals))
    volumes={s:sum(rates[s][d] for d in days)*86400*D('28.316846592') for s in SITES}
    ratio=volumes[SITES[0]]/volumes[SITES[1]]
    norm=ratio*area[SITES[1]]/area[SITES[0]]
    expected={'sites':SITES,'dates':[str(days[0]),str(days[-1])],'matched_days':len(days),'common_minutes':len(days)*1440,
              'volume_L':{s:str(v) for s,v in volumes.items()},'area_mi2':{s:str(v) for s,v in area.items()},
              'raw_ratio':str(ratio),'area_normalized_ratio':str(norm),
              'raw_ratio_bounds':[str(ratio*D('.95')/D('1.05')),str(ratio*D('1.05')/D('.95'))],
              'normalized_ratio_bounds':[str(norm*D('.95')/D('1.05')),str(norm*D('1.05')/D('.95'))],
              'authority':{'flow':URL,'area':SITE_URL},'numerical_tolerance':'Rounding within 1% acceptable; factors/units and interval conclusions must agree.'}
    write('reference.json',expected)

if __name__=='__main__':build()
