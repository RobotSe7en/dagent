Return a local replan decision JSON object.

Keep the current DAG:

{
  "action": "keep",
  "reason": "pending nodes are still appropriate"
}

Patch and retry one failed or pending node:

{
  "action": "patch_node",
  "reason": "the command argument used the wrong path",
  "node_id": "failed_node_id",
  "tool": "run_command",
  "args": {
    "command": "dir",
    "cwd": "."
  }
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

Abort when the task cannot continue safely:

{
  "action": "abort",
  "reason": "the requested operation cannot be represented by available tools"
}

Use `plan` for compact PlanSpec output. You may use `dag` only when a full DAG
is necessary. The runtime will merge the replacement with already completed
nodes and reject any attempt to alter completed nodes.
