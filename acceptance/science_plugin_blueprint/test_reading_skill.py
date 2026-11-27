"""New ordinary science Goals can use the reading guide if their native source tools exist."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.science_plugin_blueprint.catalog_probe import Scenario
from gx_core.p0 import catalog_resources, v62_sources


class SourceReading(unittest.TestCase):
    def test_scoped_available_skill_and_historical_reference(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        run = self.enterContext(Scenario(folder, spec_updates={
            "evidence_policy":"gx.evidence/6.2.4", "research_effort":"deep", "research_modules":[],
            "capabilities":[], "plugins":[], "capability_collections":["public_sources"], "input_files":[]
        }))
        args = {"reference":"gx2understand@1", "kind":"skill"}
        guide = run.call(run.lead, "gx_query_catalog", args, "get-reading-skill")["spec"]
        self.assertEqual("available", guide["status"])
        self.assertEqual(set(v62_sources.TOOL_NAMES), set(guide["tools"]))
        self.assertTrue(any("denominator" in text for text in guide["reading_guide"]))
        original = run.call(run.lead, "gx_query_catalog", {**args,"reference":guide["adapted_from"]["reference"]},
                            "historic-reading-guide")["spec"]
        self.assertEqual("reference_only", original["status"])
        self.assertEqual(original["digest"], guide["adapted_from"]["sha256"])
        brief = {"scope":"module:no-original", "allowed_tools":["gx_query_catalog","gx_read_artifact"]}
        rows = catalog_resources.rows_for_kind(run.conn, run.run_id, brief, "skill", [], [], current_reading=True)
        self.assertNotIn(args["reference"], {item["reference"] for item in rows})


if __name__ == "__main__":
    unittest.main()
