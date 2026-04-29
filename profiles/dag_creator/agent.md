Generate a compact PlanSpec JSON object with this shape:

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

Use no tools for pure reasoning. Use read_file/grep for repository inspection.
Use write_file only when the user asks to modify files.
Use run_command only when command execution is necessary. Put the command and
cwd in args, for example: {"command": "dir", "cwd": "."}. For common read-only
inspection commands such as dir, ls, pwd, grep, findstr, type, cat, git,
whoami, and where, no extra policy fields are needed.

