"""Use the acceptance resource launcher in tests that exercise real admission."""

import os
from pathlib import Path
import sys


def controlled_resources():
    from gx_core.resource_profile import PROFILE_VAR, SCOPE_VAR, deterministic_test_resources

    if deterministic_test_resources():
        return {name: os.environ[name] for name in (PROFILE_VAR, SCOPE_VAR)}
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from acceptance.closure.profile import controlled_resources as launch
    finally:
        sys.path.pop(0)
    return launch()
