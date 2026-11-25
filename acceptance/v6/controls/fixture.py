"""Evaluator-side construction of the published matched prestate and three private worlds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess

V6 = Path(__file__).resolve().parents[1]
PRODUCT = V6.parents[1]
COORD = PRODUCT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
PDB_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
CAPABILITIES = ["artifact-analysis@1", "tl1a-coordinate-quality@1", "tl1a-geometry@1", "tl1a-review@1"]
# Exactly one experimental native turn. Seeding the matched state uses no paid call.
LIMITS = {
    "api_calls": 1,
    "tokens": 1000000,
    "job_submissions": 3,
    "cpu_seconds": 300,
    "gpu_minutes": 0,
    "gpu_jobs": 0,
    "worker_tasks": 20,
    "wall_hours": 1,
}


def read(path):
    return json.loads(path.read_bytes())


def save(path, data):
    path.write_text(
        json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def digest(obj):
    return hashlib.sha256(
        json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_objects():
    suite = read(V6 / "reviewer/suite.json")
    goal = read(V6 / "inputs/goal.json")
    initial = read(V6 / "inputs/initial.json")
    if digest(goal) != suite["goal_digest"] or digest(initial) != suite["base_digest"]:
        raise ValueError("Matched design bytes do not match the published logical baseline")
    packets = []
    for label, wanted in suite["packet_digests"].items():
        packet = read(V6 / "inputs" / (label + ".json"))
        if (
            digest(packet) != wanted
            or packet["base_digest"] != digest(initial)
            or packet["packet_id"] != label
        ):
            raise ValueError("Matched intervention failed its registered digest")
        packets.append(packet)
    if len(packets) != 3:
        raise ValueError("Expected the three frozen interventions")
    return goal, initial, packets


def citation_rows():
    if file_sha(COORD) != PDB_SHA:
        raise ValueError("2RE9 coordinate pin changed")
    lines = COORD.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "_citation.id")
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "#")
    columns = []
    while lines[start].strip().startswith("_citation."):
        columns.append(lines[start].strip().split(".")[1])
        start += 1
    values = shlex.split(" ".join(lines[start:end]), posix=True)
    if len(values) % len(columns):
        raise ValueError("Incomplete citation loop in pinned source")
    return [
        {col: row[columns.index(col)] for col in ("id", "title", "pdbx_database_id_DOI")}
        for row in (values[i : i + len(columns)] for i in range(0, len(values), len(columns)))
    ]


def setup_source():
    # The same declaration can run on a dirty development checkout. Pin actual source
    # bytes at preparation, alongside the enclosing Git revision.
    files = [
        *sorted((PRODUCT / "gx-h/gx_core/p0").glob("v6*")),
        *(
            PRODUCT / "gx-h/gx_core" / name
            for name in (
                "p0/roots.py",
                "p0/root_delivery.py",
                "p0/cli.py",
                "p0/driver.py",
                "orchestration/reactor.py",
                "orchestration/engine.py",
                "orchestration/service.py",
                "jobs/service.py",
            )
        ),
        *(V6 / "controls" / name for name in ("run.py", "fixture.py", "review.py")),
        V6 / "live.py",
        V6 / "verify.py",
        V6 / "reviewer/suite.json",
    ]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=PRODUCT, check=True, capture_output=True, text=True
    ).stdout.strip()
    return {
        "git_head": revision,
        "source_sha256": {str(p.relative_to(PRODUCT)): file_sha(p) for p in files},
    }


def prepare(private, reviewer, *, api_calls=1, packets=None, run_options=None):
    private, reviewer = private.resolve(), reviewer.resolve()
    if private == reviewer or private in reviewer.parents or reviewer in private.parents:
        raise ValueError("Choose disjoint private-state and reviewer directories")
    if any(path == PRODUCT or PRODUCT in path.parents for path in (private, reviewer)):
        raise ValueError("Use working directories outside the source tree")
    if private.exists() or reviewer.exists():
        raise FileExistsError("Use new working directories to preserve prior trials")
    goal, initial, public = fixture_objects()
    packets = list(public if packets is None else packets)
    if not packets or any(p['base_digest'] != digest(initial) for p in packets):
        raise ValueError('Intervention must match the frozen initial state')
    citations = citation_rows()
    private.mkdir(parents=True, mode=0o700)
    reviewer.mkdir(parents=True, mode=0o700)
    secrets.SystemRandom().shuffle(packets)
    plan = []
    for packet in packets:
        slot = secrets.token_hex(8)
        world = private / slot
        visible = world / "inputs"
        visible.mkdir(mode=0o700, parents=True)
        review = reviewer / slot
        review.mkdir(mode=0o700)
        # No evaluator code, packet registry, controls labels or previous results in Lead-visible inputs.
        shutil.copyfile(COORD, visible / "structure.cif")
        for source in ("goal", "initial"):
            shutil.copyfile(V6 / "inputs" / (source + ".json"), visible / (source + ".json"))
        save(visible / "citations.json", citations)
        save(
            visible / "index-spec.json",
            {
                "script": "{'entries': count(data['A']), 'identifiers': column(data['A'], 'id')}",
                "output_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "entries": {"type": "integer"},
                        "identifiers": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                    },
                    "required": ["entries", "identifiers"],
                },
            },
        )
        inputs = {
            name: visible / filename
            for name, filename in (
                ("coordinates", "structure.cif"),
                ("goal", "goal.json"),
                ("prior_state", "initial.json"),
                ("citation_table", "citations.json"),
                ("citation_program", "index-spec.json"),
            )
        }
        save(
            world / "goal-config.json",
            {
                **(run_options or {}),
                "project": "v6-matched-controls",
                "capabilities": CAPABILITIES,
                "run_budget": {**LIMITS, "api_calls": api_calls},
                "workspace_budget": {**LIMITS, "api_calls": api_calls},
                "turn_token_budget": 65536,
                "input_files": [{"path": str(path), "role": role} for role, path in inputs.items()],
            },
        )
        if packet in public:
            shutil.copyfile(V6 / "inputs" / (packet["packet_id"] + ".json"), review / "intervention.json")
        else:
            save(review / "intervention.json", packet)
        plan.append(
            {
                "slot": slot,
                "packet_id": packet["packet_id"],
                "packet_sha256": digest(packet),
                "visible_initial_sha256": {role: file_sha(path) for role, path in inputs.items()},
            }
        )
    if len({digest(row["visible_initial_sha256"]) for row in plan}) != 1:
        raise AssertionError("Unequal initial data")
    save(
        reviewer / "plan.json",
        {
            "version": 1,
            "goal_digest": digest(goal),
            "initial_digest": digest(initial),
            "design": f"{len(packets)} synthetic matched interventions",
            "api_calls_per_run": api_calls,
            "source": setup_source(),
            "worlds": plan,
        },
    )
    return plan


def seed_decision(refs, initial):
    """Evaluator-owned starting graph for all cases. Only the subsequent decision is scored."""

    def commission(branch):
        geometry = branch["id"] == "geometry"
        return {
            "op": "open_branch",
            "branch": "local:" + branch["id"],
            "hypothesis": branch["commission"],
            "alternative": (
                "Results are limited to a subset of measured masks."
                if geometry
                else "Some records lack a citation."
            ),
            "method_family": "coordinate measurement" if geometry else "source catalog",
            "discriminating_observation": (
                "Matched measurements under the frozen contract."
                if geometry
                else "Number and identifiers of the citation records."
            ),
            "budget_limits": {
                "job_submissions": 2 if geometry else 1,
                "cpu_seconds": 120 if geometry else 30,
            },
            "stop_condition": (
                "Reassess when evidence arrives."
                if geometry
                else "Archive the deposited citation index."
            ),
        }

    return {
        "mode": "amend",
        "rationale": "Establish the matched synthetic starting state and index deposited source metadata.",
        "evidence_refs": [refs["prior_state"]],
        "reconsider_when": [{"event": "evidence_added", "subject_ref": "run"}],
        "ops": [
            *map(commission, initial["branches"]),
            {
                "op": "add_node",
                "branch": "local:citation-index",
                "node": "local:archive-index",
                "capability": "artifact-analysis@1",
                "inputs": [refs["citation_program"], refs["citation_table"]],
                "parameters": {},
                "depends_on": [],
                "question": "Which citation identifiers are listed in the deposited archive?",
            },
        ],
    }
