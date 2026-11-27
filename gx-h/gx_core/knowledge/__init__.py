"""Project-scoped memory, lexical retrieval and document provenance."""

from . import scopes, strategy, tokenize
from .memory import (
    OUTCOME_VALUES,
    RETRIEVAL_MODE,
    MemoryService,
    content_sha256,
    fts_match_expression,
    query_tokens,
    summarise_for_log,
)
from .scopes import LIBRARY_SCOPE, REALMS, is_synthetic, namespace_in_scope, parse_namespace

__all__ = [
    "LIBRARY_SCOPE",
    "MemoryService",
    "OUTCOME_VALUES",
    "REALMS",
    "RETRIEVAL_MODE",
    "content_sha256",
    "fts_match_expression",
    "is_synthetic",
    "namespace_in_scope",
    "parse_namespace",
    "query_tokens",
    "scopes",
    "strategy",
    "summarise_for_log",
    "tokenize",
]
