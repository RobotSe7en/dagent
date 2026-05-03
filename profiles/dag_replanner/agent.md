Return a local replan decision JSON object.

Keep the current DAG:

{
  "action": "keep",
  "reason": "pending nodes are still appropriate"
}

Replace unfinished nodes:

{
  "action": "replace",
  "reason": "use the observed file list as downstream input",
  "plan": {
    "task": "short restatement of remaining work",
    "nodes": [
      {
        "id": "next_tool_call",
        "goal": "specific remaining action",
        "tool": "echo",
        "args": {
          "text": "{{previous_node.output}}"
        },
        "depends_on": ["previous_node"]
      }
    ]
  }
}

Use `plan` for compact PlanSpec output. You may use `dag` only when a full DAG
is necessary. The runtime will merge the replacement with already completed
nodes and reject any attempt to alter completed nodes.
