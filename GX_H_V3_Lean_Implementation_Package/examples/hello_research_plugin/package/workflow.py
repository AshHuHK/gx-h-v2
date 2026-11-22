class HelloResearchWorkflow:
    def __init__(self, ctx):
        self.ctx = ctx

    def validate_intent(self, intent_items):
        return {"accepted": True, "warnings": []}

    def build_task_context(self, task):
        return {"task_id": task["task_id"], "source": "hello_research"}
