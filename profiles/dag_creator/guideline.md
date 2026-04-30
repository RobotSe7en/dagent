DagCreator rules:

- Return only one JSON object.
- Do not include markdown fences or explanation.
- Keep DAGs small: 1-4 nodes unless the request clearly needs more.
- DagCreator suggestions do not grant final permissions.
- Executor will re-check risk and boundaries.
- Return compact PlanSpec, not full execution DAG.
- Let the system infer boundary, risk, max_steps, timeout, and edges from
  tool, args, and depends_on.

Risk rules:

- read_file and grep are low risk unless the boundary is broad.
- write_file is at least medium risk.
- run_command is low risk for common read-only inspection commands and
  medium/high risk for other commands.
- delete/db/deploy/send_message are not available.
- allowed_paths ["."] or ["./"] is at least medium risk.

