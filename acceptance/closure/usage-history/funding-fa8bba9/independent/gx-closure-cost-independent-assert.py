"""Check independently observed accounting invariants from the read-only fixtures."""
import json
from pathlib import Path
import sys


data = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    data.update(json.loads(line))


def risk(record):
    return record["risk"]["observed_plus_settled"]


def settled(record):
    return record["risk"]["settled_tokens"]


def held_second(record):
    return record["turns"][1][1:3] == ["HELD", "RESERVED"]


checks = {}
for name in ("cross_funds", "same_fund"):
    before = data[name]["at_T2_terminal"]
    after = data[name]["after_T1_exact"]
    checks[name] = (held_second(before) and settled(before) == 100 and risk(before) == 145
                    and held_second(after) and settled(after) == 120 and risk(after) == 145
                    and after["unattributed"] == 25)
    if name == "cross_funds":
        checks[name] &= after["spent"][0]["tokens"] == 120 and after["spent"][1].get("tokens", 0) == 0

goal = data["unreserved"]
checks["unreserved"] = (settled(goal) == 120 and risk(goal) == 320 and goal["unattributed"] == 5
                        and goal["turns"][1][-1] == 0 and goal["risk"]["unfunded_closed_thread_tokens"] == 200)
checks["closed"] = all(settled(x) == 20 and risk(x) == 70 and x["unattributed"] == 50
                       for x in data["closed"].values())
duplicate = data["duplicate"]
checks["duplicate"] = held_second(duplicate) and settled(duplicate) == 20
historic = data["historical"]
checks["historical"] = all(risk(historic[k]) == settled(historic[k]) == amount for k, amount
                           in (("before", 100), ("after_new_exact20", 120)))
for name, delta, expected, total in (("positive", 40, 140, 150), ("negative", -40, 60, 95),
                                      ("positive_after_close", 40, 148, 148),
                                      ("negative_after_close", -40, 68, 68)):
    record = data[name]
    checks[name] = (["TURN_RECEIPT", delta] in record["adjustments"]
                    and settled(record) == expected and risk(record) == total)
    if name.endswith("_after_close"):
        checks[name] &= record["spent"][1]["tokens"] == 8 and record["spent"][0]["tokens"] == 100 + delta
mixed = data["historical_unreserved"]
checks["historical_unreserved"] = (all(settled(x) == 120 for x in mixed.values())
                                   and risk(mixed["after_host_goal"]) == 170
                                   and risk(mixed["with_open_turn"]) == 175
                                   and mixed["after_host_goal"]["risk"]["unfunded_closed_thread_tokens"] == 50)
two = data["historical_two_holds"]
checks["historical_two_holds"] = (two["reserved_tokens"] == 200 and two["observed_plus_settled"] == 340
                                  and two["committed_tokens"] >= 340)
if "historical_unreserved_raw_only" in data:
    # The independent raw 20 and 250 prove a 270 total even if an old unitemized
    # payment overlaps the thread. A provably later Goal exposes all 250.
    checks["historical_unreserved_raw_only"] = all(
        270 <= risk(x) <= 370 and settled(x) == 120 for x in data["historical_unreserved_raw_only"].values())
for name, success in checks.items():
    print(("PASS" if success else "FAIL"), name)
sys.exit(int(not all(checks.values())))
