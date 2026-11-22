"""A trusted workflow owner's preparation across the two claim transactions."""
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class DispatchPreparation:
    """Apply the result only after admission rechecks the same scientific facts."""

    basis: tuple
    prepare: Callable[[], Any]
    apply: Callable[[Any], bool]
