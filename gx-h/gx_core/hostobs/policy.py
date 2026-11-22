"""The role and model policy, as code reads it.

``config/role_policy.json`` says which model does the work and at what effort. Nothing in
this module is a prompt: the role-file generator, the task service, the host hooks and the
submission gate all ask the same functions, so the four cannot disagree about what the user
required.

Three kinds of question are answered here.

* What does this class of work require?  :func:`required_for`, :func:`required_for_role`: the
  model the policy gives the class (``task_classes.<class>.model``; the pin where a class
  names none) and its efforts. ``pinned_model.id`` is the default and the model of the session
  that coordinates a run; a class may name another model only if the entry in force of the pin
  history allows it beside the pin (``also_allowed``).
* Does this name mean that model?  :func:`names_model` for something a person or a model
  *said* (a short name counts), :func:`is_model` for something a host *recorded* (only the id
  counts: a record that says ``fable`` instead of the id is a format this code was not
  written against). :func:`names_pinned_model` and :func:`is_pinned_model` ask it of the pin.
* How does a worker stand against the policy?  :func:`judge`, a pure function from what was
  reported and what was observed to a value of ``labels.model_policy_verdict``. A worker is
  judged against its own task's requirement, never against the pin as such.

The policy file is re-read on every call. A policy read once at start-up is one that cannot
be tightened while a run is in progress, and the sha256 a run pins must be the sha256 of the
bytes on disk when it started.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import contracts
from ..domain import clock
from ..errors import SchemaInvalid

POLICY_PATH = contracts.CONFIG_DIR / "role_policy.json"

# The five verdicts and the two failure classes this module produces. Each is checked against
# the frozen contract at import, so a renamed label stops the backend instead of being written
# into a row nobody can interpret.
CONFORMS = "CONFORMS"
REPORTED_ONLY = "REPORTED_ONLY"
UNOBSERVABLE = "UNOBSERVABLE"
MISMATCH = "MISMATCH"
REFUSAL_OR_FALLBACK = "REFUSAL_OR_FALLBACK"
VERDICTS = (CONFORMS, REPORTED_ONLY, UNOBSERVABLE, MISMATCH, REFUSAL_OR_FALLBACK)

FAILURE_POLICY = "host_model_policy"
FAILURE_REFUSAL = "host_model_refusal"

for _value in VERDICTS:
    contracts.require_label("model_policy_verdict", _value)
for _value in (FAILURE_POLICY, FAILURE_REFUSAL):
    if _value not in contracts.failure_classes():
        raise RuntimeError(f"failure class {_value!r} is not in contracts/errors.json")
del _value

#: The key lists a transcript reader takes from the policy (``observation.transcript_reader``).
READER_FIELD_LISTS = (
    "row_fields", "message_fields", "stop_detail_fields", "usage_fields", "request_fields",
    "request_param_fields", "meta_fields",
)

#: Lists a reader takes when the policy has them and does without otherwise: the keys of a line
#: on which the host announces the session's model.
READER_OPTIONAL_LISTS = ("attachment_fields", "attachment_identity_fields")
#: Keys that hold text wherever they occur. No list of a reader may name one.
READER_TEXT_KEYS = frozenset({"text", "content", "prompt", "description", "summary", "stdout", "stderr", "rendered"})
#: The one key of an agent's meta a reader may look at for a worker subject, and only for that
#: (``meta_subject_field`` / ``meta_subject_pattern`` / ``meta_subject_rule`` of a reader): the
#: host writes the label a launcher gave an agent under it. The pattern
#: lives in the policy; this module holds no copy of it.
META_SUBJECT_KEY = "description"

#: What a caller does with a verdict at each of the two places one is asked for: a worker
#: that binds is accepted or refused; a submission is accepted or held for the user.
BIND_ACCEPT, BIND_REFUSE = "accept", "refuse"
SUBMIT_ACCEPT, SUBMIT_HOLD = "accept", "hold"


# ----------------------------------------------------------------------------- loading

def load_policy(path: str | Path | None = None) -> tuple[dict[str, Any], str]:
    """The policy document and the sha256 of the bytes it was parsed from."""
    target = Path(path) if path is not None else POLICY_PATH
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise SchemaInvalid(
            f"the agent policy {target} cannot be read: {exc}", details={"path": str(target)}
        ) from exc
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SchemaInvalid(
            f"the agent policy {target} is not valid JSON: {exc}", details={"path": str(target)}
        ) from exc
    problems = problems_in(policy)
    if problems:
        raise SchemaInvalid(
            f"the agent policy {target} is not usable: " + "; ".join(problems),
            details={"path": str(target), "problems": problems},
        )
    return policy, hashlib.sha256(raw).hexdigest()


def agent_policy() -> tuple[dict[str, Any], str]:
    """``(policy, sha256)`` of ``config/role_policy.json`` as it is on disk now.

    A run stores both when it starts (``runs.agent_policy_json``, ``agent_policy_sha256``);
    resume compares the sha256 with a fresh call and refuses on a difference.
    """
    return load_policy(POLICY_PATH)


def problems_in(policy: Any) -> list[str]:
    """Everything that makes a policy document unusable. An empty list means usable.

    Neither host validates a model or an effort value in a role file (observed: both accept
    ``effort: enormous``), so the base vocabulary comes from the frozen contract. Only
    the backend-selected Codex host-specific policy may append its verified ``ultra``
    level. A scope marker validates a document; it never selects or authorizes a Host.
    """
    problems: list[str] = []
    if not isinstance(policy, dict):
        return ["the document is not a JSON object"]

    pinned = policy.get("pinned_model")
    if not isinstance(pinned, dict) or not _text(pinned.get("id")):
        problems.append("pinned_model.id is missing")
    else:
        aliases = pinned.get("aliases", [])
        if not isinstance(aliases, list) or not all(_text(alias) for alias in aliases):
            problems.append("pinned_model.aliases is not a list of names")
        problems.extend(_history_problems(pinned))

    order = policy.get("effort_order")
    contract_order = contracts.label_values("effort_level")
    codex_scoped = (policy.get("policy_host") == "codex"
                    and policy.get("policy_scope") == "host-specific-v1")
    allowed_orders = [contract_order]
    if codex_scoped:
        allowed_orders.append([*contract_order, "ultra"])
    if order not in allowed_orders:
        suffix = " or that exact order with 'ultra' appended for scoped Codex" if codex_scoped else ""
        problems.append(f"effort_order {order!r} is not the contract's effort_level order {contract_order!r}{suffix}")
        order = contract_order

    classes = policy.get("task_classes")
    contract_classes = contracts.label_values("task_class")
    if not isinstance(classes, dict) or sorted(classes) != sorted(contract_classes):
        problems.append(f"task_classes must define exactly {sorted(contract_classes)}")
        classes = classes if isinstance(classes, dict) else {}
    # The models a class may name: the pin, and what the history's entry in force allows beside it.
    allowed_now = _allowed_ids(pinned) if isinstance(pinned, dict) and _text(pinned.get("id")) else None
    for name, spec in classes.items():
        if not isinstance(spec, dict):
            problems.append(f"task_classes.{name} is not an object")
            continue
        low, default = spec.get("min_effort"), spec.get("default_effort")
        if low not in order or default not in order:
            problems.append(f"task_classes.{name} names an effort outside {order}")
        elif order.index(default) < order.index(low):
            problems.append(f"task_classes.{name}.default_effort {default!r} is below its min_effort {low!r}")
        model = spec.get("model")
        if model is None or allowed_now is None:
            continue
        if model not in allowed_now:        # whatever it is: another id, a short name, not a name at all
            problems.append(
                f"task_classes.{name}.model {model!r} is neither pinned_model.id nor a model the entry in force "
                f"of pinned_model.history allows beside it (also_allowed); a class may name one of {allowed_now}, "
                "written as the id itself"
            )
    problems.extend(_model_name_problems(policy, classes))

    roles = policy.get("roles")
    contract_roles = contracts.label_values("worker_role")
    if not isinstance(roles, dict) or sorted(roles) != sorted(contract_roles):
        problems.append(f"roles must define exactly {sorted(contract_roles)}")
        roles = roles if isinstance(roles, dict) else {}
    for name, spec in roles.items():
        if not isinstance(spec, dict) or spec.get("task_class") not in classes:
            problems.append(f"roles.{name}.task_class is not one of the task classes")
        elif not _text(spec.get("does")):
            problems.append(f"roles.{name}.does is missing: a role file needs the role's intent")

    observation = policy.get("observation")
    verdicts = set(contracts.label_values("model_policy_verdict"))
    sources = set(contracts.label_values("observation_source"))
    if not isinstance(observation, dict):
        problems.append("observation is missing")
    else:
        always = observation.get("always_waits_for_the_user")
        if not isinstance(always, list) or not set(always) <= verdicts:
            problems.append("observation.always_waits_for_the_user is not a list of verdicts")
            always = []
        hosts = observation.get("by_host")
        contract_hosts = contracts.label_values("host_kind")
        if not isinstance(hosts, dict) or sorted(hosts) != sorted(contract_hosts):
            problems.append(f"observation.by_host must define exactly {sorted(contract_hosts)}")
            hosts = hosts if isinstance(hosts, dict) else {}
        for host, spec in hosts.items():
            if not isinstance(spec, dict):
                problems.append(f"observation.by_host.{host} is not an object")
                continue
            accepts, holds = spec.get("accepts"), spec.get("holds")
            if not isinstance(accepts, list) or not isinstance(holds, list):
                problems.append(f"observation.by_host.{host} needs accepts and holds lists")
                continue
            if not set(accepts) <= verdicts or not set(holds) <= verdicts:
                problems.append(f"observation.by_host.{host} names a verdict the contract does not define")
            if set(accepts) & set(holds) or set(accepts) & set(always):
                problems.append(f"observation.by_host.{host} both accepts and holds one verdict")
            undecided = verdicts - set(accepts) - set(holds) - set(always)
            if undecided:
                problems.append(f"observation.by_host.{host} leaves {sorted(undecided)} undecided")
            if not set(spec.get("sources", [])) <= sources:
                problems.append(f"observation.by_host.{host}.sources names an unknown observation source")
        readers = observation.get("transcript_reader", {})
        if not isinstance(readers, dict):
            problems.append("observation.transcript_reader is not an object")
        else:
            for host, spec in readers.items():
                versions = spec.get("verified_on_versions") if isinstance(spec, dict) else None
                if not isinstance(versions, list) or not versions or not all(_text(v) for v in versions):
                    problems.append(f"observation.transcript_reader.{host}.verified_on_versions is empty")
                    continue
                if spec.get("on_unknown_version") != UNOBSERVABLE:
                    problems.append(
                        f"observation.transcript_reader.{host}.on_unknown_version must be {UNOBSERVABLE}: "
                        "a reader never guesses at a format it was not verified on"
                    )
                for name in READER_FIELD_LISTS:
                    listed = spec.get(name)
                    if not isinstance(listed, list) or not listed or not all(_text(item) for item in listed):
                        problems.append(
                            f"observation.transcript_reader.{host}.{name} must list the keys the reader "
                            "may copy; a reader with no list has nothing it may read"
                        )
                for name in READER_OPTIONAL_LISTS:
                    listed = spec.get(name)
                    if listed is None:
                        continue
                    if not isinstance(listed, list) or not listed or not all(_text(item) for item in listed):
                        problems.append(
                            f"observation.transcript_reader.{host}.{name}, when present, lists the keys "
                            "the reader may copy"
                        )
                    elif READER_TEXT_KEYS & set(listed):
                        problems.append(
                            f"observation.transcript_reader.{host}.{name} names "
                            f"{sorted(READER_TEXT_KEYS & set(listed))}, which hold text; a reader copies "
                            "identity and settings fields only"
                        )
                limit = spec.get("max_value_chars")
                if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1024:
                    problems.append(
                        f"observation.transcript_reader.{host}.max_value_chars must be a small positive "
                        "number: an identity or settings value is short, and text is not"
                    )
                problems.extend(_meta_subject_problems(spec, f"observation.transcript_reader.{host}"))
                never = set(spec.get("never_read") or [])
                listed_paths = set(spec.get("row_fields") or []) | {
                    f"message.{key}" for key in (spec.get("message_fields") or [])
                } | set(spec.get("meta_fields") or []) | {
                    f"attachment.{key}" for key in (spec.get("attachment_fields") or [])
                    if isinstance(spec.get("attachment_fields"), list)
                }
                if never & listed_paths:
                    problems.append(
                        f"observation.transcript_reader.{host} lists {sorted(never & listed_paths)} both as "
                        "read and as never read"
                    )

    deviation = policy.get("on_deviation")
    if not isinstance(deviation, dict):
        problems.append("on_deviation is missing")
    else:
        if deviation.get("approval_action_kind") not in contracts.label_values("approval_action_kind"):
            problems.append("on_deviation.approval_action_kind is not an approval action kind")
        if deviation.get("run_state") not in contracts.states()["machines"]["run"]["waiting"]:
            problems.append("on_deviation.run_state is not a waiting state of the run machine")
        if deviation.get("standing_policy_may_grant") is not False:
            problems.append(
                "on_deviation.standing_policy_may_grant must be false: a change of model is the "
                "one approval the user reserved"
            )
    return problems


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _meta_subject_problems(spec: Mapping[str, Any], where: str) -> list[str]:
    """The rule by which a reader may keep a worker subject from an agent's meta, when it has one.

    The key it names can hold free text, so the rule is narrow by construction: it may name
    only that one key, and its pattern is anchored at both ends, so nothing but a whole value
    of the subject's shape is ever kept.
    """
    field, pattern = spec.get("meta_subject_field"), spec.get("meta_subject_pattern")
    if field is None and pattern is None:
        return []
    problems: list[str] = []
    if field != META_SUBJECT_KEY:
        problems.append(
            f"{where}.meta_subject_field may name only {META_SUBJECT_KEY!r}, the key the host writes a "
            f"launcher's label under; it names {field!r}"
        )
    if not _text(pattern) or not pattern.startswith("^") or not pattern.endswith("$") or pattern.endswith("\\$"):
        problems.append(
            f"{where}.meta_subject_pattern must be anchored at both ends (^...$): only a whole value that "
            "is a worker subject is kept"
        )
    else:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            problems.append(f"{where}.meta_subject_pattern is not a pattern: {exc}")
        else:
            # A subject has no space in it; a pattern that lets one through would keep prose.
            if any(compiled.fullmatch(text) for text in ("", " ", "a b", "a\nb", "a\tb")):
                problems.append(f"{where}.meta_subject_pattern matches an empty value or one with white space in it")
    return problems


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value)


def _allowed_ids(pinned: Mapping[str, Any]) -> list[str]:
    """The pin and the models the history's entry in force allows beside it, as ids, the pin first.

    Reads a ``pinned_model`` object that has an id and may be malformed otherwise: what is
    malformed is reported by :func:`_history_problems` and allows nothing here.
    """
    allowed = [str(pinned["id"])]
    history = pinned.get("history")
    last = history[-1] if isinstance(history, list) and history and isinstance(history[-1], dict) else {}
    extra = last.get("also_allowed")
    for item in extra if isinstance(extra, list) else []:
        if isinstance(item, dict) and _text(item.get("model")) and str(item["model"]) not in allowed:
            allowed.append(str(item["model"]))
    return allowed


def _alias_sources(policy: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """Every place a policy gives a model short names, as ``(model id, aliases)``.

    The ``models`` table, the pin's own ``aliases``, and each history entry with the models it
    allows beside its pin. One model's short names are the union of what these say of it;
    :func:`_model_name_problems` refuses a document in which they contradict each other.
    """
    found: list[tuple[Any, Any]] = []
    table = policy.get("models")
    for model, spec in (table.items() if isinstance(table, dict) else []):
        if isinstance(spec, dict):
            found.append((model, spec.get("aliases")))
    pinned = policy.get("pinned_model")
    if isinstance(pinned, dict):
        found.append((pinned.get("id"), pinned.get("aliases")))
        history = pinned.get("history")
        for entry in (history if isinstance(history, list) else []):
            if not isinstance(entry, dict):
                continue
            found.append((entry.get("model"), entry.get("aliases")))
            extra = entry.get("also_allowed")
            for item in (extra if isinstance(extra, list) else []):
                if isinstance(item, dict):
                    found.append((item.get("model"), item.get("aliases")))
    return [(str(model), [alias for alias in aliases if _text(alias)] if isinstance(aliases, list) else [])
            for model, aliases in found if _text(model)]


def _model_name_problems(policy: Mapping[str, Any], classes: Mapping[str, Any]) -> list[str]:
    """The ``models`` table, and the short names of every model wherever the document gives them.

    A short name is what a spawn, a dispatch row or a worker's report may say instead of the
    id. One that could mean two models would let a request for one be served as the other, so
    it is refused here and not resolved by an order of precedence.
    """
    problems: list[str] = []
    table = policy.get("models")
    pinned = policy.get("pinned_model")
    if table is not None:
        if not isinstance(table, dict) or not table:
            problems.append("models is not a table of model ids with their short names")
        else:
            for model, spec in table.items():
                if not _text(model) or not isinstance(spec, dict) or not _names(spec.get("aliases", [])):
                    problems.append(f"models.{model} is not an object with a list of aliases")
            if isinstance(pinned, dict) and _text(pinned.get("id")):
                named = [spec.get("model") for spec in classes.values() if isinstance(spec, dict)]
                needed = list(dict.fromkeys([*_allowed_ids(pinned), *[str(model) for model in named if _text(model)]]))
                missing = [model for model in needed if model not in table]
                if missing:
                    problems.append(
                        f"models has no entry for {missing}: where the table is present it names every model a "
                        "worker can be required or allowed to run on"
                    )
    owners: dict[str, set[str]] = {}
    ids: set[str] = set()
    for model, aliases in _alias_sources(policy):
        ids.add(normalise_model(model))
        for alias in aliases:
            owners.setdefault(normalise_model(alias), set()).add(normalise_model(model))
    for alias, models in sorted(owners.items()):
        if len(models) > 1:
            problems.append(f"the short name {alias!r} is given to more than one model: {sorted(models)}")
        elif alias in ids and alias not in models:
            problems.append(f"the short name {alias!r} is the id of another model")
    return problems


def _canonical(value: Any) -> str | None:
    """A timestamp in the fixed-width UTC form the backend's clock and the host reader write."""
    if not _text(value):
        return None
    try:
        return clock.parse(value).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except ValueError:
        return None


