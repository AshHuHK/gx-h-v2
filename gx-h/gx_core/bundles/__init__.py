"""Bundles: what a worker returns for a task, and how the backend judges one.

``schema`` reads ``contracts/bundles.json`` and checks shape record by record;
``framework.judge`` runs that check and then the validator the owning service published.
"""

from .framework import bounded_view, full_view, judge, record_view
from .schema import SchemaReport, Violation, check_bundle, list_fields, primary_list

__all__ = [
    "SchemaReport", "Violation", "bounded_view", "check_bundle", "full_view", "judge",
    "list_fields", "primary_list", "record_view",
]
