Return concise JSON:

{
  "passed": true,
  "summary": "brief overall assessment"
}

OR, when rejecting:

{
  "passed": false,
  "issues": [
    {"message": "specific explanation of what is wrong", "node_id": "optional_node_id"}
  ],
  "summary": "brief overall assessment"
}

Set passed to true if the answer sufficiently addresses the user's request.
When passed is false, you MUST include at least one concrete issue in the issues array —
describe exactly what is missing, inaccurate, or incomplete. Never return passed: false
with an empty issues list.
