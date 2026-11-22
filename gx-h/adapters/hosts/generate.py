#!/usr/bin/env python3
"""Check or regenerate the release's native role TOMLs and their content pins."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gx_core.hostobs import codex_roles, host_policy, roles


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def digests(intents):
    return {name: intent["sha256"] for name, intent in intents.items()}


def projections():
    default = codex_roles.shipped()
    model_pins = {host_policy.DEFAULT_MODEL: digests(default)}
    effort_changes = {floor: {} for floor in host_policy.PROJECT_WORKER_EFFORT_FLOORS}
    for model in host_policy.PROJECT_MODELS:
        if model not in model_pins:
            document, _ = host_policy._native_policy_for_model(model)
            model_pins[model] = digests(codex_roles.shipped(document))
        for floor in effort_changes:
            document, _ = host_policy._native_policy_for_model(model, floor)
            pins = digests(codex_roles.shipped(document))
            effort_changes[floor][model] = {name: sha for name, sha in pins.items() if model_pins[model][name] != sha}

    published = json.loads(roles.SHIPPED_PINS.read_text(encoding="utf-8"))
    published["role_intents"] = digests(roles.shipped())
    published["role_intents_by_host"] = {"codex": model_pins.pop(host_policy.DEFAULT_MODEL)}
    expected = {ROOT / entry["path"]: entry["content"] for entry in codex_roles.shipped_files()}
    expected[ROOT / "adapters/hosts/role_intents.json"] = json_bytes(default)
    expected[host_policy.PROJECT_ROLE_PINS] = json_bytes({**model_pins, "worker_effort_floors": effort_changes})
    expected[roles.SHIPPED_PINS] = json_bytes(published)
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify installed role files and pins (default)")
    mode.add_argument("--write", action="store_true", help="regenerate the role files and accept their new digests")
    write = parser.parse_args().write
    mismatches = []
    for path, data in projections().items():
        if path == roles.SHIPPED_PINS:
            matches = json.loads(path.read_bytes()) == json.loads(data)
        else:
            matches = path.is_file() and path.read_bytes() == data
        if matches:
            continue
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        else:
            mismatches.append(str(path.relative_to(ROOT)))
    if mismatches:
        print("Role files or pins differ: " + ", ".join(mismatches), file=sys.stderr)
        return 1
    print("Codex role projections and model policies match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
