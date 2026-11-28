"""Run or resume the preregistered independent question using the existing subscription."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PRODUCT = HERE.parents[3]
RUN_KEY = "food-archive-blind-row-first"


def start(base, timeout, supplied=None):
    cfg = json.loads((HERE/"preregistered.json").read_text())
    if hashlib.sha256((HERE/"question.md").read_bytes()).hexdigest() != cfg["question_sha256"]:
        raise ValueError("The frozen question changed")
    work = base / "yeast-followup"
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = work/"state"
    if supplied is None:
        src_state = base/"yeast/state"
        with sqlite3.connect((src_state/"gx.db").as_uri()+"?mode=ro",uri=True) as db:
            row = db.execute("SELECT namespace,rel_path FROM artifacts WHERE kind='journal_article' AND published=1 "
                             "AND sha256=? ORDER BY created_at LIMIT 1", (cfg["article_sha256"],)).fetchone()
        if not row:
            raise ValueError("Supply the pinned public article with --article")
        supplied = src_state.joinpath("artifacts",*row)
    blob = supplied.read_bytes()
    if hashlib.sha256(blob).hexdigest() != cfg["article_sha256"]:
        raise ValueError("Original article differs from the registered public record")
    article = work/"article.xml"
    article.write_bytes(blob)
    config = {"project":"food-reference-unlabeled-archive-row","budget_profile":"oauth_research",
              "research_effort":"medium","input_files":[{"role":"publication","path":str(article),"sha256":cfg["article_sha256"]}]}
    goal_file = work/"goal.json"
    goal_file.write_text(json.dumps(config))
    wrapper = PRODUCT/"acceptance/science_assets/deployment/start_native.py"
    codex = PRODUCT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    command = [sys.executable,"-B",str(wrapper),str(PRODUCT),str(codex),str(work),"research","--state",str(state),
               "--timeout",str(timeout),"--run-key",RUN_KEY,"--goal-config",str(goal_file),"--goal-file",str(HERE/"question.md")]
    return subprocess.run(command, cwd=PRODUCT).returncode


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base",type=Path)
    ap.add_argument("--timeout",type=int,default=1200)
    ap.add_argument("--article",type=Path,help="Previously acquired pinned JATS original; omit to use the first study’s copy.")
    args = ap.parse_args()
    sys.exit(start(args.base.resolve(),args.timeout,args.article))
