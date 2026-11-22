"""Oversized commissions and standalone model terms remain readable before a Root starts a run."""
from contextlib import closing
from pathlib import Path
import re
import tempfile
import unittest

from gx_core import api
from gx_core.hostobs import native_context
from gx_core.native_release import native_state
from gx_core.persistence import store
from gx_core.project_followup import PROMPT_LIMIT, launch_brief


class LaunchBriefPaging(unittest.TestCase):
    def test_root_reads_paged_originals_before_run_start(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, "state")
            state.mkdir()
            prompt = state / "commission.md"
            body = "计量依据：100\n" * 9000
            budget = "完整模型条款\n" * 9000
            self.assertLess(len(body.encode()), PROMPT_LIMIT)
            self.assertGreater(len((body + budget).encode()), PROMPT_LIMIT)
            prompt.write_text(body)
            terms = {"model_budget": budget, "model_budget_file": str(state / "budget.md")}
            def launch(phase=None):
                return launch_brief(prompt, None, state=state, project="demo",
                                    model_terms=terms, phase=phase).read_text()

            first = launch()
            phase = {"idempotency_key": "next-round", "predecessor_run_id": "previous-run"}
            second = launch(phase)
            self.assertIn("inputs.parent_run_id=previous-run", second)
            self.assertNotIn("directly as your input", second)
            pattern = r"artifact_ref=(gxa_[a-z0-9]+)"
            refs = re.findall(pattern, first)
            self.assertEqual(len(refs), 2)
            self.assertEqual(re.findall(pattern, second), refs)
            prompt.write_text("Read prior measurements and resume the waiting worker.\n")
            warm = launch_brief(prompt, None, state=state, project="demo", model_terms=terms,
                                continuation=True).read_text()
            # With this moderate standalone budget, the shorter handoff now fits inline.
            self.assertIn(prompt.read_text(), warm)
            self.assertIn(budget, warm)
            with native_state(state) as owned, closing(store.open_store(owned.db_path)) as conn:
                def root(project):
                    authority = native_context.NativeAuthority(project_ref=project)
                    self.addCleanup(authority.close)
                    authority.register_thread({"id": "root", "sessionId": "root", "parentThreadId": None,
                        "cliVersion": authority.host_version, "model": "recorded", "reasoningEffort": "high"},
                        root=True, sequence=1)
                    authority.start_turn("root", "active", sequence=2)
                    return authority

                def read(authority, ref, offset):
                    args = {"artifact_ref": ref, "offset": offset}
                    seq = authority.sequence + 1
                    call = authority.make_call({"threadId": "root", "turnId": "active", "callId": str(seq),
                        "tool": "gx_artifact_get", "arguments": args}, sequence=seq)
                    try:
                        return api.dispatch("gx_artifact_get", args, conn=conn, principal=call.principal,
                                            transport=native_context.TRANSPORT, native_call=call)
                    finally:
                        authority.complete(call)

                owner = root("demo")
                self.assertFalse(read(root("unrelated"), refs[0], 0)["ok"])
                for ref, expected in zip(refs, (budget, body)):
                    chunks, offset = [], 0
                    while offset is not None:
                        answer = read(owner, ref, offset)
                        self.assertTrue(answer["ok"], answer)
                        page = answer["data"]
                        chunks.append(page["content"])
                        offset = page["metadata"]["next_offset"]
                    self.assertGreater(len(chunks), 1)
                    self.assertEqual("".join(chunks), expected)
                self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM artifacts").fetchone()[0], 2)
