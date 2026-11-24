"""Observe large assessment schema delivery through pinned Codex and a loopback Responses peer."""
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance"))
from native_probe import Probe, _proxy_preflight
from native_probe_peer import Peer, tool
from e2e_responses import brief_from, output_json
from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from workflows.science_grid_v3 import products


class SchemaPeer(Peer):
    def __init__(self):
        super().__init__()
        self.offset = 0
        self.pages = []
        self.contents = []

    def record(self, body):
        row, index = super().record(body)
        tools = [item for group in body["tools"] for item in group.get("tools", [group])]
        submit, = [item for item in tools if item.get("name") == "gx_submit_product"]
        self.wire = submit["parameters"]
        assert "offset=0" in submit["description"] and "metadata.next_offset" in submit["description"]
        self.schema_ref = brief_from(body)["input"]["product_contract"]["schema_ref"]
        if index > 1:
            response, = [item for item in body["input"] if item.get("type") == "function_call_output"
                         and item["call_id"] == f"read-schema-{index - 1}"]
            content = output_json(response["output"])
            if index == 2:
                self.direct_path_count = len(content["properties"]["findings_by_path"]["properties"])
            else:
                meta = content["metadata"]
                assert meta["offset"] == self.offset and meta["returned_bytes"] == len(content["content"].encode())
                self.pages.append((self.offset, meta["returned_bytes"]))
                self.contents.append(content["content"])
                self.offset = meta["next_offset"]
        return row, index

    def item(self, row, index):
        assert self.gate.wait(10)
        if self.offset is not None:
            args = {"ref": self.schema_ref}
            if index > 1:
                args["offset"] = self.offset
            assert index <= 32, "schema paging did not finish"
            return tool("gx_read_artifact", args, f"read-schema-{index}")
        return self.message(row["id"], "The full frozen reviewer schema was received.")


def assignment(conn, roots_dir, provider):
    schema.install(conn)
    run = records.create_run(conn, objective="Inspect schema transport for a synthetic failed-measurement fixture.",
        project_ref="schema-transport", actor="fixture", workflow_id="science_grid", workflow_version="3",
        budget_ref=budgets.create_root(conn, run_id=None, actor="fixture", profile_name="p0"),
        host={"policy_selection": {"model_provider": provider, "test_mode": True}})
    for state in ("AUDITING", "PLANNING", "ACTIVE"):
        store.transition(conn, machine="run", subject_id=run, to_state=state, actor="fixture")
    store.acquire_coordinator_lease(conn, run_id=run, host="codex", owner="transport-fixture")
    assets = ArtifactService(conn)

    def publish(kind, body):
        return assets.publish_bytes(run_id=run, namespace=run, kind=kind, rel_path=kind + ".json",
            media_type="application/json", data=ids.canonical_json(body)).artifact_ref

    input_ref = publish("grid_input", {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
                                       "candidates": [{"candidate_id": "sample", "x": 2}]})
    paths = [f"schema-path-{i:02d}" for i in range(32)]
    proposal_ref = publish("research_proposal", {"threshold": -0.5, "method": "fixture-grid@1", "paths": [
        {"id": path, "input_ref": input_ref, "prediction": "At least one sampled score reaches the cutoff.",
         "expected_reaches_threshold": True} for path in paths]})
    evidence_ref = publish("research_evidence", {"proposal_ref": proposal_ref, "routes": [
        {"path_id": path, "input_ref": input_ref, "job_id": "fixture-" + path, "status": "FAILED",
         "outcome_kind": "infrastructure_failure"} for path in paths]})
    inputs, rules = products.for_review(conn, run, {"round": 0, "max_rounds": 1, "proposal_ref": proposal_ref,
                                                   "evidence_refs": [evidence_ref]})
    task_id = records.create_task(conn, run_id=run, actor="fixture", role="evidence_reviewer", kind="research_round",
        task_input=inputs, required_artifacts=["science_assessment"], acceptance={"validate_workflow_product": True})
    store.transition(conn, machine="task", subject_id=task_id, to_state="READY", actor="fixture")
    roots = Roots(conn, run_id=run, cwd=roots_dir)
    row = roots._claim(task_id)
    return roots, row, inputs, rules


