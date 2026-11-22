"""Retrieval memory: ``memory_docs`` is the record, two FTS5 tables are the index.

What this module is for, in the order the first build got it wrong:

**A search names its scope.** A namespace is ``<realm>:<project>[/<segment>…]`` and a
search reaches only the namespaces at or under the scopes it is given
(:mod:`.scopes`). There is no default: with no scope nothing is searched and the answer
says so. Records marked synthetic are refused in the ``real`` realm, so a test record
cannot be written where a real query would find it.

**The ranking is what it says it is.** How documents are ranked is a strategy read from
``strategy_pointers`` (:mod:`.strategy`), not code in here. Every answer states the
retrieval mode, each channel's status with its reason, and the strategy version that
ranked it. ``HYBRID`` is reported only if an embedding channel ran, which today it never
does.

**Failures and uncertainty are retrievable.** ``outcome`` is SUCCESS, FAILURE or UNCERTAIN
and the default search returns all three. A memory that only remembers successes is why a
team repeats a failed approach.

**A hit can be read, and what is read is what was stored.** A result is a pointer: a
snippet, the document's source refs and its locators. :meth:`MemoryService.read` serves the
body after re-hashing it against the sha256 taken at write time, and refuses a document
that was retracted, superseded, or rests on a withdrawn source.

**Every search is kept.** ``memory_queries`` records the query, the scopes, the channels,
the strategy version and the top results with their source refs: the audit record design
reference §4 asks for, written by the search itself.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .. import contracts
from ..domain import clock, ids
from ..errors import ArtifactHashMismatch, ArtifactNotFound, GroundsWithdrawn, SchemaInvalid, ScopeDenied
from ..persistence import store
from . import scopes as scope_rules
from . import strategy as strategy_rules
from . import tokenize
from .scopes import REALMS, is_synthetic, namespace_in_scope, parse_namespace  # noqa: F401  (re-exported)

#: Filter keys accepted by :meth:`MemoryService.search`. An unknown key is refused rather
#: than ignored: a filter that is silently dropped widens a scoped query.
ALLOWED_FILTER_KEYS = frozenset({"memory_class", "outcome", "namespace"})

#: Source refs of this shape are also written to ``dependencies``, which is how a
#: retraction finds the documents that rest on what was withdrawn.
_DEPENDENCY_REF = re.compile(r"^(source|evidence|asset|episode):(\S+)$")

DEFAULT_LIMIT = 10
MAX_LIMIT = 200
#: How many locators a search result carries. The document has them all.
RESULT_LOCATORS = 5
#: A body is served in slices that fit a response: bytes, not characters, because a
#: Chinese character is three.
READ_BYTES = 16000

_TABLE = {strategy_rules.CHANNEL_WORD: "memory_fts", strategy_rules.CHANNEL_CJK: "memory_fts_cjk"}


def outcome_values() -> list[str]:
    return contracts.label_values("memory_outcome")


#: Kept as a name importers of the first build use.
OUTCOME_VALUES = tuple(outcome_values())


def content_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


class MemoryService:
    """Write, search and read long-term memory for one store."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------------- write

    def write(
        self,
        *,
        namespace: str,
        memory_class: str,
        title: str,
        body: str,
        source_refs: Sequence[str] | None = None,
        locators: Sequence[Mapping[str, Any]] | None = None,
        meta: Mapping[str, Any] | None = None,
        outcome: str | None = None,
        origin: str | None = None,
        import_ref: str | None = None,
        actor: str = "knowledge",
        library: bool = False,
    ) -> str:
        """Store one document and index it on both lexical channels. Returns the ``doc_id``.

        Writing the same content again returns the document that is already there: by
        ``import_ref`` when one is given (an import whose content changed supersedes the
        old document), otherwise by namespace, class, title and content sha256. The shared
        library is written only with ``library=True``, which an ingest passes and a tool
        handler never does.
        """
        contracts.require_label("memory_class", memory_class)
        realm, _project, _rest = parse_namespace(namespace)
        if not library:
            scope_rules.writable(namespace)
        if not isinstance(body, str) or not body.strip():
            raise SchemaInvalid(
                "a memory document needs a body; an empty record indexes nothing and retrieves as a false positive",
                details={"namespace": namespace, "title": title},
            )
        if outcome is not None and outcome not in outcome_values():
            raise SchemaInvalid(
                f"unknown outcome {outcome!r}; allowed: {outcome_values()}",
                details={"outcome": outcome, "allowed": outcome_values()},
            )
        meta_out = dict(meta or {})
        declared_synthetic = meta_out.get("synthetic")
        if realm == "synthetic":
            if declared_synthetic is False:
                raise SchemaInvalid(
                    "meta.synthetic=false contradicts the synthetic namespace realm", details={"namespace": namespace}
                )
            meta_out["synthetic"] = True
        elif declared_synthetic:
            raise ScopeDenied(
                "a record marked synthetic may not be written into the 'real' realm; test data must live in a "
                "'synthetic:' namespace so a real project scope can never retrieve it",
                details={"namespace": namespace},
            )
        refs = [str(ref) for ref in (source_refs or [])]
        locator_list = [dict(locator) for locator in (locators or [])]
        # A credential is never stored (D4.7): each match of the credentials screen is replaced, and the
        # unit says how many spans were, never what they were. Email addresses and paths stay; the
        # outbound screen keeps them from leaving. A paper's own bytes are held as published elsewhere.
        from ..research import screening  # local: gx_core.knowledge stays importable without the research package

        body, body_redacted = screening.redact(body)
        title_text, title_redacted = screening.redact(title or "")
        redacted = {key: body_redacted.get(key, 0) + title_redacted.get(key, 0) for key in {*body_redacted, *title_redacted}}
        if redacted:
            meta_out["redacted"] = {"spans": sum(redacted.values()), "screens": sorted(redacted)}
        digest = content_sha256(body)
        # A document that rests on a withdrawn source does not enter retrieval, whoever writes it:
        # an import run again after a retraction would otherwise bring the retracted paper back.
        for ref in refs:
            match = _DEPENDENCY_REF.match(ref)
            if match and match.group(1) == "source":
                source = self.conn.execute("SELECT status FROM sources WHERE source_id=?", (match.group(2),)).fetchone()
                if source is not None and source["status"] in ("RETRACTED", "SUPERSEDED"):
                    raise GroundsWithdrawn(
                        f"source {match.group(2)} is {source['status']}; nothing that rests on it is written to memory",
                        details={"source_id": match.group(2), "status": source["status"], "namespace": namespace},
                    )

        with store.transaction(self.conn) as conn:
            superseded: sqlite3.Row | None = None
            if import_ref:
                existing = conn.execute(
                    "SELECT doc_id, content_sha256, rowid FROM memory_docs WHERE import_ref=? AND namespace=?"
                    " AND retracted_at IS NULL AND superseded_by IS NULL",
                    (import_ref, namespace),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] == digest:
                        return existing["doc_id"]
                    superseded = existing
            else:
                existing = conn.execute(
                    "SELECT doc_id FROM memory_docs WHERE namespace=? AND memory_class=? AND content_sha256=?"
                    " AND ifnull(title,'')=? AND retracted_at IS NULL AND superseded_by IS NULL",
                    (namespace, memory_class, digest, title_text),
                ).fetchone()
                if existing is not None:
                    return existing["doc_id"]

            doc_id = ids.new_id("doc")
            cursor = conn.execute(
                "INSERT INTO memory_docs(doc_id, namespace, memory_class, title, body, content_sha256,"
                " source_refs_json, locators_json, meta_json, outcome, origin, import_ref, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    doc_id, namespace, memory_class, title_text, body, digest, ids.canonical_json(refs),
                    ids.canonical_json(locator_list), ids.canonical_json(meta_out), outcome, origin, import_ref,
                    clock.now_iso(),
                ),
            )
            rowid = cursor.lastrowid
            conn.execute(
                "INSERT INTO memory_fts(rowid, doc_id, namespace, title, body) VALUES(?,?,?,?,?)",
                (rowid, doc_id, namespace, title_text, body),
            )
            conn.execute(
                "INSERT INTO memory_fts_cjk(rowid, doc_id, namespace, title, body) VALUES(?,?,?,?,?)",
                (rowid, doc_id, namespace, tokenize.bigram_text(title_text), tokenize.bigram_text(body)),
            )
            for ref in refs:
                match = _DEPENDENCY_REF.match(ref)
                if match:
                    conn.execute(
                        "INSERT OR IGNORE INTO dependencies(subject_kind, subject_id, depends_on_kind,"
                        " depends_on_id, relation, created_at) VALUES('memory_doc',?,?,?,?,?)",
                        (doc_id, match.group(1), match.group(2), "cites", clock.now_iso()),
                    )
            if superseded is not None:
                conn.execute("UPDATE memory_docs SET superseded_by=? WHERE doc_id=?", (doc_id, superseded["doc_id"]))
                self._unindex(conn, superseded["doc_id"], superseded["rowid"])
                store.append_event(
                    conn, kind="memory.superseded", actor=actor, subject_kind="memory_doc",
                    subject_id=superseded["doc_id"], reason=f"import {import_ref} changed content",
                    evidence_level="EXECUTION_OBSERVED", payload={"by_doc_id": doc_id, "namespace": namespace},
                )
        return doc_id

    # ------------------------------------------------------------------------ search

    def search(
        self,
        query: str,
        project_scope: Any,
        filters: Mapping[str, Any] | None = None,
        limit: int | None = None,
        *,
        strategy: Any | None = None,
        record: bool = True,
        run_id: str | None = None,
        principal: str | None = None,
    ) -> dict[str, Any]:
        """Rank the documents at or under ``project_scope`` for ``query``.

        ``project_scope`` is read by :func:`gx_core.knowledge.scopes.from_argument`: one
        namespace, several separated by commas, or a list; a bare project reference means
        that project's own namespace, and the shared library is searched only when it is
        named (``scopes.for_run`` gives a run its own namespace and the library).
        ``strategy`` runs a config (or a built-in by name) by content instead of the active
        one; an improvement cycle passes it, with ``record=False``, to compare an incumbent
        and a candidate.

        ``score`` is a reciprocal-rank fusion score: higher is better, it orders this
        answer only, and it is neither a probability nor comparable across queries or
        strategies. A hit is a candidate to read, not a verified answer.
        """
        scope_list = scope_rules.from_argument(project_scope)
        limit = DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
        filters = dict(filters or {})
        unknown = set(filters) - ALLOWED_FILTER_KEYS
        if unknown:
            raise SchemaInvalid(
                f"unknown search filter(s) {sorted(unknown)}; allowed: {sorted(ALLOWED_FILTER_KEYS)}. "
                "An ignored filter silently widens the query.",
                details={"unknown": sorted(unknown)},
            )
        wanted_namespaces = _as_list(filters.get("namespace"))
        for namespace in wanted_namespaces:
            if not any(namespace_in_scope(namespace, scope) for scope in scope_list):
                raise ScopeDenied(
                    f"namespace {namespace!r} is outside the scope {scope_list}",
                    details={"namespace": namespace, "project_scope": scope_list},
                )
        effective_scopes = wanted_namespaces or scope_list
        classes = self._validated(filters.get("memory_class"), "memory_class")
        outcomes = self._validated(filters.get("outcome"), "memory_outcome")

        chosen = strategy_rules.resolve(self.conn, strategy)
        config = chosen["config"]
        where_sql, where_params = self._doc_filter(effective_scopes, classes, outcomes)
        searched: list[str] = []
        if effective_scopes:
            scope_sql, scope_params = scope_rules.sql_scope_clause("d.namespace", effective_scopes)
            searched = [
                row["namespace"] for row in self.conn.execute(
                    f"SELECT DISTINCT d.namespace AS namespace FROM memory_docs d WHERE {scope_sql} ORDER BY 1",
                    scope_params,
                )
            ]

        response: dict[str, Any] = {
            "query": query,
            "project_scope": scope_list if len(scope_list) != 1 else scope_list[0],
            "results": [],
            "retrieval_mode": strategy_rules.retrieval_mode([c["channel"] for c in config["channels"]]),
            "channels": [],
            "strategy_version": chosen["version"],
            "strategy_sha256": chosen["sha256"],
            "strategy_source": chosen["source"],
            "strategy_asset_sha256": chosen["asset_sha256"],
            "searched_namespaces": searched,
            "filters": {"memory_class": classes, "outcome": outcomes, "namespace": wanted_namespaces},
            "note": (
                "Lexical retrieval over a local index. Open candidate documents with gx_memory_read "
                "and check their source references before relying on a hit."
            ),
        }

        reason = None
        if not scope_list:
            reason = "no scope was named; a search reaches only the namespaces it is given, and there is no default"
        elif not searched:
            reason = f"no memory namespaces exist under {effective_scopes}"
        elif not tokenize.word_terms(query) and not tokenize.bigram_tokens(query):
            reason = "the query contains no indexable term; no search was run"
        channel_reports: dict[str, dict[str, str]] = {}
        ranked: dict[str, list[str]] = {}
        terms_for_snippet: list[str] = []
        if reason is None:
            for channel in config["channels"]:
                name = channel["channel"]
                if channel.get("when") == strategy_rules.WHEN_CJK and not tokenize.has_cjk(query):
                    # Routed: this strategy consults the channel only for a query with CJK characters.
                    channel_reports[name] = {
                        "status": "skipped",
                        "reason": "this strategy consults the channel only for a query with CJK characters; this query has none",
                    }
                    continue
                terms = tokenize.word_terms(query) if name == strategy_rules.CHANNEL_WORD else tokenize.bigram_terms(query)
                terms_for_snippet.extend(terms)
                expression = tokenize.match_expression(terms)
                if channel["weight"] <= 0:
                    channel_reports[name] = {"status": "skipped", "reason": "weight 0 in the strategy"}
                    continue
                if expression is None:
                    channel_reports[name] = {"status": "empty", "reason": "the query has no term this channel indexes"}
                    continue
                hits = self._channel(name, expression, config, where_sql, where_params)
                ranked[name] = hits
                channel_reports[name] = (
                    {"status": "ran", "reason": f"{len(hits)} candidate(s)"} if hits
                    else {"status": "empty", "reason": "no document matched"}
                )

        fused = self._fuse(ranked, config)
        routed_sources: list[str] = []
        if reason is None and config["identifier_routing"] == "promote":
            routed_sources = self._routed_sources(query, effective_scopes)
            if routed_sources:
                fused = self._promote(fused, routed_sources, config, where_sql, where_params)
                channel_reports[strategy_rules.CHANNEL_ROUTING] = {
                    "status": "ran", "reason": f"{len(routed_sources)} source(s) named in the query were promoted",
                }
            else:
                channel_reports[strategy_rules.CHANNEL_ROUTING] = {
                    "status": "empty", "reason": "no identifier or known name in the query",
                }
        else:
            channel_reports.setdefault(strategy_rules.CHANNEL_ROUTING, {
                "status": "skipped",
                "reason": "not in this strategy" if config["identifier_routing"] != "promote" else "no search was run",
            })
        channel_reports[strategy_rules.CHANNEL_EMBEDDING] = {
            "status": "skipped", "reason": strategy_rules.EMBEDDING_NOT_BUILT,
        }
        for name in contracts.label_values("retrieval_channel"):
            report = channel_reports.get(name, {"status": "skipped", "reason": "not in this strategy"})
            response["channels"].append({"channel": name, "status": report["status"], "reason": report["reason"]})
        ran = [name for name, report in channel_reports.items() if report["status"] == "ran"]
        if ran:
            response["retrieval_mode"] = strategy_rules.retrieval_mode(ran)

        if reason is not None:
            response["reason"] = reason
        else:
            response["results"] = self._results(fused, ranked, config, limit, list(dict.fromkeys(terms_for_snippet)))
            if not response["results"]:
                response["reason"] = "no document under the scope matched the query"
        if record:
            response["query_id"] = self._record(response, scope_list, run_id=run_id, principal=principal)
        return response

    # ---------------------------------------------------------------- search, privately

    def _doc_filter(self, scopes: Sequence[str], classes: Sequence[str], outcomes: Sequence[str]) -> tuple[str, list[Any]]:
        clause, params = scope_rules.sql_scope_clause("d.namespace", scopes)
        where = ["d.retracted_at IS NULL", "d.superseded_by IS NULL", clause]
        out: list[Any] = list(params)
        if classes:
            where.append(f"d.memory_class IN ({', '.join('?' for _ in classes)})")
            out.extend(classes)
        if outcomes:
            # NULL never matches IN: filtering by outcome leaves out documents written without one.
            where.append(f"d.outcome IN ({', '.join('?' for _ in outcomes)})")
            out.extend(outcomes)
        return " AND ".join(where), out

    def _channel(self, name: str, expression: str, config: Mapping[str, Any], where_sql: str, where_params: Sequence[Any]) -> list[str]:
        table = _TABLE[name]
        # The weight is a validated finite float, formatted here because bm25() takes literals.
        title_weight = repr(float(config["title_weight"]))
        sql = (
            f"SELECT d.doc_id AS doc_id FROM {table} JOIN memory_docs d ON d.doc_id = {table}.doc_id"
            f" WHERE {table} MATCH ? AND {where_sql}"
            f" ORDER BY bm25({table}, 0.0, 0.0, {title_weight}, 1.0), d.doc_id LIMIT ?"
        )
        rows = self.conn.execute(sql, (expression, *where_params, int(config["candidates_per_channel"])))
        return [row["doc_id"] for row in rows]

    @staticmethod
    def _fuse(ranked: Mapping[str, list[str]], config: Mapping[str, Any]) -> list[tuple[str, float]]:
        weights = {channel["channel"]: channel["weight"] for channel in config["channels"]}
        k = config["rrf_k"]
        scores: dict[str, float] = {}
        for name, doc_ids in ranked.items():
            weight = weights.get(name, 0.0)
            for rank, doc_id in enumerate(doc_ids, start=1):
                scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

    def _routed_sources(self, query: str, scopes: Sequence[str]) -> list[str]:
        """Sources the query names: by a strong identifier, or by a registered name."""
        clause, params = scope_rules.sql_scope_clause("namespace", scopes)
        found: list[str] = []
        for kind, value in tokenize.strong_identifiers(query):
            for row in self.conn.execute(
                f"SELECT source_id FROM source_identifiers WHERE id_kind=? AND id_value=? AND {clause}",
                (kind, value, *params),
            ):
                found.append(row["source_id"])
        lowered = (query or "").lower()
        by_name: dict[str, list[str]] = {}
        for row in self.conn.execute(
            f"SELECT id_value, source_id FROM source_identifiers WHERE id_kind IN ('paper_id','work_id','tag')"
            f" AND {clause}",
            params,
        ):
            value = row["id_value"]
            if len(value) >= 4 and value.lower() in lowered:
                by_name.setdefault(value, []).append(row["source_id"])
        for name in tokenize.names_in_query(query, list(by_name)):
            found.extend(by_name[name])
        return list(dict.fromkeys(found))

    def _promote(
        self, fused: list[tuple[str, float]], sources: Sequence[str], config: Mapping[str, Any],
        where_sql: str, where_params: Sequence[Any],
    ) -> list[tuple[str, float]]:
        """Documents of the named sources first, in their fused order; the rest follow.

        A named source's documents that no lexical channel returned are added after the
        ones that were, so a query that is nothing but an identifier still finds them.
        """
        marks = ", ".join("?" for _ in sources)
        routed_docs = [
            row["doc_id"] for row in self.conn.execute(
                "SELECT d.doc_id AS doc_id FROM dependencies x JOIN memory_docs d ON d.doc_id = x.subject_id"
                f" WHERE x.subject_kind='memory_doc' AND x.depends_on_kind='source' AND x.depends_on_id IN ({marks})"
                f" AND {where_sql} ORDER BY d.rowid LIMIT ?",
                (*sources, *where_params, int(config["candidates_per_channel"])),
            )
        ]
        routed = set(routed_docs)
        if not routed:
            return fused
        first = [(doc_id, score) for doc_id, score in fused if doc_id in routed]
        seen = {doc_id for doc_id, _ in first}
        extra = [(doc_id, 0.0) for doc_id in routed_docs if doc_id not in seen]
        rest = [(doc_id, score) for doc_id, score in fused if doc_id not in routed]
        return first + extra + rest

    def _results(
        self, fused: list[tuple[str, float]], ranked: Mapping[str, list[str]], config: Mapping[str, Any],
        limit: int, terms: list[str],
    ) -> list[dict[str, Any]]:
        cap = int(config["max_per_source"])
        ranks = {name: {doc_id: rank for rank, doc_id in enumerate(doc_ids, start=1)} for name, doc_ids in ranked.items()}
        results: list[dict[str, Any]] = []
        per_source: dict[str, int] = {}
        position = 0
        while len(results) < limit and position < len(fused):
            window = fused[position:position + max(limit * 2, 50)]
            position += len(window)
            marks = ", ".join("?" for _ in window)
            rows = {
                row["doc_id"]: row for row in self.conn.execute(
                    "SELECT doc_id, namespace, memory_class, outcome, title, body, content_sha256, source_refs_json, locators_json"
                    f" FROM memory_docs WHERE doc_id IN ({marks})",
                    [doc_id for doc_id, _ in window],
                )
            }
            for doc_id, score in window:
                row = rows.get(doc_id)
                if row is None:
                    continue
                refs = store.loads(row, "source_refs_json", [])
                if cap:
                    key = next((ref for ref in refs if ref.startswith("source:")), None)
                    if key is not None:
                        if per_source.get(key, 0) >= cap:
                            continue
                        per_source[key] = per_source.get(key, 0) + 1
                # A snippet is cut from the stored body, so the body is checked against the sha256
                # taken when it was written before any of it is shown. One changed byte stops the
                # answer and names the document: a store that was altered is not searched around.
                if row["content_sha256"] and content_sha256(row["body"]) != row["content_sha256"]:
                    raise ArtifactHashMismatch(
                        f"memory document {doc_id} no longer hashes to the sha256 it was written with; nothing is "
                        "served from it. Retract it, or restore the store, and search again",
                        details={"doc_id": doc_id, "namespace": row["namespace"], "recorded_sha256": row["content_sha256"]},
                    )
                locators = store.loads(row, "locators_json", [])
                results.append({
                    "doc_id": doc_id,
                    "namespace": row["namespace"],
                    "memory_class": row["memory_class"],
                    "outcome": row["outcome"],
                    "title": row["title"],
                    "score": round(float(score), 6),
                    "snippet": tokenize.snippet(row["body"], terms),
                    "source_refs": refs,
                    "locators": locators[:RESULT_LOCATORS],
                    "locators_total": len(locators),
                    "channels": [
                        {"channel": name, "rank": table[doc_id]} for name, table in ranks.items() if doc_id in table
                    ],
                })
                if len(results) >= limit:
                    break
        return results

    def _record(self, response: Mapping[str, Any], scope_list: Sequence[str], *, run_id: str | None, principal: str | None) -> str | None:
        """Keep the search. A store that cannot be written to says so instead of failing the read."""
        query_id = ids.new_id("query")
        top = [
            {"doc_id": r["doc_id"], "namespace": r["namespace"], "memory_class": r["memory_class"],
             "score": r["score"], "source_refs": r["source_refs"]}
            for r in response["results"][:10]
        ]
        try:
            self.conn.execute(
                "INSERT INTO memory_queries(query_id, run_id, principal, project_scope, query, filters_json,"
                " retrieval_mode, channels_json, strategy_version, searched_namespaces_json, top_json, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    query_id, run_id, principal, ",".join(scope_list), response["query"] or "",
                    ids.canonical_json(response["filters"]), response["retrieval_mode"],
                    ids.canonical_json(response["channels"]), response["strategy_version"],
                    ids.canonical_json(response["searched_namespaces"]), ids.canonical_json(top), clock.now_iso(),
                ),
            )
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise
            return None
        return query_id

    @staticmethod
    def _validated(value: Any, axis: str) -> list[str]:
        items = _as_list(value)
        for item in items:
            if item not in contracts.label_values(axis):
                raise SchemaInvalid(
                    f"{item!r} is not a valid {axis}; allowed: {contracts.label_values(axis)}",
                    details={axis: item},
                )
        return items

    # -------------------------------------------------------------------------- read

    def read(self, doc_id: str, *, offset: int | None = None, limit: int | None = None, principal: Any = None) -> dict[str, Any]:
        """The body of a document, re-hashed against the sha256 recorded when it was written.

        ``offset`` and ``limit`` are in characters. Whatever is asked for, one slice stays
        within :data:`READ_BYTES` so that it fits a response; ``truncated`` and
        ``next_offset`` say whether and where more follows.
        """
        row = self._require_doc(doc_id)
        scope_rules.check_principal(principal, [row["namespace"]])
        if row["retracted_at"] is not None or row["superseded_by"] is not None:
            raise GroundsWithdrawn(
                f"memory document {doc_id} was " + ("retracted" if row["retracted_at"] else f"superseded by {row['superseded_by']}"),
                details={"doc_id": doc_id, "retracted_at": row["retracted_at"], "superseded_by": row["superseded_by"]},
            )
        withdrawn = [
            f"{dep['depends_on_id']} ({dep['status']})" for dep in self.conn.execute(
                "SELECT x.depends_on_id AS depends_on_id, s.status AS status FROM dependencies x"
                " JOIN sources s ON s.source_id = x.depends_on_id"
                " WHERE x.subject_kind='memory_doc' AND x.subject_id=? AND x.depends_on_kind='source'"
                " AND s.status IN ('RETRACTED','SUPERSEDED')",
                (doc_id,),
            )
        ]
        if withdrawn:
            raise GroundsWithdrawn(
                f"memory document {doc_id} rests on a withdrawn source: {', '.join(withdrawn)}",
                details={"doc_id": doc_id, "withdrawn_sources": withdrawn},
            )
        body = row["body"]
        recorded = row["content_sha256"]
        observed = content_sha256(body)
        if recorded and observed != recorded:
            raise ArtifactHashMismatch(
                f"memory document {doc_id} hashes to {observed[:16]}... but was written as {recorded[:16]}...; "
                "the stored body is not what was ingested",
                details={"doc_id": doc_id, "recorded_sha256": recorded, "observed_sha256": observed},
            )
        start = max(0, int(offset or 0))
        if start > len(body):
            raise SchemaInvalid(
                f"offset {start} is beyond the document's {len(body)} characters",
                details={"doc_id": doc_id, "offset": start, "length": len(body)},
            )
        wanted = len(body) - start if limit is None else max(0, int(limit))
        piece = body[start:start + wanted]
        while len(piece.encode("utf-8")) > READ_BYTES:
            piece = piece[: max(1, len(piece) * READ_BYTES // len(piece.encode("utf-8")) - 1)]
        end = start + len(piece)
        evidence_ids = [
            dep["depends_on_id"] for dep in self.conn.execute(
                "SELECT depends_on_id FROM dependencies WHERE subject_kind='memory_doc' AND subject_id=?"
                " AND depends_on_kind='evidence' ORDER BY depends_on_id",
                (doc_id,),
            )
        ]
        return {
            "doc_id": doc_id,
            "namespace": row["namespace"],
            "memory_class": row["memory_class"],
            "outcome": row["outcome"],
            "title": row["title"],
            "body": piece,
            "offset": start,
            "next_offset": end if end < len(body) else None,
            "total_chars": len(body),
            "truncated": end < len(body),
            "content_sha256": recorded or observed,
            "hash_verified": bool(recorded),
            "source_refs": store.loads(row, "source_refs_json", []),
            "locators": store.loads(row, "locators_json", []),
            "evidence_ids": evidence_ids,
            "meta": store.loads(row, "meta_json", {}),
            "created_at": row["created_at"],
        }

    def get(self, doc_id: str) -> dict[str, Any]:
        """The whole record, including one that was withdrawn. For audit, not for use."""
        row = self._require_doc(doc_id)
        return {
            "doc_id": row["doc_id"],
            "namespace": row["namespace"],
            "kind": row["memory_class"],
            "memory_class": row["memory_class"],
            "title": row["title"],
            "body": row["body"],
            "content_sha256": row["content_sha256"],
            "source_refs": store.loads(row, "source_refs_json", []),
            "locators": store.loads(row, "locators_json", []),
            "meta": store.loads(row, "meta_json", {}),
            "outcome": row["outcome"],
            "origin": row["origin"],
            "import_ref": row["import_ref"],
            "created_at": row["created_at"],
            "superseded_by": row["superseded_by"],
            "retracted_at": row["retracted_at"],
        }

    # ------------------------------------------------------------- retract / supersede

    def retract(self, doc_id: str, *, reason: str, actor: str = "knowledge") -> None:
        """Withdraw a document: it leaves both indexes and never ranks again.

        The row stays. A retraction is part of the record, and a deleted row cannot explain
        why a past decision cited it.
        """
        if not reason or not reason.strip():
            raise SchemaInvalid(
                "a retraction needs a reason; an unexplained withdrawal cannot be reviewed", details={"doc_id": doc_id}
            )
        row = self._require_doc(doc_id)
        if row["retracted_at"] is not None:
            return
        with store.transaction(self.conn) as conn:
            conn.execute("UPDATE memory_docs SET retracted_at=? WHERE doc_id=?", (clock.now_iso(), doc_id))
            self._unindex(conn, doc_id, row["rowid"])
            store.append_event(
                conn, kind="memory.retracted", actor=actor, subject_kind="memory_doc", subject_id=doc_id,
                reason=reason, evidence_level="EXECUTION_OBSERVED",
                payload={"namespace": row["namespace"], "memory_class": row["memory_class"]},
            )

    def supersede(self, doc_id: str, by_doc_id: str, *, actor: str = "knowledge") -> None:
        """Mark ``doc_id`` as replaced by ``by_doc_id``; the old document stops ranking."""
        if doc_id == by_doc_id:
            raise SchemaInvalid("a document cannot supersede itself", details={"doc_id": doc_id})
        row = self._require_doc(doc_id)
        replacement = self._require_doc(by_doc_id)
        if replacement["retracted_at"] is not None:
            raise SchemaInvalid(
                "the replacement document is retracted; superseding by a withdrawn record would remove both "
                "from retrieval with no successor",
                details={"doc_id": doc_id, "by_doc_id": by_doc_id},
            )
        with store.transaction(self.conn) as conn:
            conn.execute("UPDATE memory_docs SET superseded_by=? WHERE doc_id=?", (by_doc_id, doc_id))
            self._unindex(conn, doc_id, row["rowid"])
            store.append_event(
                conn, kind="memory.superseded", actor=actor, subject_kind="memory_doc", subject_id=doc_id,
                reason=f"superseded by {by_doc_id}", evidence_level="EXECUTION_OBSERVED",
                payload={"namespace": row["namespace"], "by_doc_id": by_doc_id},
            )

    # ------------------------------------------------------------------------ private

    def namespaces(self) -> list[str]:
        return [r["namespace"] for r in self.conn.execute("SELECT DISTINCT namespace FROM memory_docs ORDER BY namespace")]

    @staticmethod
    def _unindex(conn: sqlite3.Connection, doc_id: str, rowid: int | None) -> None:
        for table in _TABLE.values():
            removed = 0
            if rowid is not None:
                removed = conn.execute(f"DELETE FROM {table} WHERE rowid=? AND doc_id=?", (rowid, doc_id)).rowcount
            if not removed:
                conn.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,))

    def _require_doc(self, doc_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT rowid, * FROM memory_docs WHERE doc_id=?", (doc_id,)).fetchone()
        if row is None:
            raise ArtifactNotFound(f"no such memory document {doc_id!r}", details={"doc_id": doc_id})
        return row


def summarise_for_log(response: Mapping[str, Any], *, top: int = 3) -> dict[str, Any]:
    """The audit view of one search: query, ranking, namespaces and top hits, without bodies."""
    return {
        "query": response.get("query"),
        "project_scope": response.get("project_scope"),
        "retrieval_mode": response.get("retrieval_mode"),
        "strategy_version": response.get("strategy_version"),
        "channels": response.get("channels"),
        "searched_namespaces": response.get("searched_namespaces"),
        "result_count": len(response.get("results", [])),
        "top": [
            {"doc_id": r["doc_id"], "namespace": r["namespace"], "memory_class": r["memory_class"],
             "score": r["score"], "source_refs": r["source_refs"]}
            for r in response.get("results", [])[:top]
        ],
    }


# ---------------------------------------------------------------- names the first build used
#
# ``gx_core/knowledge/literature.py`` (v1, removed at this stream's M1) and two callers
# outside this package still import these. They are aliases, not a second implementation.

RETRIEVAL_MODE = "BM25_FTS5"
query_tokens = tokenize.word_terms


def fts_match_expression(query: str, *, mode: str = "any") -> str | None:
    """v1's helper: the word channel's MATCH expression for a query."""
    if mode not in ("any", "all"):
        raise SchemaInvalid(f"unknown match mode {mode!r}", details={"mode": mode})
    terms = tokenize.word_terms(query)
    if not terms:
        return None
    joiner = " OR " if mode == "any" else " AND "
    return joiner.join('"' + term.replace('"', '""') + '"' for term in terms)
