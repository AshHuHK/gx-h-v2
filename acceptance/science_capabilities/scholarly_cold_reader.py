"""Continue a scholarly handoff control in another process without fetching its original again."""
import argparse
import json
from pathlib import Path

from acceptance.multi_workflow.acquired_material_handoff import receive_reports
from acceptance.v622.test_close import CloseStudy, actual_packet
from acceptance.v62a.interleavings_fixture import draft, save


def verify(study, receipt):
    ref = receipt["outputs"]["article.xml"]
    assert study.run_id == receipt["run_id"]
    job_count = study.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (study.run_id,)).fetchone()[0]
    producer = study.next_actor(receipt["module"])
    packet = actual_packet(study, producer)
    assert any(m["artifact_ref"] == ref for m in packet["packet"]["materials"])
    published = study.report(producer, ref, "cold-publish-article",
                             text="The acquired article is available for an independent reading.")["report_ref"]
    lead = receive_reports(study, [published])
    spec = study.open_args("read-acquired-paper", inputs=[ref, published], caps=[], round=2)
    spec.update(module_id="scholarly_investigation", focus="literature",
                expected_delivery="Extract one body observation and report its limits.")
    peer = study.call(lead, "gx_open_module", spec, "cold-open-reader")
    study.call(lead, "gx_decide_revision", {"decision": draft(evidence=[published])}, "cold-await-reader")
    reader = study.bind(peer["task_id"])
    assert reader.scope != producer.scope and reader.thread != producer.thread
    materials = actual_packet(study, reader)["packet"]["materials"]
    material, = [item for item in materials if item["artifact_ref"] == ref]
    assert material["sha256"] == receipt["descriptor"]["sha256"]
    # Fixed public control datum. An autonomous researcher's selection is tested separately.
    quote = "all nth1Δ and nth2Δ mutants exhibited increased trehalose levels compared to their isogenic wild-type strains"
    read = study.call(reader, "gx_read_source", {"source_ref":ref, "segment_id":"p35.1"}, "cold-read-result")
    assert quote in read["text"] and read["source_sha256"] == material["sha256"]
    extract = study.call(reader, "gx_extract_evidence", dict(source_ref=ref, segment_id=read["segment_id"],
        receipt_id=read["receipt_id"], quote=quote, direction="supports",
        claim="The study reports altered intracellular trehalose in both deletion groups.",
        applicability="The yeast backgrounds and conditions studied in this paper.",
        limitations="An intracellular concentration difference does not measure purified enzyme activity.",
        confidence_basis="The authors compare each deletion group to its isogenic wild type in the Results."),
        "cold-extract-result")
    assert extract["accepted"]
    evidence = extract["evidence_ref"]
    report = study.report(reader, evidence, "cold-independent-report", artifact_refs=[evidence],
        findings=[dict(text="The paper reports “" + quote + "”.", source_refs=[ref, evidence])])["report_ref"]
    lead = receive_reports(study, [report])
    delivered = study.call(lead, "gx_read_artifact", {"ref": report}, "cold-lead-read")
    assert any(evidence in f["source_refs"] and quote in f["text"] for f in delivered["findings"])
    decision = draft(evidence=[report, evidence])
    decision["rationale"] = ("The located intracellular comparison warrants checking direct enzyme activity "
                             "and substrate conditions before inferring a biochemical rate.")
    adopted = study.call(lead, "gx_decide_revision", {"decision": decision}, "cold-lead-decide")
    assert study.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (study.run_id,)).fetchone()[0] == job_count
    return dict(run_id=study.run_id, transport="scripted native turns; acquired real Job, restarted GX store",
                acquisition_job=receipt["fetch_job"], source_ref=ref, source_sha256=material["sha256"],
                producer=producer.task_id, reader=reader.task_id, reader_scope=reader.scope, lead=lead.task_id,
                location={k:read[k] for k in ("segment_id","label","text_sha256","receipt_id")},
                quote=quote, extraction=evidence, report=report, decision=adopted["decision_ref"],
                rationale=decision["rationale"], jobs_on_reopen=job_count, jobs_after=job_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="completed scholarly_handoff control directory")
    work = parser.parse_args().directory.resolve()
    if (work / "cold-reader.json").exists():
        print((work / "cold-reader.json").read_text())
    else:
        receipt = json.loads((work / "receipt.json").read_text())
        with CloseStudy(work, reopen=True) as s:
            completed = verify(s, receipt)
        save(work / "cold-reader.json", completed)
        print(json.dumps(completed, ensure_ascii=False, indent=2))
