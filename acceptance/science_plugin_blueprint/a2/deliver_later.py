"""Deliver the pre-registered second paper after the first bound reader sees an original paragraph."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
KEY = 'skyglow-a2-later-paper'


def first_body_read(conn, digest):
    """Only an original article body paragraph admits the second paper."""
    return conn.execute("""SELECT rr.receipt_id, rr.delivered_at, rr.task_id, rr.principal, rr.segment_id,
        s.blob_artifact_ref AS source_ref, g.run_id, g.status FROM source_reads rr
        JOIN sources s USING(source_id) JOIN v6_goals g ON g.run_id=s.namespace
        JOIN source_segments seg ON seg.source_id=s.source_id AND seg.segment_id=rr.segment_id
        WHERE s.blob_sha256=? AND seg.kind='body' AND (seg.label LIKE '%/p' OR seg.label LIKE '%/p/%'
              OR seg.label LIKE '%/p characters %') ORDER BY rr.rowid LIMIT 1""",(digest,)).fetchone()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work', type=Path)
    args = parser.parse_args()
    work = args.work.resolve()
    database = work / 'state/gx.db'
    manifest = json.loads(Path(__file__).with_name('materials.json').read_text())
    atlas_digest = next(m['sha256'] for m in manifest['materials'] if m['name']=='atlas.xml')
    deadline = time.monotonic() + 1000
    while time.monotonic() < deadline:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            first = first_body_read(conn, atlas_digest)
            goal = conn.execute('SELECT status FROM v6_goals').fetchone()
        if first:
            break
        if goal and goal[0] == 'CLOSED':
            raise RuntimeError('The first field Run closed without a paragraph receipt')
        time.sleep(1)
    else:
        raise TimeoutError('The first field Run has no paragraph receipt. Resume the same Run.')
    cmd = [sys.executable, '-B', str(ROOT / 'acceptance/science_capabilities/gx_oauth.py'),
           str(work), 'message', '--state', str(work / 'state'), '--run-id', first['run_id'],
           '--message-id', KEY, '--queue-only', '--attachment', str(work / 'materials/trend.xml'),
           '--text', '補上一份後來收到的相關原始研究，請判斷是否影響問題的比較或結論。']
    reply = subprocess.run(cmd, check=True, text=True, capture_output=True)
    print(json.dumps({'first_read':dict(first),'delayed_ingress':json.loads(reply.stdout)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
