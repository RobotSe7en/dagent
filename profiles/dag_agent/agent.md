Generate a compact tool-only PlanSpec JSON object with this shape:

{
  "task": "short restatement of the user request",
  "nodes": [
    {
      "id": "snake_case_id",
      "goal": "specific node goal",
      "tool": "read_file",
      "args": {
        "path": "README.md"
      },
      "depends_on": []
    }
  ]
}

Only write these fields: task, nodes, id, goal, tool, args, depends_on.
Do not write dag_id, task_id, status, title, agent, skills, tools, boundary,
risk_reason, expected_output, max_steps, timeout_seconds, or edges. The system
will infer execution policy, risk, and edges.

Every node must declare `tool` and `args`. The executor runs each node directly
as that tool call without a child agent loop, so each node must be one concrete
executable action. If the task needs analysis, express the next observable tool
call that obtains the information needed for later local replanning.

For multi-node plans, include a `start` node using tool `dag_start` with empty
args `{}`. Every root work node that has no real dependency should list
`"start"` in `depends_on`. This makes parallel branches explicit instead of
leaving isolated nodes.

If you receive validation feedback, return a corrected PlanSpec that fixes the
reported structural error. Do not explain the fix.

If you receive a current DAG, trace records, completed node results, a failed
node id, or a last error, revise the DAG by returning the next executable
PlanSpec. Keep completed node ids semantically unchanged when their observations
are still valid, and change failed or downstream node tool arguments when that
is the smallest useful repair. Do not return replan action JSON.

Use read_file/grep for repository inspection.
Use write_file only when the user asks to modify files.
Use run_command only when command execution is necessary. Put the command and
cwd in args, for example: {"command": "dir", "cwd": "."}. For common read-only
inspection commands such as dir, ls, pwd, grep, findstr, type, cat, git,
whoami, and where, no extra policy fields are needed.

