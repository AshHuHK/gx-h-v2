"""Run the supplied 0.1.0 workflow and the installable 0.2.0 tool together."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "gx-h"))

from gx_core.plugins.catalog import package_digest
from gx_core.plugins.loader import bootstrap


def exercise(work):
    name = "org.example.gx.hello-research"
    archive = ROOT / "GX_H_V3_Lean_Implementation_Package.zip"
    prefix = "GX_H_V3_Lean_Implementation_Package/examples/hello_research_plugin/"
    root = work / "installed"
    one = root / name / "0.1.0"
    with ZipFile(archive) as zipped:
        members = [info for info in zipped.infolist() if info.filename.startswith(prefix) and not info.is_dir()]
        assert members
        for member in members:
            target = one / member.filename.removeprefix(prefix)
            if not target.resolve().is_relative_to(one.resolve()):
                raise ValueError(f"invalid fixture path {member.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zipped.read(member))
    two = root / name / "0.2.0"
    shutil.copytree(ROOT / "examples/plugins/hello_research", two)
    rows = [{"path": p.relative_to(root).as_posix(), "sha256": package_digest(p)} for p in (one, two)]
    catalog = work / "workflow_packages.json"
    catalog.write_text(json.dumps({"plugins": rows}))
    registry = bootstrap(root, catalog, [(name, two.name), (name, one.name)])
    assert not registry.failures, registry.failures
    first, second = (registry.get(name, p.name, row["sha256"]) for p, row in zip((one, two), rows))
    assert first.namespace != second.namespace
    workflow = first.contributions.workflows[("hello_research", "1")]
    hooks = workflow.hooks
    intent = hooks["validate_intent"]([])
    task = hooks["build_task_context"]({"task_id": "fixture-1"})
    greeting = second.contributions.tools["hello_research"].handler(None, {"name": "Ada"})
    assert intent == {"accepted": True, "warnings": []}
    assert task == {"task_id": "fixture-1", "source": "hello_research"}
    assert greeting == {"greeting": "Hello, Ada!"}
    assert not first.contributions.tools and not second.contributions.workflows
    return {"zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "packages": [{**row, "namespace": loaded.namespace} for row, loaded in zip(rows, (first, second))],
            "first_workflow": {"validate_intent": intent, "build_task_context": task},
            "second_tool": greeting, "exit_code": 0}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp:
        result = exercise(Path(temp))
    print(json.dumps(result, indent=2))
