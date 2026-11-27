"""Task card terms enter the same immutable Goal and survive ordinary recovery."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from gx_core.p0 import cli


class TaskCard(unittest.TestCase):
    def test_frozen_terms_and_reference_guides_on_normal_research(self):
        work = Path(self.enterContext(TemporaryDirectory()))
        original = Path(__file__).with_name("coordinate-coverage.json")
        task = work/"task.json"
        task.write_bytes(original.read_bytes())
        terms = json.loads(task.read_bytes())
        arguments = ["research", "--state", str(work/"state"), "--run-key", "card-study", "--task-card", str(task)]
        def invoke(parts):
            with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), redirect_stdout(io.StringIO()):
                return cli.main(parts)
        self.assertEqual(invoke(arguments), 0)
        with CloseStudy(work, reopen=True) as s:
            lead = s.next_actor("run")
            packet = s.brief(lead)
            for part in (terms["question"], *terms["acceptance"], *terms["stop_when"]):
                self.assertIn(part, packet["goal"])
            run = s.run_id
            for ref in ["interpret-coordinate-coverage@1","interpret-enzyme-annotation@1"]:
                content = s.call(lead, "gx_query_catalog", {"kind":"skill","reference":ref}, ref)["spec"]
                self.assertGreater(len(content["reading_guide"]), 1)
                self.assertEqual(content["status"],"reference_only")
        terms["stop_when"] = ["A different stopping rule."]
        task.write_text(json.dumps(terms))
        self.assertNotEqual(invoke(arguments), 0)
        task.unlink()
        self.assertEqual(invoke(["research", "--state", str(work/"state"), "--run-id", run]), 0)
