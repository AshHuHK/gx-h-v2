"""Continue an existing project using its pinned workflow's replanning behavior."""
from ..orchestration.service import hooks_for_run


def prepare_replan(conn, run_id):
    return hooks_for_run(conn, run_id).replan(conn, run_id)
