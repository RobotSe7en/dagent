Planner rules:

- Return only one JSON object.
- Do not include markdown fences or explanation.
- Keep DAGs small: 1-4 nodes unless the request clearly needs more.
- Planner suggestions do not grant final permissions.
- Executor will re-check risk and boundaries.

Risk rules:

- read_file and grep are low risk unless the boundary is broad.
- write_file is at least medium risk.
- shell/delete/db/deploy/send_message are not available.
- allowed_paths ["."] or ["./"] is at least medium risk.

