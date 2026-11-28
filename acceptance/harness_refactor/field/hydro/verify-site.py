"""Check the two registered hydrometric methods on the site's ordinary Goal path."""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    args = parser.parse_args()
    release = args.release.resolve()
    sys.path.insert(0, str(release/"gx-h"))
    from gx_core.p0 import capability_views, harness_policy
    question = Path(__file__).resolve().parent/"a/question.md"
    scope = capability_views.freeze_scope({"mode": "goal", "objective": question.read_text(),
        "research_effort": "deep", "harness_policy": harness_policy.VERSION,
        "discovery_policy": capability_views.DISCOVERY_POLICY})
    methods = {opt["method"]: opt for opt in scope["capability_options"]
               if opt.get("provider", "").startswith("org.gx.hydro-")}
    installed = json.loads((release/"field-deployment.json").read_bytes())["installed"]
    for provider in installed:
        for name in provider["methods"]:
            if methods[name]["provider_digest"] != provider["sha256"]:
                raise ValueError("Goal method does not use the installed provider")
    if len(methods) != 2:
        raise ValueError("The expected two archived instruments were not available")
    print(json.dumps({"access": "selectable", "source": "site_policy", "methods": methods}, indent=2))


if __name__ == "__main__":
    main()
