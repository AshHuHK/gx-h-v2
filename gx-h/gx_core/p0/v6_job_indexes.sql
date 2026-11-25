-- Branch admission reads original settlement amounts while it holds the Job writer.
-- Look up just the reservations of the branch and the execution identity being admitted.
CREATE INDEX v6_budget_settled_ref ON events(json_extract(payload_json,'$.reservation_ref'))
    WHERE kind='budget.settled' AND subject_kind='budget';
CREATE INDEX v6_branch_work ON v6_nodes(branch_id,status);
CREATE INDEX v6_run_execution ON v6_nodes(run_id,exec_key,status);
