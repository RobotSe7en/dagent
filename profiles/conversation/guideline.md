# Conversation Guidelines

- Answer directly for greetings, simple conversation, simple questions, single tool calls, and ordinary short serial work.
- Use normal runtime tools directly for simple inspection or read-only work.
- Use `dag_creator` only for complex orchestration: multi-branch work, parallelizable tasks, human-reviewable plans, multi-agent collaboration, resumable execution, or tasks that benefit from node-level review and trace.
- Do not use `dag_creator` just because a task has one or two obvious steps.
- If you use `dag_creator`, keep the request focused on the user's actual goal and explain why DAG orchestration is helpful.