def _history_problems(pinned: Mapping[str, Any]) -> list[str]:
    """``pinned_model.history``: dated entries in order, the last of them the pin in force now."""
    history = pinned.get("history")
    if history is None:
        return []
    if not isinstance(history, list) or not history:
        return ["pinned_model.history is not a list of dated entries"]
    problems: list[str] = []
    previous = None
    for position, entry in enumerate(history):
        if not isinstance(entry, dict) or not _text(entry.get("model")):
            problems.append(f"pinned_model.history[{position}] names no model")
            continue
        at = _canonical(entry.get("from"))
        if at is None:
            problems.append(f"pinned_model.history[{position}].from is not a time")
            continue
        if previous is not None and at <= previous:
            problems.append(f"pinned_model.history[{position}].from is not after the entry before it")
        previous = at
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list) or not all(_text(alias) for alias in aliases):
            problems.append(f"pinned_model.history[{position}].aliases is not a list of names")
        problems.extend(_also_allowed_problems(entry, f"pinned_model.history[{position}]"))
    last = history[-1] if isinstance(history[-1], dict) else {}
    if normalise_model(last.get("model")) != normalise_model(pinned.get("id")):
        problems.append("the last entry of pinned_model.history is not pinned_model.id, the pin in force now")
    return problems


def _also_allowed_problems(entry: Mapping[str, Any], where: str) -> list[str]:
    """``also_allowed`` of one history entry: the models the user allowed beside that pin, each for something."""
    extra = entry.get("also_allowed")
    if extra is None:
        return []
    if not isinstance(extra, list):
        return [f"{where}.also_allowed is not a list of models allowed beside the pin"]
    problems: list[str] = []
    seen = {normalise_model(entry.get("model"))}
    for index, item in enumerate(extra):
        if not isinstance(item, dict) or not _text(item.get("model")):
            problems.append(f"{where}.also_allowed[{index}] names no model")
            continue
        if normalise_model(item["model"]) in seen:
            problems.append(f"{where}.also_allowed[{index}] repeats a model of its entry")
        seen.add(normalise_model(item["model"]))
        if not _names(item.get("aliases", [])):
            problems.append(f"{where}.also_allowed[{index}].aliases is not a list of names")
        if not _text(item.get("for")):
            problems.append(
                f"{where}.also_allowed[{index}].for is missing: a model is allowed beside the pin for "
                "something the user named"
            )
    return problems


