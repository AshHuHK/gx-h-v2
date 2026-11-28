"""Compare the shared product change and site-specific registrations against each release base."""
import argparse
import json
import subprocess


FULL_BASE = "4b6aa93ad5bf638d06bed7004c26c27d030a59ea"
LEAN_BASE = "6ec604d176d307c27b3c4f7d3e7ee02e37325bce"
SCOPE = ("bin", "gx-h/gx_core", "gx-h/config", "gx-h/contracts", "gx-h/plugins", "gx-h/adapters")
REGISTRY = "gx-h/config/workflow_packages.json"
READER = "gx-h/adapters/scientific/published_documents.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()


def changed(repo, older, newer):
    return set(git(repo, "diff", "--name-only", older, newer, "--", *SCOPE).splitlines())


def registry(repo, rev):
    value = json.loads(git(repo, "show", rev + ":" + REGISTRY))
    rows = value.pop("plugins")
    return value, {r["path"]: r["sha256"] for r in rows}


def compare(repo, full, lean):
    full, lean = [git(repo, "rev-parse", rev + "^{commit}") for rev in (full, lean)]
    change = changed(repo, FULL_BASE, full)
    if change != changed(repo, LEAN_BASE, lean):
        raise ValueError("The sets of changed installed product files differ.")
    prior = changed(repo, FULL_BASE, LEAN_BASE)
    current = changed(repo, full, lean)
    if current != prior:
        raise ValueError(f"Changed cross-site packaging differences: {sorted(current ^ prior)}")
    unique = change & current
    if REGISTRY not in unique or unique - {REGISTRY, READER}:
        raise ValueError(f"Changed product files requiring site-specific review: {sorted(unique)}")
    if READER in unique:
        original_hint = b"PDF reader from release-assets/python/requirements.lock"
        lean_hint = b"PDF reader from requirements.txt"
        for source, destination in ((FULL_BASE, LEAN_BASE), (full, lean)):
            shared, local = [subprocess.check_output(["git", "-C", repo, "show", revision + ":" + READER])
                             for revision in (source, destination)]
            if shared.count(original_hint) != 1 or shared.replace(original_hint, lean_hint) != local:
                raise ValueError("Unexpected scientific reader difference in the lean release.")

    full_config, full_pins = registry(repo, full)
    lean_config, lean_pins = registry(repo, lean)
    _, old_full = registry(repo, FULL_BASE)
    _, old_lean = registry(repo, LEAN_BASE)
    old_extra = {key: pin for key, pin in old_lean.items() if key not in old_full}
    if full_config != lean_config or lean_pins != {**full_pins, **old_extra}:
        raise ValueError("Site registries lost shared config or a lean-only provider pin.")

    files = {}
    for name in sorted(change - unique):
        tree = git(repo, "ls-tree", full, "--", name).split(maxsplit=3)
        files[name] = tree[2] if tree else None
    return {
        "full": full, "lean": lean, "full_base": FULL_BASE, "lean_base": LEAN_BASE,
        "scope": SCOPE, "changed_product_files": len(change), "identical_changed_blobs": files,
        "adapters_trees": {name: git(repo, "rev-parse", rev + ":gx-h/adapters")
                           for name, rev in (("full", full), ("lean", lean))},
        "changed_site_specific_files": sorted(unique),
        "preserved_site_specific_files": sorted(current), "lean_only_plugin_pins": old_extra,
        "registry_settings_match": True, "full_plugin_count": len(full_pins), "lean_plugin_count": len(lean_pins),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("full")
    parser.add_argument("lean")
    args = parser.parse_args()
    print(json.dumps(compare(args.repo, args.full, args.lean), indent=2))
