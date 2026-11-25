"""Native transport and status for the released org.gx.science-grid v3 workflow."""
from ..orchestration.native_worker import NativeWorker
from . import grid_brief, grid_planner, terminal_products


class GridWorker(NativeWorker):
    role_tools = True
    schema_page_kind = "grid_product_schema"

    def check_start(self, spec):
        terminal_products.repair_limit(spec)

    def snapshot(self, conn, run_id):
        from workflows.science_grid_v3.settlement import current_evidence
        fields = ("path_id", "job_id", "eval_id", "evidence_id", "assessment_ref", "direction")
        state = {"current_evidence": [{key: row[key] for key in fields}
                                      for row in current_evidence(conn, run_id, verify_bytes=False)]}
        if turns := terminal_products.pending(conn, run_id):
            state["pending_terminal_turns"] = [dict(row) for row in turns]
        return state

    def assign(self, run, packet):
        grid_planner.bind(packet)
        return grid_brief.render if grid_brief.applies(run, packet["input"]) else None

    assigned = staticmethod(grid_planner.assigned)

    def validate_submission(self, binding, product):
        descriptor = self.assigned(binding)
        if descriptor:
            grid_planner.validate_grids(product, descriptor)
        return descriptor

    def guidance(self, brief):
        return grid_brief.GUIDANCE if grid_brief.includes_artifacts(brief) else brief

    def resume_text(self, brief):
        if grid_brief.includes_artifacts(brief):
            return ("Resume your assigned task. Use gx_task_context for current Intent and supplemental "
                    "sources, then continue with the frozen Artifacts in your original brief.")
        return brief


WORKER = GridWorker()
