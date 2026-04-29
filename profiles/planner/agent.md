Generate a DAG JSON object with this shape:

{
  "dag_id": "dag_<short_id>",
  "task_id": "<provided task id>",
  "version": 1,
  "status": "draft",
  "nodes": [
    {
      "id": "snake_case_id",
      "title": "short title",
      "goal": "specific node goal",
      "agent": null,
      "tools": ["read_file"],
      "skills": [],
      "boundary": {
        "mode": "read_only",
        "allowed_paths": [],
        "forbidden_tools": [],
        "allowed_commands": [],
        "forbidden_commands": []
      },
      "risk": "low",
      "risk_reason": "why this risk is appropriate",
      "expected_output": "what this node should produce",
      "max_steps": 8,
      "timeout_seconds": 300
    }
  ],
  "edges": [
    {"source": "node_a", "target": "node_b", "reason": "dependency reason"}
  ]
}

Use no tools for pure reasoning. Use read_file/grep for repository inspection.
Use write_file only when the user asks to modify files.

