"""Read and hash every portable science_assets Goal in two checkouts; no Host or Job is started."""
import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path


def digest(path):
    with path.open("rb") as stream:
        content = sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            content.update(block)
    return content.hexdigest()


def material_path(root, goal, value):
    if not isinstance(value, str) or Path(value).is_absolute():
        raise ValueError(f"{goal.relative_to(root)} needs a release-relative path, got {value!r}")
    path = (goal.parent / value).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"{goal.relative_to(root)}: outside release or not a file: {value}")
    return path


def inspect(root):
    assets = root / "acceptance/science_assets"
    cards = {}
    for goal in sorted(assets.rglob("*.json")):
        contents = goal.read_bytes()
        if b'"input_files"' not in contents:
            continue
        doc = json.loads(contents)
        if not isinstance(doc, dict) or "project" not in doc or not isinstance(doc.get("input_files"), list):
            continue
        key = str(goal.relative_to(root))
        prompt = goal.with_name("yeast-task.json") if goal.name == "yeast-goal.json" else goal.with_name("question.md")
        question = {"path":str(prompt.relative_to(root)), "sha256":digest(prompt)}
        item = {"project":doc["project"], "goal_sha256":digest(goal), "prompt":question, "inputs":[]}
        for supplied in doc["input_files"]:
            source = material_path(root, goal, supplied["path"])
            hashed = digest(source)
            if hashed != supplied.get("sha256"):
                raise ValueError(f"{key}: input digest mismatch for {supplied['path']}")
            row = {"role":supplied.get("role"), "path":str(source.relative_to(root)), "sha256":hashed, "bytes":source.stat().st_size}
            if descriptor := supplied.get("acquisition"):
                metadata = material_path(root, goal, descriptor)
                raw = json.loads(metadata.read_bytes())
                if not raw.get("source") or not raw.get("acquisition"):
                    raise ValueError(f"{key}: missing source identity in {descriptor}")
                if "sha256" in raw and raw["sha256"] != hashed:
                    raise ValueError(f"{key}: acquisition input digest differs from {supplied['path']}")
                row["acquisition"] = {"path":str(metadata.relative_to(root)), "sha256":digest(metadata),
                                      "source":raw["source"], "material_sha256_in_descriptor":"sha256" in raw}
            item["inputs"].append(row)
        cards[key] = item
    return cards


def also_used_by_examples(root, materials):
    uses = defaultdict(set)
    for config in sorted((root/"examples").rglob("*.json")):
        doc = json.loads(config.read_bytes())
        rows = doc.get("input_files", []) if isinstance(doc, dict) else []
        for item in rows:
            if isinstance(item,dict) and item.get("sha256") in materials and "path" in item:
                path = material_path(root,config,item["path"])
                if digest(path) == item["sha256"]:
                    uses[item["sha256"]].add(str(config.relative_to(root)))
    return {key:sorted(value) for key,value in sorted(uses.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("full", type=Path)
    parser.add_argument("slim", type=Path)
    args = parser.parse_args()
    full, slim = args.full.resolve(strict=True), args.slim.resolve(strict=True)
    cards, deployed = inspect(full), inspect(slim)
    if cards.keys() != deployed.keys():
        raise ValueError("Goal paths differ: " + str(sorted(cards.keys() ^ deployed.keys())))
    for path, card in cards.items():
        if deployed[path] != card:
            raise ValueError(f"Release bytes or bindings differ: {path}")
    pins = [i for c in cards.values() for i in c["inputs"]]
    acquisitions = [p["acquisition"] for p in pins if "acquisition" in p]
    hashes = {i["sha256"] for i in pins}
    print(json.dumps({"scope":"Per-release local Goal, question/task card, input bytes and acquisition descriptor checks. Includes first-run configs and input-free studies.",
        "checkouts":[full.name,slim.name], "result":"PASS", "counts_per_release":{
            "goals":len(cards), "input_bindings":len(pins), "unique_input_sha256":len(hashes),
            "acquisition_bindings":len(acquisitions), "descriptor_material_sha256_present":sum(a["material_sha256_in_descriptor"] for a in acquisitions)},
        "slim_examples_sharing_pinned_inputs":also_used_by_examples(slim,hashes),
        "goals":cards}, ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