def main():
    _proxy_preflight()
    lock = json.loads((ROOT / "release-assets/lock.json").read_text())
    binary = ROOT / "vendor/codex-runtime/package" / lock["linux_x64"]["binary"]
    with binary.open("rb") as stream:
        binary_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        assert binary_sha == lock["linux_x64"]["binary_sha256"]
    with tempfile.TemporaryDirectory(prefix="gx-schema-wire-") as directory, ExitStack() as stack:
        temp = Path(directory)
        peer = SchemaPeer()
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        stack.callback(peer.server_close)
        stack.callback(peer.shutdown)
        probe = Probe(temp / "native", binary, peer)
        stack.callback(probe.close)
        stack.enter_context(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=directory))
        conn = stack.enter_context(closing(store.open_store(temp / "fixture.db")))
        roots, row, inputs, rules = assignment(conn, temp, probe.provider)
        settings = roots._settings(row, new=True)
        submit_group, = [group for group in settings["dynamicTools"]
                         if group.get("type") == "namespace" and group["name"] == "gx"]
        requested, = [tool["inputSchema"] for tool in submit_group["tools"] if tool["name"] == "gx_submit_product"]
        complete = requested["properties"]["product"]
        ref = inputs["product_contract"]["schema_ref"]
        assert complete == rules.schema() == ArtifactService(conn).read_json(ref)
        assert ref in row["brief"] and '"schema_ref"' in row["brief"]

        probe.callback = roots._callback
        probe.connect()
        start = probe.rpc("thread/start", settings)
        roots._verify_settings(row, start)
        thread = roots._bind(row, start["thread"])
        peer.gate.clear()
        turn = probe.start_turn(thread, row["brief"])
        roots.active_turns[thread] = turn
        peer.gate.set()
        probe.finish(thread, turn)
        recovered = "".join(peer.contents).encode()
        assert peer.schema_ref == ref and recovered == ArtifactService(conn).read_bytes(ref)
        assert json.loads(recovered) == complete
        encoded = lambda value: len(ids.canonical_json(value).encode())
        assert encoded(peer.wire) < encoded(requested)
        wire_paths = peer.wire["properties"]["product"]["properties"]["findings_by_path"]
        exact_paths = complete["properties"]["findings_by_path"]["required"]
        assert set(wire_paths["required"]) == set(exact_paths)
        assert wire_paths["properties"][exact_paths[0]] == {}
        model = {"findings_by_path": {key: {"reason": "Unmeasured.", "risk": "Infrastructure failure."}
                                     for key in exact_paths}}
        del model["findings_by_path"][exact_paths[-1]]
        try:
            products.prepare_native_product(conn, {"run_id": roots.run_id, "input": inputs}, model)
        except SchemaInvalid as failure:
            assert failure.details["category"] == "product_structure" and failure.details["contract_digest"] == rules.digest
        else:
            raise AssertionError("The server accepted an incomplete path map.")
        receipt = {"scope": "official transport, synthetic assignment, loopback scripted model",
            "binary_sha256": binary_sha, "source_commit": lock["source"]["commit"], "threads": 1,
            "responses": len(peer.rows), "path_count": len(exact_paths), "schema_ref": ref,
            "contract_digest": rules.digest, "complete_schema_sha256": ids.sha256_of_obj(complete),
            "complete_schema_bytes": encoded(complete), "advertised_argument_bytes": encoded(requested),
            "model_visible_argument_bytes": encoded(peer.wire), "model_visible_argument_sha256": ids.sha256_of_obj(peer.wire),
            "all_path_names_visible": True, "nested_model_schema_on_wire": wire_paths["properties"][exact_paths[0]],
            "direct_json_model_visible_paths": peer.direct_path_count, "page_offsets_and_bytes": peer.pages,
            "model_retrieved_full_schema_sha256": hashlib.sha256(recovered).hexdigest(),
            "incomplete_model_map_rejected": True, "scientific_jobs_executed": 0}
    target = ROOT / "acceptance/v31/schema-transport.json"
    target.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
