"""A zero-turn grant may open; the accepted receipt explains its effect and preserves the original request contract."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_final.pi.probe import study
from gx_core.p0 import v62_modules, v62_native


class OpeningBudget(unittest.TestCase):
    def test_opening_explains_zero_turns_and_retains_actual_partial_report(self):
        text = v62_native.open_tool({"harness_policy":"gx.harness/3"})["inputSchema"]["properties"]["sub_budget"]
        original = json.loads(Path(__file__).with_name("a-original-contract.json").read_text())
        self.assertEqual(text, original["official_tool_contract"]["sub_budget"])
        older = v62_native.open_tool({"harness_policy":"gx.harness/2"})["inputSchema"]["properties"]["sub_budget"]
        self.assertNotIn("description", older)
        with tempfile.TemporaryDirectory() as work, study(Path(work), "Compare these archived source records.") as s:
            lead = s.next_actor("run")
            s.call(lead, "gx_query_catalog", {"kind":"all","pi_method_scope":[]}, "overview")
            calls = []
            for key, extra in (("omitted", {}), ("zero", {"api_calls":0}), ("funded", {"api_calls":1})):
                limits = {**original["allocation"], **extra}
                opening = s.open_args(key, inputs=[], caps=[], limits=limits)
                opening.update(module_id="ad_hoc", sub_budget=limits, focus="Check source dates.", expected_delivery="Return the date coverage.")
                result = s.call(lead, "gx_open_module", opening, key)
                self.assertEqual(s.call(lead, "gx_open_module", opening, "replay-"+key), result)
                v62_modules.advance(s.conn, s.run_id)
                module = s.conn.execute("SELECT * FROM v62_modules WHERE scope=?", (result["scope"],)).fetchone()
                self.assertEqual(json.loads(module["budget_json"]), limits)
                if key == "funded":
                    self.assertNotIn("api_calls_notice", result)
                    self.assertEqual(module["status"], "OPEN")
                    self.assertEqual(s.bind(result["task_id"]).scope, result["scope"])
                else:
                    self.assertIn("api_calls", result["api_calls_notice"])
                    self.assertIn("new operation_key", result["api_calls_notice"])
                    self.assertIn("omitted" if key == "omitted" else "explicitly zero", result["api_calls_notice"])
                    self.assertEqual(module["status"], "PARTIAL")
                    self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM p0_roots WHERE task_id=?", (result["task_id"],)).fetchone()[0], 0)
                    self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM v62_module_reports WHERE module_instance_id=?",
                                                    (module["module_instance_id"],)).fetchone()[0], 1)
                calls.append({"case":key, "limits":limits, "module":module["module_instance_id"],
                              "status":module["status"], "notice":result.get("api_calls_notice"),
                              "task":result["task_id"], "root_count":s.conn.execute(
                                  "SELECT COUNT(*) FROM p0_roots WHERE task_id=?", (result["task_id"],)).fetchone()[0]})
            if output := os.getenv("GX_DELEGATION_NOTICE_RECEIPT"):
                Path(output).write_text(json.dumps({"entry":"gx-p0 research", "run":s.run_id,
                    "transport":"controlled native callbacks", "cases":calls}, indent=2)+"\n")
