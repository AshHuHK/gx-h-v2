"""One budget line, read one way (SCHEDULING_DESIGN r2.2a §4.6, §15 item 17; the lead's ruling of 2026-11-13T00:55:01Z).

What is left of a line is ``limit - spent - reserved``, rounded as the ledger rounds it. A line
with nothing left is **exhausted**; a zero line is exhausted too. A line whose limit is zero or
less is **prohibited**: the run may not use that resource at all, and a budget increase is not
how it is had. A prohibited line that was spent on all the same is **broken**: the ledger shows
a use its limits never allowed.

Every reader of a budget line calls :func:`line` (the scheduler's snapshot, selection's
permission filter, job admission, the engine, status), so no reader keeps a copy of the
formula; the ledger's own ``available`` is computed with it. This module is a leaf: it imports
nothing from the store, the config or the rest of the package, so the scheduler's pure modules
may import it.
"""

from __future__ import annotations

import math
from numbers import Real
from typing import Any

#: The digits the ledger keeps: a line reads 3, not 3.0000000000000004, and 0.3 - 0.1 - 0.2 is 0.
DIGITS = 9


def clean(value: float) -> float | int:
    """Round as the ledger rounds, and keep integers integral."""
    rounded = round(value, DIGITS)
    return int(rounded) if float(rounded).is_integer() else rounded


def _figure(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise TypeError(f"a budget line's {name} is a finite number, not {value!r}")
    return float(value)


def line(limit: float, spent: float = 0, reserved: float = 0) -> dict[str, Any]:
    """``{"available", "exhausted", "prohibited", "broken"}`` for one line. Pure.

    * ``available`` = limit - spent - reserved, rounded to :data:`DIGITS` (an int when whole);
    * ``exhausted`` = available <= 0;
    * ``prohibited`` = limit <= 0;
    * ``broken`` = prohibited and spent > 0.

    A figure that is not a finite number (a bool, a string, NaN, an infinity) is a TypeError: a
    line nobody can read is not a line with room on it.
    """
    limit_, spent_, reserved_ = _figure("limit", limit), _figure("spent", spent), _figure("reserved", reserved)
    available = clean(limit_ - spent_ - reserved_)
    prohibited = limit_ <= 0
    return {"available": available, "exhausted": available <= 0, "prohibited": prohibited,
            "broken": prohibited and spent_ > 0}


__all__ = ["DIGITS", "clean", "line"]
