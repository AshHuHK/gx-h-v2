"""Exercise the same-store receipt chain used by previously upgraded, still active static Runs."""
from contextlib import closing
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/"gx-h"))
from gx_core import contracts
from gx_core.persistence import store


class HistoricalPin(unittest.TestCase):
    def test_only_ordered_authenticated_edges_between_declared_pairs_can_establish_continuity(self):
        declared = contracts.additive_schema_sources()
        begin, middle = list(declared)[:2]
        final = contracts.contract_digest()["__combined__"]
        ddl = {**declared, final:contracts.contract_digest()["db_schema.sql"]}
        scenarios = {
            "ordered declared receipts": ([(begin,middle,"backend",ddl[middle]),(middle,final,"backend",ddl[final])],True),
            "backwards timestamps": ([(middle,final,"backend",ddl[final]),(begin,middle,"backend",ddl[middle])],False),
            "no origin": ([(middle,final,"backend",ddl[final])],False),
            "mismatched intermediate DDL": ([(begin,middle,"backend","0"*64),(middle,final,"backend",ddl[final])],False),
            "model wrote a similar event": ([(begin,middle,"model",ddl[middle]),(middle,final,"backend",ddl[final])],False),
            "unrecognized intermediate": ([(begin,"unknown","backend","9"*64),("unknown",final,"backend",ddl[final])],False),
        }
        for name,(edges,expected) in scenarios.items():
            with self.subTest(name=name), closing(store.open_store(":memory:")) as db:
                for old,new,actor,next_ddl in edges:
                    store.append_event(db,kind="store.contract_resynced",actor=actor,subject_kind="store",
                                       evidence_level="EXECUTION_OBSERVED",
                                       payload={"from_sha256":old,"from_db_schema_sha256":ddl.get(old,"9"*64),
                                                "to_sha256":new,"to_db_schema_sha256":next_ddl})
                self.assertIs(store.contract_pin_matches(db,begin,final),expected)

if __name__=="__main__":
    unittest.main()
