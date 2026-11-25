"""Passively reconcile an already admitted control turn from its original official host store."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys

from fixture import PRODUCT, V6, read, save

sys.path.insert(0, str(PRODUCT / "gx-h"))
from gx_core.native_release import native_state
from gx_core.persistence import store
from gx_core.p0 import metering, profiles, v6_costs
from gx_core.p0.roots import Roots


def audit(world, review):
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "GX_P0_CODEX")
    }
    env.update(
        HOME=str(world / "native/home"),
        CODEX_HOME=str(world / "native/home/codex"),
        GX_P0_PROFILE=str(world / "native/model-profile.json"),
        **profiles.proxy_settings()
    )
    state = world / "state"
    rid = read(review / "seed.json")["run_id"]
    with native_state(state), closing(store.open_store(state / "gx.db")) as db:
        before = v6_costs.resource_report(db, rid)
        meter_rows = db.execute(
            "SELECT thread_id,turn_id,reservation_ref FROM p0_meter_native_turns WHERE run_id=? AND budget_status='HELD'",
            (rid,),
        ).fetchall()
        admissions = lambda: [
            tuple(r)
            for r in db.execute(
                "SELECT event_id,thread_id,turn_id FROM p0_root_deliveries WHERE run_id=? ORDER BY rowid",
                (rid,),
            )
        ]
        original = admissions()
        details = []
        if meter_rows:
            with Roots(db, run_id=rid, cwd=state / "threads", env=env) as native:
                for meter_row in meter_rows:
                    thread_id, turn_id, reservation = meter_row
                    binding = native._binding(thread_id)
                    thread = native._request(
                        "thread/read", {"threadId": thread_id, "includeTurns": True}
                    )["thread"]
                    if thread["id"] != thread_id or not native._matches(binding, thread):
                        raise ValueError("Official history does not match the admitted native identity")
                    native.delivery.attached(thread_id, thread)
                    native.delivery.reconcile(thread_id)
                    details.append(
                        {
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "reservation_ref": reservation,
                            "history_statuses": [
                                t["status"] for t in thread.get("turns", []) if t["id"] == turn_id
                            ],
                        }
                    )
            metering.maintain(db, rid)
        if admissions() != original:
            raise RuntimeError("Admission history changed during the passive read")
        after = v6_costs.resource_report(db, rid)
        return {
            "run_id": rid,
            "items": details,
            "new_turn_deliveries": 0,
            "before": before,
            "after": after,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private-dir", required=True, type=Path)
    p.add_argument("--review-dir", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args()
    if a.out.exists():
        p.error("Choose a new receipt path")
    worlds = read(a.review_dir / "plan.json")["worlds"]
    save(
        a.out,
        {
            "mode": "official passive history and GX meter maintenance",
            "runs": [
                {
                    "packet_id": world["packet_id"],
                    **audit(a.private_dir / world["slot"], a.review_dir / world["slot"]),
                }
                for world in worlds
            ],
        },
    )
