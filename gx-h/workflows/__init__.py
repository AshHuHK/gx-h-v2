"""Versioned workflow definitions.

One directory per workflow, one JSON file per version. A definition is never edited in
place once a run has pinned it: ``contracts/states.json`` → ``asset.rules.versioning``
says a modified workflow is a new version, and the registry refuses anything else.
"""
