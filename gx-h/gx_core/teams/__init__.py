"""The task loop: one task, one brief, one bundle, judged record by record.

``service`` is the task service (create, plan, claim, bind, submit, hold), ``recovery``
settles what time and approvals have decided, ``tools`` publishes the four task tools,
and ``briefs`` / ``brief_sections`` (host stream) compile what a worker is told.
"""

from .service import TeamService

__all__ = ["TeamService"]
