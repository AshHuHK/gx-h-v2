"""Offline regression set for the asset catalog, scientific contracts, recovery and actual publications."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
CURRENT = sorted(p.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
                 for p in (ROOT / "acceptance/science_assets").rglob("test_*.py"))
AFFECTED = [
    "acceptance.unified_multiturn.test_successor", "acceptance.science_execution.test_research_entry",
    "acceptance.science_capabilities.test_derived_method", "acceptance.science_capabilities.test_scholarly_sources",
    "acceptance.science_capabilities.test_yeast_evidence", "acceptance.science_capabilities.test_method_citations",
    "acceptance.science_plugin_blueprint.test_catalog", "acceptance.science_plugin_blueprint.test_catalog_survey",
    "acceptance.science_plugin_blueprint.test_catalog_survey_sources", "acceptance.science_plugin_blueprint.test_method_health",
    "acceptance.science_plugin_blueprint.test_reading_skill", "acceptance.science_plugin_blueprint.test_recipe_binding",
    "acceptance.science_plugin_blueprint.test_scholarly_pages", "acceptance.science_plugin_blueprint.a1.test_hydrometry",
    "acceptance.science_plugin_blueprint.b0.test_calibration", "acceptance.v624.test_cheap_job",
    "tests.test_p0_manifest", "tests.test_delivery_manifest", "tests.test_v62_module_catalog",
    "tests.test_v62_modules_catalog", "tests.test_v6_named_ports", "tests.test_science_capability_collections",
    "tests.test_publication_discovery", "tests.test_researcher_handoff", "tests.test_source_decision_receipts",
]
if __name__ == "__main__":
    env = dict(os.environ, GX_CODEX_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in (".", "gx-h", "gx-h/tests", "acceptance/v62a")))
    sys.exit(subprocess.call([sys.executable, "-B", "-m", "unittest", "-v", *CURRENT, *AFFECTED], cwd=ROOT, env=env))
