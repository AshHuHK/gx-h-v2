import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from .build import digest, encoded, reconcile
from .selection import card


class HistoricalInventory(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.work = Path(folder.name)
        self.mount = self.work / "mount-a"
        self.tables = self.work / "archive-a"
        self.mount.mkdir()
        self.tables.mkdir()
        self.record = self.mount / "mcp.json"
        self.record.write_text(json.dumps({"server":"archive","tools":[{"name":"read","description":"Protein record"}]}))
        self.fields = ["server_id","tool_name","schema_file","json_pointer"]
        self.row = {"server_id":"archive","tool_name":"read","schema_file":"mcp.json","json_pointer":"/tools/0"}
        self.write_table([self.row])
        self.selection = [card("mcp-tools",{"server_id":"archive","tool_name":"read"},"archive-read",
                              "record-reading","historical-pdb","method",["protein"],"Archive",
                              "Read archived observations",["archived values"],["accession"],
                              ["report"],"Historical probe only")]

    def write_table(self, rows):
        with (self.tables / "mcp-tools.tsv").open("w",newline="") as handle:
            writer = csv.DictWriter(handle,fieldnames=self.fields,delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

    def test_relocated_mount_preserves_identity_and_unknown_denominators(self):
        count, ledger, cards = reconcile(self.mount,self.tables,self.selection)
        self.assertEqual(1,count["tables"]["mcp-tools"]["rows"])
        self.assertIsNone(count["tables"]["research"]["rows"])
        self.assertEqual(2,ledger[0]["line"])
        self.assertEqual("json_pointer_verified",ledger[0]["source_check"])
        self.assertEqual("archive-read@legacy",ledger[0]["catalog_ref"])
        self.assertEqual(cards["cards"][0]["source"]["line"],ledger[0]["line"])
        other = self.work / "different-root"
        shutil.copytree(self.mount,other/"sources")
        shutil.copytree(self.tables,other/"tables")
        self.assertEqual((count,ledger,cards),reconcile(other/"sources",other/"tables",self.selection))

    def test_reordered_operation_does_not_publish_named_card(self):
        shifted = {"server":"archive","tools":[{"name":"different-action"},{"name":"read"}]}
        self.record.write_text(json.dumps(shifted))
        report,rows,cards = reconcile(self.mount,self.tables,self.selection)
        self.assertEqual("identity_mismatch",rows[0]["source_check"])
        self.assertEqual([],cards["cards"])
        self.assertEqual("identity_mismatch",report["incomplete_selections"][0]["reason"])

    def test_changed_contract_under_the_same_operation_is_retained_as_unqualified(self):
        original = json.loads(self.record.read_bytes())
        spec = {**self.selection[0],"operation_sha256":digest(encoded(original["tools"][0]))}
        operation = original["tools"][0]
        operation["inputSchema"] = {"type":"object","properties":{"include_extra": {"type":"boolean"}}}
        self.record.write_text(json.dumps(original))
        report,ledger,cards = reconcile(self.mount,self.tables,[spec])
        self.assertEqual("operation_schema_changed",ledger[0]["source_check"])
        self.assertEqual([],cards["cards"])
        self.assertEqual(1,len(report["incomplete_selections"]))

    def test_server_lists_and_registry_only_records_are_different_evidence(self):
        (self.mount/"registry.json").write_text(json.dumps({"servers":{"empty":{}}}))
        lines = "server_id\tn_tools\tcatalog_or_registry\n"
        lines += "archive\t1\tmcp.json\nempty\t0\tregistry.json\nmislabelled\t2\tmcp.json\n"
        (self.tables/"mcp-servers.tsv").write_text(lines)
        report,_,_ = reconcile(self.mount,self.tables,[])
        self.assertEqual({"server_tool_count_verified":1,"registry_only_server":1,"identity_mismatch":1},
                         report["tables"]["mcp-servers"]["source_checks"])

    def test_missing_original_keeps_historical_row(self):
        self.record.unlink()
        report,ledger,cards = reconcile(self.mount,self.tables,self.selection)
        self.assertEqual(1,report["tables"]["mcp-tools"]["rows"])
        self.assertEqual("missing_source",ledger[0]["source_check"])
        self.assertFalse(cards["cards"])

    def test_duplicate_source_rows_retained_without_silent_curated_collision(self):
        self.write_table([self.row,self.row])
        summary,ledger,_ = reconcile(self.mount,self.tables,[])
        self.assertEqual(2,len(ledger))
        self.assertEqual(1,summary["tables"]["mcp-tools"]["distinct_historical_keys"])
        self.assertEqual(1,summary["tables"]["mcp-tools"]["distinct_complete_rows"])
        with self.assertRaisesRegex(ValueError,"multiple historical rows"):
            reconcile(self.mount,self.tables,self.selection)


if __name__ == "__main__":
    unittest.main()
