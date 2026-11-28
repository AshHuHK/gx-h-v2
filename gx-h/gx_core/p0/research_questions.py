"""Resolve a visible scientific question using the identities of its existing Goal, route or Join."""
import re

from ..domain import ids
from ..errors import SchemaInvalid


def _key(brief, kind, value):
    return "q_" + ids.sha256_of_obj([brief["run_id"], brief["goal_version"], kind, value])[:32]


class Questions:
    def __init__(self, brief):
        self.brief = brief
        self.root = _key(brief, "goal", "goal")
        self.labels = {self.root: brief["goal"]}
        for route in brief.get("research_intents", ()):
            self.labels[route["intent"]] = route["question"]
        if module := brief.get("module"):
            self.labels[module.get("route_intent") or module["module_instance_id"]] = module["focus"]
        for report in brief.get("module_report_index", ()):
            commission = report.get("gx_facts", {}).get("commission", {})
            if commission.get("question_id") and commission.get("question"):
                self.labels.setdefault(commission["question_id"], commission["question"])
        self.joins = {}
        # Joins are individually named by a stable key. Two independent Joins
        # asking the same question must not merge merely because their wording matches.
        routes = dict(self.labels)
        for join in brief.get("research_joins", ()):
            label = join["question"]
            matches = [oid for oid, title in routes.items() if title == label]
            if label in ("goal", brief["goal"]):
                qid = self.root
            elif label in routes:
                qid = label
            elif len(matches) == 1:
                qid = matches[0]
            else:
                qid = _key(brief, "join", join["key"])
            self.joins[join["key"]] = qid
            self.labels.setdefault(qid, label)

    def lookup(self, label, *, create_for=None):
        label = label.strip()
        if label in ("goal", self.brief["goal"].strip()):
            return self.root
        if label in self.labels:
            return label
        matches = [qid for qid, title in self.labels.items() if title.strip() == label]
        if len(matches) == 1:
            return matches[0]
        if not matches and create_for is not None and not re.fullmatch(r"(?:q|module|intent)_[a-f0-9]{32}", label):
            qid = _key(self.brief, create_for, label)
            self.labels[qid] = label
            return qid
        reason = "ambiguous_core_question" if matches else "unknown_core_question"
        options = matches or list(self.labels)
        raise SchemaInvalid("Choose a question_id from the visible questions, or use goal for the PI question.",
                            details={"reason":reason, "options":[
                                {"question_id":qid, "question":self.labels[qid]} for qid in options]})

    def designate(self, item):
        label = item.get("question", "goal")
        if qid := item.get("question_id"):
            self.labels.setdefault(qid, label)
        else:
            # Legacy free-text nominations predate route IDs. Preserve them as
            # distinct authority-scoped links when no unique owner can be inferred.
            try:
                qid = self.lookup(label, create_for=item["linked_by"])
            except SchemaInvalid:
                qid = _key(self.brief, item["linked_by"], label)
                self.labels[qid] = label
        return {**item, "question_id":qid, "goal_version":self.brief["goal_version"]}
