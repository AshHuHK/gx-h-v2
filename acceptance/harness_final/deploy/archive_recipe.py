"""Check current reader compatibility with immutable historic recipe bodies and real adoption requests."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys


def inspect(site, state, name):
    sys.path.insert(0, str(site / "gx-h"))
    from gx_core.domain import ids
    from gx_core.p0 import v6_recipes

    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        record = db.execute(
            "SELECT source_run,digest,definition_json FROM v6_saved_recipes WHERE reference=?", (name,)
        ).fetchone()
        if record is None:
            raise ValueError(f"No archived recipe {name}.")
        body = json.loads(record["definition_json"])
        card = next(c for c in v6_recipes.cards(db) if c["reference"] == name)
        if card["digest"] != record["digest"] or card["digest"] != ids.sha256_of_obj(body):
            raise ValueError("The computed recipe hash disagrees with the archived card.")
        replays = []
        for row in db.execute(
            "SELECT r.run_id,n.call_id,n.request_json FROM p0_v6_native_calls n "
            "JOIN p0_roots r USING (thread_id) "
            "WHERE json_extract(n.request_json,'$.tool')='gx_instantiate_recipe' "
            "AND json_extract(n.request_json,'$.arguments.recipe')=? ORDER BY n.call_id", (name,)
        ):
            request = json.loads(row["request_json"])["arguments"]
            result = v6_recipes.instantiate(db, row["run_id"], request)
            if result["digest"] != request["digest"] or result["digest"] != card["digest"]:
                raise ValueError("The original request no longer expands the same recipe.")
            replays.append({
                "run_id": row["run_id"], "call_id": row["call_id"], "request_sha256": ids.sha256_of_obj(request),
                "expansion_sha256": ids.sha256_of_obj(result), "input_bindings": request["inputs"],
                "already_bound_steps": result.get("bound_steps", {}), "planned_operations": len(result["ops"]),
                "module_calls": len(result.get("module_calls", [])),
            })
        if not replays:
            raise ValueError("No original adoption request references this recipe.")
        return {
            "evidence": "READ_ONLY_REPLAY_OF_ARCHIVED_NATIVE_REQUESTS",
            "source_run": record["source_run"], "recipe": name,
            "definition_schema": body["schema"], "body_sha256": record["digest"],
            "card_sha256": ids.sha256_of_obj(card), "closed_state_expansions": replays,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    parser.add_argument("original_state", type=Path)
    parser.add_argument("recipe")
    args = parser.parse_args()
    print(json.dumps(inspect(args.site.resolve(), args.original_state.resolve(), args.recipe), indent=2))
