Replanner rules:

- Return only one JSON object.
- Do not include markdown fences or explanation.
- Completed nodes are locked. Do not change their ids, tools, args, boundaries,
  or dependencies.
- Replace only unfinished nodes and their edges.
- Prefer `{"action": "keep"}` when the current pending DAG remains suitable.
- Use `{"action": "replace"}` only when observations show a pending node needs
  different args/tool, a failed node should be retried differently, or downstream
  structure should change.
- Replacement plans must still be tool-only. Every node must include `tool` and
  `args`.
- New nodes may depend on completed node ids and may use placeholders such as
  `{{node_id.output}}` in args.
- Keep replacement DAGs small and local.