# ----------------------------------------------------------------------- pin over time

def pin_history(policy: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """``[{"from", "model", "aliases", "also_allowed"}]``, oldest first: what the user pinned from when.

    ``also_allowed`` lists the models the user allowed beside that pin (``{"model", "aliases",
    "for"}``), empty for an entry that allows none. A policy without a history has one entry,
    the pin in force now, from no particular time.
    """
    policy = policy if policy is not None else agent_policy()[0]
    pinned = policy["pinned_model"]
    entries = pinned.get("history") or []
    if not entries:
        return [{"from": None, "model": str(pinned["id"]), "aliases": list(pinned.get("aliases", [])),
                 "also_allowed": []}]
    return [{"from": _canonical(entry["from"]), "model": str(entry["model"]),
             "aliases": list(entry.get("aliases", [])),
             "also_allowed": [{"model": str(item["model"]), "aliases": list(item.get("aliases", [])),
                               "for": item.get("for")} for item in entry.get("also_allowed") or []]}
            for entry in entries]


def _entry_at(history: list[dict[str, Any]], moment: str | None) -> dict[str, Any]:
    chosen = history[0]
    for entry in history:
        if entry["from"] is None or (moment is not None and entry["from"] <= moment):
            chosen = entry
    return chosen


def pin_at(at: str | None, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The entry of the pin history in force at ``at`` (the record view of a turn).

    A verdict about a recorded turn is made against the pin the user had set when it was
    served; a change the history records is the user's, not drift. A run is judged against the
    policy it pinned at its start instead (the run view), which this function is not for. A
    time before the first entry is judged against the first entry.
    """
    return _entry_at(pin_history(policy), _canonical(at) if at is not None else None)


class PinOverTime:
    """``at -> model``: the pin in force at a turn's time, for the record view of a whole record.

    Calling it gives the pin (what a report names as the model that was required then).
    :meth:`allowed` gives every model a turn served at that time may have run on without being
    drift: the pin, and the models the user allowed beside it from that entry on. ``at`` is a
    timestamp in the fixed-width UTC form the reader writes.
    """

    def __init__(self, history: list[dict[str, Any]]):
        self._history = history

    def __call__(self, at: str) -> str:
        return _entry_at(self._history, at)["model"]

    def allowed(self, at: str) -> tuple[str, ...]:
        entry = _entry_at(self._history, at)
        return (entry["model"], *(item["model"] for item in entry["also_allowed"]))


def model_at(policy: Mapping[str, Any] | None = None) -> PinOverTime:
    """The record view of a policy: see :class:`PinOverTime`."""
    return PinOverTime(pin_history(policy))


# ------------------------------------------------------------------------------ models

def normalise_model(name: Any) -> str:
    """A model name with case, surrounding space and a context-window suffix removed."""
    if not isinstance(name, str):
        return ""
    text = name.strip().lower()
    if text.endswith("[1m]"):
        text = text[:-4].rstrip()
    return text


def pinned_model(policy: Mapping[str, Any] | None = None) -> str:
    policy = policy if policy is not None else agent_policy()[0]
    return str(policy["pinned_model"]["id"])


def is_pinned_model(name: Any, policy: Mapping[str, Any] | None = None) -> bool:
    """True only for the pinned model id itself. For what a host record says."""
    text = normalise_model(name)
    return bool(text) and text == normalise_model(pinned_model(policy))


def names_pinned_model(name: Any, policy: Mapping[str, Any] | None = None) -> bool:
    """True for the pinned id or one of its aliases. For what a request or a report says."""
    policy = policy if policy is not None else agent_policy()[0]
    return names_model(name, policy["pinned_model"]["id"], policy)


def is_model(name: Any, model: Any) -> bool:
    """True only for the id of ``model`` itself. For what a host record says."""
    text = normalise_model(name)
    return bool(text) and text == normalise_model(model)


def aliases_of(model: Any, policy: Mapping[str, Any] | None = None) -> list[str]:
    """The short names a request or a report may use for a model, in the order the policy gives them."""
    policy = policy if policy is not None else agent_policy()[0]
    wanted = normalise_model(model)
    found: list[str] = []
    for candidate, aliases in _alias_sources(policy):
        if wanted and normalise_model(candidate) == wanted:
            found.extend(alias for alias in aliases if alias not in found)
    return found


def names_model(name: Any, model: Any, policy: Mapping[str, Any] | None = None) -> bool:
    """True for the id of ``model`` or one of its short names. For what a request or a report says."""
    policy = policy if policy is not None else agent_policy()[0]
    text = normalise_model(name)
    if not text or not normalise_model(model):
        return False
    return text in {normalise_model(candidate) for candidate in (model, *aliases_of(model, policy))}


def short_name(model: Any, policy: Mapping[str, Any] | None = None) -> str:
    """What to call a model where a host takes a short name (the Agent tool's ``model``): its first alias, else its id."""
    aliases = aliases_of(model, policy)
    return str(aliases[0] if aliases else model)


def allowed_models(policy: Mapping[str, Any] | None = None) -> list[str]:
    """The pin and the models the user allows beside it now (``also_allowed`` of the entry in force), the pin first.

    Every model a class of work can require is one of these. A worker the backend can tie to
    no task and no role is held to this list, because the user's rule covers every agent and
    names no kind of work for it.
    """
    policy = policy if policy is not None else agent_policy()[0]
    return _allowed_ids(policy["pinned_model"])


def class_model(task_class: str, policy: Mapping[str, Any] | None = None) -> str:
    """The model the policy gives a class of work: the one the class names, else the pin."""
    policy = policy if policy is not None else agent_policy()[0]
    return str(_class_spec(policy, task_class).get("model") or policy["pinned_model"]["id"])


def models_in_use(policy: Mapping[str, Any] | None = None) -> list[str]:
    """Every model some role's work requires under this policy, the pin first."""
    policy = policy if policy is not None else agent_policy()[0]
    found = [str(policy["pinned_model"]["id"])]
    for spec in policy["task_classes"].values():
        if spec.get("model") and str(spec["model"]) not in found:
            found.append(str(spec["model"]))
    return found


# ----------------------------------------------------------------------------- efforts

def effort_rank(effort: Any, policy: Mapping[str, Any] | None = None) -> int | None:
    """Position of an effort level in the policy's order, or None if it is not a level."""
    policy = policy if policy is not None else agent_policy()[0]
    order = list(policy["effort_order"])
    text = effort.strip().lower() if isinstance(effort, str) else None
    return order.index(text) if text in order else None


def allowed_efforts(task_class: str, policy: Mapping[str, Any] | None = None) -> list[str]:
    """Every effort level at or above the minimum the task class requires."""
    policy = policy if policy is not None else agent_policy()[0]
    spec = _class_spec(policy, task_class)
    order = list(policy["effort_order"])
    return order[order.index(spec["min_effort"]):]


def floor_effort(policy: Mapping[str, Any] | None = None) -> str:
    """The lowest minimum over all task classes: what any worker at all must meet.

    A spawn the backend cannot tie to a task or a role still falls under the user's rule,
    whose most permissive line is the one for simple execution.
    """
    policy = policy if policy is not None else agent_policy()[0]
    order = list(policy["effort_order"])
    return min((spec["min_effort"] for spec in policy["task_classes"].values()), key=order.index)


def _class_spec(policy: Mapping[str, Any], task_class: str) -> Mapping[str, Any]:
    classes = policy["task_classes"]
    if task_class not in classes:
        raise SchemaInvalid(
            f"{task_class!r} is not a task class of the agent policy",
            details={"task_class": task_class, "known": sorted(classes)},
        )
    return classes[task_class]


#: The role that drives a run. It is not handed tasks; its model is the pin itself (design r2.2 §8.11).
COORDINATOR_ROLE = contracts.require_label("worker_role", "coordinator")


def _requirement(policy: Mapping[str, Any], sha256: str | None, task_class: str,
                 role: str | None = None) -> dict[str, Any]:
    """The requirement of a class of work; for the coordinating role, on the pin whatever its class names."""
    spec = _class_spec(policy, task_class)
    model = str(policy["pinned_model"]["id"]) if role == COORDINATOR_ROLE else class_model(task_class, policy)
    return {
        "task_class": task_class,
        "model": model,
        "model_aliases": aliases_of(model, policy),
        "min_effort": spec["min_effort"],
        "default_effort": spec["default_effort"],
        "allowed_efforts": allowed_efforts(task_class, policy),
        "policy_sha256": sha256,
    }


def required_for(task_class: str) -> dict[str, Any]:
    """What whoever does a task of this class must run on.

    ``model`` is the class's model (``task_classes.<class>.model``, the pin where the class
    names none) and goes to ``tasks.required_model``; ``allowed_efforts`` goes to
    ``tasks.required_effort_json``; ``default_effort`` is the level a dispatch plan asks for.
    """
    policy, sha256 = agent_policy()
    return _requirement(policy, sha256, task_class)


def task_class_for(role: str, policy: Mapping[str, Any] | None = None) -> str:
    policy = policy if policy is not None else agent_policy()[0]
    roles = policy["roles"]
    if role not in roles:
        raise SchemaInvalid(
            f"{role!r} is not a role of the agent policy; the one role vocabulary is "
            f"{sorted(roles)}",
            details={"role": role, "known": sorted(roles)},
        )
    return str(roles[role]["task_class"])


def required_for_role(role: str, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What a worker in this role must run on: the requirement of the role's class of work.

    The coordinating role runs on the pin itself, at the efforts of its class.
    """
    sha256 = None
    if policy is None:
        policy, sha256 = agent_policy()
    requirement = _requirement(policy, sha256, task_class_for(role, policy), role)
    requirement["role"] = role
    return requirement


def requirement_of_task(task: Mapping[str, Any], policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The requirement a task row carries, falling back to its class and then to its role.

    The row's own columns come first because they are what the run pinned when the task was
    created; a later edit of the policy file does not loosen a task that already exists. So
    ``required_model`` is the model of the task's class in the policy its run pinned, or the
    one model a person authorised for this task alone (design r2.2 §8.7), and a worker is
    judged against it. A row with no class and no role the policy knows is held to the pin
    and the floor of the efforts.
    """
    policy = policy if policy is not None else agent_policy()[0]
    role = task.get("role") if task.get("role") in policy["roles"] else None
    task_class = task.get("task_class") or (task_class_for(role, policy) if role else None)
    base = _requirement(policy, None, task_class, role) if task_class else {
        "task_class": None,
        "model": str(policy["pinned_model"]["id"]),
        "model_aliases": aliases_of(policy["pinned_model"]["id"], policy),
        "min_effort": floor_effort(policy),
        "default_effort": floor_effort(policy),
        "allowed_efforts": list(policy["effort_order"])[list(policy["effort_order"]).index(floor_effort(policy)):],
        "policy_sha256": None,
    }
    if task.get("required_model"):
        base["model"] = task["required_model"]
        base["model_aliases"] = aliases_of(task["required_model"], policy)
    stored = task.get("required_effort_json")
    if isinstance(stored, str) and stored.strip():
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = None
    if isinstance(stored, list) and stored:
        order = list(policy["effort_order"])
        levels = [level for level in order if level in stored]
        if levels:
            base["allowed_efforts"] = levels
            base["min_effort"] = levels[0]
    return base


# ------------------------------------------------------------------------------ verdict

def host_mode(host: str, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """How strictly a worker on this kind of host must be shown before its work is taken."""
    policy = policy if policy is not None else agent_policy()[0]
    contracts.require_label("host_kind", host)
    return dict(policy["observation"]["by_host"][host])


def reader_spec(host: str, policy: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """What the policy says about reading this host's own records, or None if it says nothing."""
    policy = policy if policy is not None else agent_policy()[0]
    spec = policy["observation"].get("transcript_reader", {}).get(host)
    return dict(spec) if isinstance(spec, dict) else None


def judge(
    *,
    host: str,
    required: Mapping[str, Any],
    reported: Mapping[str, Any] | None = None,
    observed: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    untied: bool = False,
) -> dict[str, Any]:
    """How a worker stands against the policy, from a report and from a host record.

    ``reported`` is what the worker said about itself: ``{"model", "effort"}``, either of
    which may be missing. ``observed`` is the summary of a host record over the turns in
    question (see ``observation.summarise``): ``readable``, ``turns``, ``turns_off_model``,
    ``turns_below_effort``, ``turns_effort_unknown`` and ``deviations``; None means no host
    record was read.

    Precedence, strongest finding first:

    1. A refusal or a fallback the host recorded during the work: ``REFUSAL_OR_FALLBACK``.
    2. A host record that shows a turn on another model than the work requires, or below the
       allowed effort: ``MISMATCH``. The model required is the task's own (``required["model"]``):
       a turn on the pin is a mismatch for a task whose class runs on another model.
    3. A host record that shows every turn on the required model at an allowed effort:
       ``CONFORMS``. If the worker's own report says otherwise the record still decides -
       a model's statement about itself is a report, never an authentication - and the
       disagreement is returned in ``report_disagrees`` so it is not lost.
    4. No usable host record: the report decides. One that names another model or a lower
       effort is ``MISMATCH``; one that matches is ``REPORTED_ONLY``; none at all, or one
       that leaves the model or the effort out, is ``UNOBSERVABLE``.

    ``untied``: on a host whose workers are shown through its hooks, no hook tied the
    worker's subject to one agent (or tied it to the session's main thread for a bind that did
    not declare single_agent_sequential). Then no record was read on purpose, and a report
    that matches is ``UNOBSERVABLE``, not ``REPORTED_ONLY`` (design r2 §8.2): the backend
    cannot say whose report it is. A report outside the policy is still ``MISMATCH``.
    """
    policy = policy if policy is not None else agent_policy()[0]
    mode = host_mode(host, policy)
    always = list(policy["observation"]["always_waits_for_the_user"])
    allowed = list(required["allowed_efforts"])
    reasons: list[str] = []

    report = _judge_report(reported, required, policy, allowed)
    record = _usable(observed)

    failure_class: str | None = None
    if record is not None and record.get("deviations"):
        verdict = REFUSAL_OR_FALLBACK
        kinds = sorted({str(item.get("kind")) for item in record["deviations"]})
        switched = "fallback" in kinds or int(record.get("turns_off_model") or 0) > 0
        failure_class = FAILURE_POLICY if switched else FAILURE_REFUSAL
        reasons.append(
            f"the host recorded {len(record['deviations'])} {' and '.join(kinds)} event(s) during the work"
        )
        basis = "host_record"
    elif record is not None and (record["turns_off_model"] or record["turns_below_effort"]):
        verdict = MISMATCH
        failure_class = FAILURE_POLICY
        if record["turns_off_model"]:
            reasons.append(
                f"{record['turns_off_model']} of {record['turns']} observed turn(s) ran on a model "
                f"other than {_or(models_of(required))}: {_counts(record.get('models'))}"
            )
        if record["turns_below_effort"]:
            reasons.append(
                f"{record['turns_below_effort']} of {record['turns']} observed turn(s) ran below the "
                f"allowed effort {allowed}: {_counts(record.get('efforts'))}"
            )
        reasons.extend(_stretch_reason(stretch) for stretch in (record.get("stretches") or [])[:4])
        basis = "host_record"
    elif record is not None and not record.get("turns_effort_unknown"):
        verdict = CONFORMS
        reasons.append(
            f"all {record['turns']} observed turn(s) ran on {_or(models_of(required))} at an allowed effort"
        )
        basis = "host_record"
    else:
        basis = "report" if report["given"] else "nothing"
        if observed is None:
            reasons.append("no host record was read for this worker")
        elif not observed.get("readable"):
            reasons.append(f"the host record is not readable: {observed.get('reason') or 'no reason recorded'}")
        elif not int(observed.get("turns") or 0):
            reasons.append("the host record holds no turn of this worker in the period in question")
        else:
            reasons.append(
                f"{observed.get('turns_effort_unknown')} observed turn(s) carry no effort level, so "
                "an allowed effort cannot be shown from the record"
            )
        if report["outside"]:
            verdict = MISMATCH
            failure_class = FAILURE_POLICY
            reasons.extend(report["problems"])
        elif report["complete"] and untied:
            verdict = UNOBSERVABLE
            reasons.append("the worker's report matches the policy, but no hook of the host tied its subject to one "
                           "agent, so whose report it is cannot be shown")
        elif report["complete"]:
            verdict = REPORTED_ONLY
            reasons.append("the worker's own report matches the policy; a report is not an observation")
        else:
            verdict = UNOBSERVABLE
            reasons.append(
                "the worker reported " + ("only part of what the policy asks about" if report["given"] else "nothing")
            )

    waits = verdict in always
    accepted = verdict in mode["accepts"] and not waits
    return {
        "policy_verdict": verdict,
        "basis": basis,
        "host": host,
        "mode": mode.get("mode"),
        "reasons": reasons,
        "report_disagrees": bool(basis == "host_record" and report["outside"]),
        "report_problems": report["problems"],
        "failure_class": failure_class,
        "waits_for_user": waits,
        "bind_action": BIND_REFUSE if waits else BIND_ACCEPT,
        "submit_action": SUBMIT_ACCEPT if accepted else SUBMIT_HOLD,
    }


_CAUSE_TEXT = {
    "host_fallback": "the host recorded moving the session to it",
    "model_set_between_turns": "the session's model was set to it between turns and the host recorded no "
                               "fallback, which is what a switch made with the host's model command leaves",
    "began_off_policy": "the worker's first turn was already on it",
    "effort_below_requirement": "the model is the required one and the effort is not",
    "unrecorded": "the record does not show how it began",
}


def _stretch_reason(stretch: Mapping[str, Any]) -> str:
    """One off-policy stretch in words: which model, from when to when, how it began, whether it ended."""
    cause = str(stretch.get("cause") or "unrecorded")
    began = stretch.get("fallback") or {}
    when = began.get("at") or stretch.get("announced_at")
    end = (f"ended; the next turn, at {stretch.get('ended_at')}, was not part of it" if stretch.get("ended")
           else "not ended at the time of the read")
    return (
        f"{stretch.get('turns')} turn(s) on {stretch.get('model')} from {stretch.get('first_at')} to "
        f"{stretch.get('last_at')} ({end}): {_CAUSE_TEXT.get(cause, cause)}" + (f" at {when}" if when else "")
    )


def _usable(observed: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The observation, if it is a readable record with at least one turn in it.

    A record with no turn cannot show that the work ran on the pinned model, so it never
    produces ``CONFORMS``; but a refusal that left no turn behind is still a finding.
    """
    if observed is None or not observed.get("readable"):
        return None
    turns = int(observed.get("turns") or 0)
    if turns <= 0 and not observed.get("deviations"):
        return None
    return {
        "turns": turns,
        "turns_off_model": int(observed.get("turns_off_model") or 0),
        "turns_below_effort": int(observed.get("turns_below_effort") or 0),
        "turns_effort_unknown": int(observed.get("turns_effort_unknown") or 0),
        "deviations": list(observed.get("deviations") or []),
        "models": dict(observed.get("models") or {}),
        "efforts": dict(observed.get("efforts") or {}),
        "stretches": [dict(item) for item in (observed.get("stretches") or []) if isinstance(item, Mapping)],
    }


def _judge_report(
    reported: Mapping[str, Any] | None,
    required: Mapping[str, Any],
    policy: Mapping[str, Any],
    allowed: list[str],
) -> dict[str, Any]:
    model = (reported or {}).get("model")
    effort = (reported or {}).get("effort")
    problems: list[str] = []
    # The model its own work requires, by id or by short name. The pin's name does not pass for
    # a task whose class runs on another model, and that model's name does not pass for the pin.
    if _text(model) and not any(names_model(model, wanted, policy) for wanted in models_of(required)):
        problems.append(f"the worker reported model {model!r}; the policy requires {_or(models_of(required))}")
    if _text(effort) and str(effort).strip().lower() not in allowed:
        problems.append(f"the worker reported effort {effort!r}; the policy allows {allowed}")
    return {
        "given": _text(model) or _text(effort),
        "complete": _text(model) and _text(effort) and not problems,
        "outside": bool(problems),
        "problems": problems,
    }


def _counts(table: Mapping[str, Any] | None) -> str:
    if not table:
        return "{}"
    return ", ".join(f"{name} x{count}" for name, count in sorted(table.items(), key=lambda item: (-item[1], item[0])))


def models_of(required: Mapping[str, Any]) -> list[str]:
    """The models a requirement is met on: the one its task or role requires, or, for a worker
    tied to neither, every model the policy allows now (``requirement_for_spawn``)."""
    listed = required.get("models")
    if listed is not None:
        return [str(model) for model in listed]
    return [str(required["model"])] if required.get("model") else []


def _or(names: list[str]) -> str:
    return " or ".join(names) if names else "no model"


# -------------------------------------------------------------------------------- spawn

SPAWN_ALLOW, SPAWN_DENY, SPAWN_ASK = "allow", "deny", "ask"
_NOT_A_MODEL = ("", "inherit")
_NOT_AN_EFFORT = ("", "auto", "unset")


def _distinct_models(names: list[Any]) -> list[str]:
    """The models named, each once (by id, whatever its case or window suffix), in the order first named."""
    found: dict[str, str] = {}
    for name in names:
        key = normalise_model(name)
        if key and key not in found:
            found[key] = str(name)
    return list(found.values())


def requirement_for_spawn(
    tasks: list[Mapping[str, Any]] | None = None, role: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What a worker about to be spawned must run on and at.

    For named tasks: the model they require and the efforts every one of them allows. Tasks
    that require different models leave no model one worker could run on: ``models`` is then
    empty and ``models_in_conflict`` lists them. For a role: the model and efforts of its
    class of work (the pin for the coordinating role). For a spawn tied to neither: any model
    the policy allows now - the pin, and what the user allowed beside it - at the floor of the
    efforts, because the user's rule covers every agent and the backend cannot tell which kind
    of work this one is for.

    ``models`` is what the worker may run on; ``model`` is the one model required, the pin for
    an untied spawn, and None for tasks in conflict.
    """
    policy = policy if policy is not None else agent_policy()[0]
    order = list(policy["effort_order"])
    if tasks:
        requirements = [requirement_of_task(task, policy) for task in tasks]
        allowed = [level for level in order if all(level in item["allowed_efforts"] for item in requirements)]
        models = _distinct_models([item["model"] for item in requirements])
        one = len(models) == 1
        return {"model": models[0] if one else None, "models": models if one else [],
                "models_in_conflict": [] if one else models, "allowed_efforts": allowed, "basis": "task",
                "task_classes": sorted({str(item["task_class"]) for item in requirements})}
    if role is not None and role in policy["roles"]:
        requirement = _requirement(policy, None, task_class_for(role, policy), role)
        return {"model": requirement["model"], "models": [requirement["model"]], "models_in_conflict": [],
                "allowed_efforts": requirement["allowed_efforts"], "basis": "role",
                "task_classes": [requirement["task_class"]]}
    floor = floor_effort(policy)
    models = allowed_models(policy)
    return {"model": models[0], "models": models, "models_in_conflict": [],
            "allowed_efforts": order[order.index(floor):], "basis": "floor", "task_classes": []}


def judge_spawn(
    *,
    required: Mapping[str, Any],
    model: Any = None,
    effort: Any = None,
    definition_model: Any = None,
    definition_effort: Any = None,
    env_model: Any = None,
    env_force: bool = False,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether a spawn, as it is worded, may be made under the policy.

    The gate reads what the spawn *names*: a model and an effort, each in the spawn's own
    parameter or, failing that, in the agent definition it starts. On Claude Code 2.1.322 the
    parameter was seen to take precedence over the definition for the effort and for the
    model, a definition's model to be used when the spawn names none, and each to be served
    as named, by id and by short name.

    * **Model.** It must be named, and it must be the model the work requires
      (``requirement_for_spawn``): the task's own, the role's class's, or, for a spawn tied to
      neither, one the policy allows now. A spawn that names none is refused rather than left
      to inherit: an agent type may carry a default of its own that no file shows, and an
      unnamed model is how a worker ends up on another one. A spawn for tasks that require
      different models is refused whatever it names.
    * **Effort.** A level that is named and is below what the work allows is refused. A
      spawn that names none is let through: what a worker is served at cannot be settled by
      reading the request, in either direction (one session was seen serving `max` to agents
      asked for at `xhigh`), so it is judged from the host's record of the turns, at bind
      and at submission.

    ``CLAUDE_CODE_SUBAGENT_MODEL`` with ``CLAUDE_CODE_SUBAGENT_MODEL_FORCE`` is documented to
    override every other source of a subagent's model. That has not been observed here; a
    forced variable that names another model than the work requires refuses the spawn all the
    same, because the refusal costs one message and the alternative is a worker on another
    model.
    """
    policy = policy if policy is not None else agent_policy()[0]
    allowed = list(required["allowed_efforts"])
    models = models_of(required)
    conflict = [str(item) for item in required.get("models_in_conflict") or []]
    basis = required.get("basis")
    if len(models) == 1:
        wanted = {"task": f"its task requires {models[0]}",
                  "role": f"the role's class of work requires {models[0]}"}.get(basis, f"the policy pins {models[0]}")
    else:
        wanted = f"the policy allows {_or(models)} for a worker tied to no task and no role"

    def named(value: Any, excluded: tuple[str, ...]) -> str | None:
        text = value.strip() if isinstance(value, str) else ""
        return None if not text or text.lower() in excluded else text

    def fits(name: str) -> bool:
        return any(names_model(name, item, policy) for item in models)

    model_basis, named_model = next(
        ((basis, value) for basis, value in (("the spawn's model parameter", named(model, _NOT_A_MODEL)),
                                             ("the agent definition", named(definition_model, _NOT_A_MODEL))) if value),
        (None, None))
    effort_basis, named_effort = next(
        ((basis, value) for basis, value in (("the spawn's effort parameter", named(effort, _NOT_AN_EFFORT)),
                                             ("the agent definition", named(definition_effort, _NOT_AN_EFFORT))) if value),
        (None, None))

    problems: list[str] = []
    if conflict:
        problems.append(
            f"the tasks it names require different models ({', '.join(conflict)}); one worker runs on one "
            "model, so each of them is started by a spawn of its own"
        )
    forced = named(env_model, _NOT_A_MODEL) if env_force else None
    if forced is not None and not conflict and not fits(forced):
        problems.append(
            f"CLAUDE_CODE_SUBAGENT_MODEL is set to {forced!r} with CLAUDE_CODE_SUBAGENT_MODEL_FORCE, which the "
            f"host documents as overriding the model of every subagent; {wanted}"
        )
    if named_model is None:
        if len(models) == 1:
            how = f"pass model \"{short_name(models[0], policy)}\" with the spawn"
        elif conflict:
            how = "name the model of the one task a spawn is for"
        else:
            how = "pass one of " + ", ".join(f"\"{short_name(item, policy)}\"" for item in models) + " as model with the spawn"
        problems.append(f"it names no model; {how} or start a role whose definition names it")
    elif not conflict and not fits(named_model):
        problems.append(f"it names the model {named_model!r} (in {model_basis}); {wanted}")
    if named_effort is not None and named_effort.lower() not in allowed:
        problems.append(f"it names the effort {named_effort!r} (in {effort_basis}); this work allows {allowed}")
    return {
        "decision": SPAWN_DENY if problems else SPAWN_ALLOW,
        "reason": "; ".join(problems) if problems else None,
        "named_model": named_model, "model_basis": model_basis,
        "named_effort": named_effort, "effort_basis": effort_basis,
        "required": {"model": required.get("model"), "models": models, "allowed_efforts": allowed, "basis": basis},
    }


# --------------------------------------------------------------- the two calls core makes

def bind_worker(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_id: str,
    host: str,
    host_session_ref: str | None = None,
    agent_ref: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    delegation_mode: str | None = None,
    evidence_level: str = "CLIENT_REPORTED",
    actor: str = "backend",
    _codex_prepared: Any = None,
) -> dict[str, Any]:
    """Record a worker's report for a lease and judge it against what the host recorded.

    Called by ``gx_task_bind`` inside its transaction. The answer carries
    ``worker_observation_id``, ``observed``, ``policy_verdict`` and ``action``; an action of
    ``refuse`` is the caller's ``MODEL_POLICY_VIOLATION``.
    """
    from . import observation

    return observation.bind_worker(
        conn, task_id=task_id, lease_id=lease_id, host=host, host_session_ref=host_session_ref,
        agent_ref=agent_ref, model=model, effort=effort, delegation_mode=delegation_mode,
        evidence_level=evidence_level, actor=actor,
        **({"_codex_prepared": _codex_prepared} if _codex_prepared is not None else {}),
    )


def verdict_for(conn: sqlite3.Connection, *, task_id: str, lease_id: str, _codex_prepared: Any = None) -> dict[str, Any]:
    """The verdict a submission under this lease is accepted or held on.

    Called by ``gx_task_submit`` inside its transaction. It reads the host's own record of
    the worker for the period of the lease, writes what it found as an observation, and
    answers with ``policy_verdict``, ``worker_observation_id`` and ``action`` (``accept`` or
    ``hold``), plus the deviation to put in front of the user when there is one.
    """
    from . import observation

    return observation.verdict_for(conn, task_id=task_id, lease_id=lease_id,
        **({"_codex_prepared": _codex_prepared} if _codex_prepared is not None else {}))


def holder_stopped(conn: sqlite3.Connection, *, lease_id: str) -> dict[str, Any]:
    """Whether the worker that holds a lease has stopped, as far as the host's own record shows.

    Called by core's settle and by the reaction to a ``worker.stopped`` trigger (design r2.2
    sections 8.7, 8.8). It is decided when it is asked and never stored: a stop event is a
    hook's proposal (r2.2a section 8.2), and what bears it out is the agent's record at that
    moment. The answer is ``{"stopped", "cause", "host_event_id", "agent", "reason"}``, with
    ``stopped`` true only when

    * the host ties the lease's subject to one of its agents;
    * a hook reported that agent's stop (``subagent_stop``), a failed turn of the session's
      own thread (``stop_failure``), or the end of the session (``session_end``);
    * that agent's record holds no turn after the event: a worker that is still working
      contradicts a forged or early stop with its next turn, before anything is released;
    * for an agent started with the Agent tool, its meta says it finished. An agent of a
      Workflow script has no such mark: for it the stop rests on the absence of a
      later turn alone.

    No tie, no stop event, or a record that cannot be read: ``stopped`` is false and ``reason``
    says which. Reads the store and the host's records; writes nothing.
    """
    from . import observation

    return observation.holder_stopped(conn, lease_id=lease_id)


def stopped_holders(conn: sqlite3.Connection, *, run_id: str) -> list[dict[str, Any]]:
    """The run's live leases whose holders the host's record shows stopped (:func:`holder_stopped`).

    ``[{"lease_id", "task_id", "agent", "cause", "host_event_id"}]``: what a session claiming
    the coordinator may release (design r2.2 section 8.8.4). Writes nothing.
    """
    from . import observation

    return observation.stopped_holders(conn, run_id=run_id)


def record_verdict_for_lease(conn: sqlite3.Connection, *, task_id: str, lease_id: str, _codex_prepared: Any = None) -> dict[str, Any]:
    """Read the host's record for a lease now, write what it shows, and answer the verdict.

    Called by core's settle inside its transaction before it re-queues a lapsed lease, and by
    the reaction to a ``worker.verdict`` trigger (design r2.2 sections 8.7, 8.8.2). It is
    ``verdict_for`` without the rows that say "the worker submitted": the period judged is the
    lease's, the record is the one the host ties to the lease's subject, and one observation
    row is written when that record is usable. The answer is ``{"policy_verdict",
    "worker_observation_id", "waits_for_user", "failure_class", "refusal", "readable", "tied",
    "deviation", "reasons"}``: ``refusal`` is a refusal or a fallback the host recorded in the
    period; ``readable`` false or ``tied`` false is a record the backend could not read.
    """
    from . import observation

    return observation.record_verdict_for_lease(conn, task_id=task_id, lease_id=lease_id,
        **({"_codex_prepared": _codex_prepared} if _codex_prepared is not None else {}))


def claim_independence(conn: sqlite3.Connection, *, task: Mapping[str, Any], subject: str | None) -> dict[str, Any] | None:
    """Why a claim of a review task under ``subject`` breaks its independence, or None (design r2 §8.2).

    Called by ``gx_task_claim`` before it issues the lease. The claimant is the agent the
    host's hook saw run this very claim call: the PreToolUse(Bash) event of the call is written
    before the command runs, and a subject names one hand-out, so the subject's tie is that
    call's agent. The answer, when not None, is ``{"reason", "claimant", "authors", "basis"}``:
    the claimant's agent is on record for a lease of the authoring task (``basis``
    ``observed_author``), or, on a strict host, no hook tied the subject at all
    (``no_hook_tie``). Refuse the claim with INDEPENDENCE_REQUIRED and these as details.
    """
    from . import observation

    return observation.claim_independence(conn, task=task, subject=subject)
