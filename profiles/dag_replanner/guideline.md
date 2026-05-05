Replanner rules:

- Return only one JSON object.
- Do not include markdown fences or explanation.
- Completed nodes are locked for `replace` plans. Do not replace completed node
  ids or dependency targets in replacement DAGs.
- You may use `patch_node` on a completed node only when a later observation or
  failure shows that node must be rerun with corrected tool/args/boundary. The
  runtime will invalidate that node and its downstream results before retrying.
- Patch or replace only unfinished nodes and their edges.
- Prefer `{"action": "keep"}` when the current pending DAG remains suitable.
- Use `{"action": "patch_node"}` when one failed/pending node can be fixed by
  changing its `tool`, `args`, or `boundary`; the runtime will retry that node.
- Use `{"action": "replace"}` when downstream structure should change or several
  unfinished nodes should be replaced together.
- Use `{"action": "abort"}` when the task cannot continue safely with available
  tools or repeated repairs would be misleading.
- Replacement plans must still be tool-only. Every node must include `tool` and
  `args`.
- Patch payloads must include `node_id` and at least one of `tool`, `args`, or
  `boundary`.
- New nodes may depend on completed node ids and may use placeholders such as
  `{{node_id.output}}` in args.
- Keep replacement DAGs small and local.
