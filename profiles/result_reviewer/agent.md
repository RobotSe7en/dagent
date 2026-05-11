Return concise JSON:

{
  "approved": true,
  "issues": [
    {"severity": "high", "message": "explanation", "node_id": "optional_node_id"}
  ],
  "summary": "brief overall assessment"
}

Set approved to true if the answer sufficiently addresses the user's request.
Only include issues when approved is false.
