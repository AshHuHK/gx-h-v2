"""Drive an ordinary Goal and try an actual inherited local method selection."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from acceptance.v622.test_close import CloseStudy
from gx_core.errors import GxError

PHASE = "只查文獻，這是第一階段；第二階段請查酵母資料庫。"
WHOLE = "本題僅限文獻研究。"
METHOD = "yeast-archive-lookup@2"
ROOT = Path(os.getcwd())


def study(directory, question, config=None):
    args = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), "research", "--state", str(directory/"state"),
            "--run-key", "pi-scope", "--goal", question, "--research-effort", "medium", "--timeout", "0.001"]
    if config is not None:
        (directory/"goal.json").write_text(json.dumps(config))
        args.extend(["--goal-config", str(directory/"goal.json")])
    r = subprocess.run(args, env={**os.environ, "GX_CODEX_OFFLINE":"1"}, capture_output=True, text=True)
    if r.returncode not in (0,75):
        raise RuntimeError(r.stdout + r.stderr)
    return CloseStudy(directory, reopen=True)


def open_child(s, lead, key="investigator"):
    opening = s.open_args(key, inputs=[], caps=[], limits={"tokens":10000,"api_calls":3,"worker_tasks":2})
    opening.update(module_id="ad_hoc", focus="Check archive coverage.", expected_delivery="Return a sourced record.")
    scope = s.call(lead, "gx_open_module", opening, "open-" + key)["scope"]
    return s.next_actor(scope)


def run(question, clauses=None):
    if destination := os.environ.get("GX_PI_SCOPE_STATES"):
        Path(destination).mkdir(parents=True, exist_ok=True)
        private = nullcontext(tempfile.mkdtemp(prefix="pi-", dir=destination))
    else:
        private = tempfile.TemporaryDirectory()
    with private as scratch, study(Path(scratch),question) as s:
        lead = s.next_actor("run")
        form = {"kind":"all"}
        if clauses is not None:
            form["pi_method_scope"] = clauses
        s.call(lead, "gx_query_catalog", form, "overview")
        child = open_child(s, lead)
        row = s.conn.execute("SELECT grant_json FROM v62_modules WHERE run_id=? AND scope=?",
                             (s.run_id,child.scope)).fetchone()
        choices = json.loads(row[0]).get("method_options",{})
        try:
            s.call(child, "gx_select_capability", {"action":"enable","method":METHOD,"revision":0}, "select")
            bound = True
            reason = None
        except GxError as exc:
            bound = False
            reason = exc.code
        events = [dict(r) for r in s.conn.execute(
            "SELECT event_id,kind,subject_id FROM events WHERE run_id=? AND kind IN "
            "('research.pi.method_scope','research.catalog.surveyed','v62.module.opened',"
            "'v62.module.method_selected') ORDER BY event_id", (s.run_id,))]
        return {"question":question,"run_id":s.run_id,"method":METHOD,"options":len(choices),"bound":bound,
                "error":reason,"events":events,"native_calls":s.actions,
                **({"state":str(s.state)} if destination else {})}


if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--classified",action="store_true")
    a=p.parse_args()
    q=[(PHASE,[{"kind":"phase","quote":"只查文獻，這是第一階段","targets":["public_sources"]},
               {"kind":"phase","quote":"第二階段請查酵母資料庫","targets":["public_databases"]}]),
       (WHOLE,[{"kind":"only","quote":WHOLE,"targets":["public_sources"]}])]
    print(json.dumps([run(question,cs if a.classified else None) for question,cs in q],ensure_ascii=False,indent=2))
