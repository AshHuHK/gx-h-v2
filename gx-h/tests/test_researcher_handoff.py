"""Exercise the public Goal tool boundary with distinct, individually leased source readers."""
from unittest.mock import patch
from pathlib import Path
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.errors import ScopeDenied, SchemaInvalid
from gx_core.p0 import cli, research_handoff, v6_goal, v62_sources, v62_reports
from tests.test_v6_decisions import draft
from tests import test_v62_sources as source_tests


class Researchers(unittest.TestCase):
    attach = source_tests.SourceGoal.attach
    bind = source_tests.SourceGoal.bind
    call = source_tests.SourceGoal.call
    open = source_tests.SourceGoal.open
    paragraph = source_tests.SourceGoal.paragraph

    def setUp(self):
        start = cli.start
        def research_start(conn, directory, spec, **args):
            front = ("<front><article-meta><article-id pub-id-type='doi'>10.5555/fixture"
                     "</article-id><abstract><p>Brightness was compared across the city.</p></abstract>"
                     "</article-meta></front>")
            body = ("<body><sec id='measured'>" + "".join(f"<p>Station {n}.</p>" for n in range(13)) +
                    "</sec><sec id='methods'><p>Only west stations after midnight were sampled.</p></sec></body>")
            for name, content in (("index.xml",front),("paper.xml", front + body)):
                original = Path(directory) / name
                original.write_text("<article>" + content + "</article>")
                spec["input_files"].append({"path":str(original),"role":"literature"})
            return start(conn, directory, {**spec, "discovery_policy": "gx.catalog/1"}, **args)
        with patch.object(cli, "start", side_effect=research_start):
            source_tests.SourceGoal.setUp(self)

    def test_addressed_hint_then_verbatim_report_and_cold_read(self):
        src = self.refs["article.nxml"]
        owner, author = self.open(self.lead, [src])
        peer, reader = self.open(self.lead, [src])
        other, third = self.open(self.lead, [self.refs["manual.md"]])
        original = self.paragraph(author, "article.nxml", "p1")
        text = "Only the second station remained dry. Restrict the comparison to that station."
        hint = {"text":text, "recipient_scope":peer["scope"], "source_refs":[src], "operation_key":"dry-station"}
        menu = self.call(author, "gx_task_context", {})
        self.assertIn(peer["scope"], [row["scope"] for row in menu["open_peers"]])
        first = self.call(author, research_handoff.HINT, hint)
        self.assertEqual(first, self.call(author, research_handoff.HINT, hint))
        self.assertGreater(first["event_id"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='v62.module.peer_hint'").fetchone()[0], 1)
        with self.assertRaises(ScopeDenied):
            self.call(author, research_handoff.HINT, {**hint, "recipient_scope":other["scope"], "operation_key":"wrong"})
        self.assertEqual(self.call(third, "gx_task_context", {})["peer_hints"], [])
        received, = self.call(reader, "gx_task_context", {})["peer_hints"]
        self.assertEqual(received["text"], text)
        self.assertEqual(received["author"]["threadId"], author["threadId"])
        with self.assertRaises(ScopeDenied):
            self.call(reader, "gx_submit_module_report", {"operation_key":"unread","status":"COMPLETE",
                "findings":[{"text":text,"source_refs":[src]}],"artifact_refs":[],"gaps":[],"next_steps":[]})
        review_read = self.paragraph(reader, "article.nxml", "p1")
        self.assertNotEqual(review_read["receipt_id"], original["receipt_id"])
        self.assertEqual(review_read["source_sha256"], original["source_sha256"])
        report = self.call(author, "gx_submit_module_report", {"operation_key":"qualified","status":"COMPLETE",
            "findings":[{"kind":"counterevidence","text":text,"source_refs":[src]}],"artifact_refs":[],
            "gaps":["Other stations have no measurement."],"next_steps":["Compare only shared stations."]})
        bundle = ArtifactService(self.conn).read_json(report["report_ref"])
        self.assertEqual(bundle["researcher_statement"]["findings"][0]["text"], text)
        self.assertEqual(bundle["author"]["thread_id"], author["threadId"])
        entry = v62_reports._index_entry(bundle, report["report_ref"])
        fits = v62_reports._preview(bundle, entry, 4096)["researcher_message"]
        self.assertEqual((fits["author_kind"], fits["finding"]["text"], fits["gaps"], fits["next_steps"]),
                         ("MODULE", text, ["Other stations have no measurement."], ["Compare only shared stations."]))
        self.assertEqual(research_handoff.message_preview({"author":{"kind":"GX"}, "researcher_statement":{}}), {"author_kind":"GX"})
        from gx_core.persistence import store
        self.conn.close()
        self.conn = store.open_store(self.path)
        self.attach()
        self.roots.active_turns[reader["threadId"]] = reader["turnId"]
        self.assertEqual(self.call(reader, "gx_task_context", {})["peer_hints"], [received])

    def test_abstract_duplicate_and_later_page_do_not_imply_an_independent_all_city_comparison(self):
        index, paper = self.refs["index.xml"], self.refs["paper.xml"]
        _, author = self.open(self.lead, [index, paper])
        requests = [self.call(author, v62_sources.READ, {"source_ref":ref}) for ref in (index, paper)]
        self.assertEqual([x["text_coverage"] for x in requests], ["ABSTRACT_ONLY","FULL_TEXT"])
        self.assertEqual([x["parsing"]["publication_identifiers"] for x in requests], [["10.5555/fixture"]]*2)
        self.assertNotEqual(requests[0]["source_sha256"], requests[1]["source_sha256"])
        from acceptance.science_plugin_blueprint.a2.deliver_later import first_body_read
        abstract = self.paragraph(author, "index.xml", "p1")
        self.assertIsNone(first_body_read(self.conn, requests[1]["source_sha256"]))
        narrow = {"claim":"The comparison covers all stations throughout the night.",
                  "claim_grain":"METHOD_DETAIL", "direction":"opposes", "applicability":"Scope of the station survey",
                  "confidence_basis":"The original methods specify the observed stations and times."}
        def extract(read):
            return self.call(author, v62_sources.EXTRACT, {
                **{k:read[k] for k in ("source_ref","segment_id","receipt_id")}, "quote":read["text"], **narrow})
        with self.assertRaisesRegex(SchemaInvalid, "Abstract-only"):
            extract(abstract)
        self.assertFalse(any("methods" in item["label"] for item in requests[1]["segments"]))
        page = self.call(author, v62_sources.READ, {"source_ref":paper,"start":requests[1]["next_start"]})
        segment = next(s["segment_id"] for s in page["segments"] if "methods" in s["label"])
        targeted = self.call(author,v62_sources.READ,{"source_ref":paper,"query":"After midnight"})
        self.assertEqual([r["segment_id"] for r in targeted["segments"]],[segment])
        self.assertNotIn("receipt_id",targeted)
        first_page = self.call(author,v62_sources.READ,{"source_ref":paper,"query":"Station"})
        rest = self.call(author,v62_sources.READ,{"source_ref":paper,"query":"Station","start":first_page["next_start"]})
        self.assertFalse(set(x["segment_id"] for x in first_page["segments"]) & set(x["segment_id"] for x in rest["segments"]))
        self.assertIsNone(rest["next_start"])
        read = self.paragraph(author, "paper.xml", segment)
        arrived = first_body_read(self.conn, requests[1]["source_sha256"])
        self.assertEqual(arrived["receipt_id"], read["receipt_id"])
        observation = extract(read)
        evidence = observation["evidence_ref"]
        self.call(self.lead, "gx_query_catalog", {"kind":"all","query":"original conditions of station comparisons"})
        self.call(self.lead, "gx_decide_revision", {"decision":draft()})
        scoped = "Compare only the west stations sampled after midnight."
        report = self.call(author, "gx_submit_module_report", {"operation_key":"source-conflict","status":"COMPLETE",
            "findings":[{"kind":"counterevidence","text":scoped,"source_refs":[paper,evidence]}],"artifact_refs":[],
            "gaps":["Street-level readings before midnight remain unknown."],"next_steps":["Obtain matched local readings."]})
        lead = self.bind(v6_goal.ensure_task(self.conn, self.run))
        current = self.call(lead, "gx_task_context", {})
        delivered = next(r for r in current["module_reports"] if r["report_ref"] == report["report_ref"])
        self.assertEqual(delivered["researcher_message"]["finding"]["text"], scoped)
        self.assertTrue(delivered["gx_facts"]["examples"])
        grant = self.call(lead,"gx_open_module",{"module_id":"offline_literature_mechanism","module_version":"1",
            "operation_key":"review-only","focus":"review the scope","round":2,"input_refs":[paper,evidence],
            "expected_delivery":"Review the exact disputed paragraph.","capabilities":[],
            "tools":["gx_read_source","gx_review_evidence","gx_submit_module_report"],
            "sub_budget":{"api_calls":2,"tokens":3000,"worker_tasks":3}})
        reviewer = self.bind(grant["task_id"])
        from gx_core.p0 import v6_native
        frozen = v6_native.brief(self.roots._binding(reviewer["threadId"]))
        self.assertNotIn("gx_read_artifact",frozen["allowed_tools"])
        _, limited = self.open(lead,[evidence],3)
        limited_packet = v6_native.brief(self.roots._binding(limited["threadId"]))
        self.assertNotIn("review_target", next(m for m in limited_packet["materials"] if m["artifact_ref"]==evidence))
        with self.assertRaises(ScopeDenied):
            self.paragraph(limited,"paper.xml",segment)
        quote = next(x["review_target"] for x in frozen["materials"] if x["artifact_ref"]==evidence)
        self.assertEqual((quote["source_ref"],quote["segment_id"],quote["quote_preview"]),(paper,segment,read["text"]))
        reread = self.paragraph(reviewer, "paper.xml", segment)
        result = self.call(reviewer, v62_sources.REVIEW, {"evidence_ref":evidence,
            "receipt_id":reread["receipt_id"],"quote_sha256_seen":quote["quote_sha256"],"verdict":"SUPPORTED",
            "note":"The located sentence restricts both place and observing time."})
        original = ArtifactService(self.conn).read_json(result["review_ref"])
        self.assertEqual(original["reviewer"]["thread_id"], reviewer["threadId"])
        self.assertNotEqual(original["extractor"]["thread_id"], reviewer["threadId"])
        self.assertEqual(original["quote"],read["text"])
        self.assertEqual(original["review_receipt_id"],reread["receipt_id"])

    def test_lead_can_recover_each_authored_word_when_short_preview_omits_items(self):
        src = self.refs["article.nxml"]
        _, author = self.open(self.lead, [src])
        self.paragraph(author, "article.nxml", "p1")
        self.call(self.lead, "gx_query_catalog", {"kind":"all"})
        self.call(self.lead, "gx_decide_revision", {"decision":draft()})
        request = {
            "operation_key":"conditional-stations", "status":"COMPLETE", "artifact_refs":[],
            "findings":[{"text":"The station appeared dry.", "source_refs":[src]},
                        {"text":"The sampling window limits that interpretation.", "source_refs":[src],
                         "kind":"counterevidence"}],
            "gaps":["No readings outside the window.", "Water input remains unmeasured."],
            "next_steps":["Retain the original comparison.", "Check the flow measurements separately."]}
        receipt = self.call(author, "gx_submit_module_report", request)
        from gx_core.persistence import store
        self.conn.close()
        self.conn = store.open_store(self.path)
        self.attach()
        lead = self.bind(v6_goal.ensure_task(self.conn, self.run))
        packet = self.call(lead, "gx_task_context", {})
        preview = next(row["researcher_message"] for row in packet["module_reports"]
                       if row["report_ref"] == receipt["report_ref"])
        self.assertEqual(preview["finding"]["text"], request["findings"][1]["text"])
        self.assertTrue(preview.get("read_full_report"), "The short message omits other findings, gaps and next steps")
        full = self.call(lead, "gx_read_artifact", {"ref":receipt["report_ref"]})
        self.assertEqual(full["author"]["thread_id"], author["threadId"])
        self.assertNotEqual(full["author"]["thread_id"], lead["threadId"])
        for field in ("findings", "gaps", "next_steps"):
            self.assertEqual(full["researcher_statement"][field], request[field])

    def test_signed_gap_in_acknowledged_packet_is_delivered_and_waiting_has_no_enacted_effect(self):
        from gx_core.p0 import research_routes
        source = self.refs["article.nxml"]
        owner, author = self.open(self.lead, [source])
        self.paragraph(author, "article.nxml", "p1")
        self.call(self.lead, "gx_query_catalog", {"kind":"all"})
        self.call(self.lead, "gx_decide_revision", {"decision":draft()})
        report = self.call(author, "gx_submit_module_report", {
            "operation_key":"report-unmeasured", "status":"COMPLETE", "artifact_refs":[source], "findings":[],
            "gaps":["Readings after sunrise were not collected."], "next_steps":["Seek daylight records."]})
        lead = self.bind(v6_goal.ensure_task(self.conn, self.run))
        packet = self.call(lead, "gx_task_context", {})
        item = next(x for x in packet["module_reports"] if x["report_ref"] == report["report_ref"])
        self.assertEqual(item["researcher_message"]["gaps"], ["Readings after sunrise were not collected."])
        self.call(lead, "gx_query_catalog", {"kind":"all"})
        self.call(lead, "gx_decide_revision", {"decision":{**draft(), "evidence_refs":[report["report_ref"]]}})
        record, = [r for r in research_routes.routes(self.conn, self.run, self.directory)["handoffs"]
                   if r["report_ref"] == report["report_ref"] and r.get("thread_id") == lead["threadId"]]
        self.assertTrue(record["received_content"], "The acknowledged packet contains the researcher's actual gap")
        self.assertEqual((record["status"], record["effects"]), ("CITED", []))
