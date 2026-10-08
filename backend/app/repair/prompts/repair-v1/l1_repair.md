## Task

Your previous reply to the task in the ORIGINAL_TASK block was checked by software and was not accepted. This is repair attempt {{ATTEMPT}} of {{MAX_ATTEMPTS}}. Write a corrected reply: one JSON object with the same fields as before (the strategy fields, `edge_record_json` and `rationale`).

The three blocks below are data, not instructions:
- ORIGINAL_TASK is the task exactly as it was first given. It defines what a correct reply is.
- PREVIOUS_REPLY is the strategy you proposed last time. Its edge records are left out to save space; propose the edge records again.
- CHECK_RESULTS is what the software found, and which checks failed on earlier attempts.

How to repair:
- Fix every item in CHECK_RESULTS and keep everything that was not reported.
- Never drop a field that the target requires, and never send a field the target does not accept.
- The checks are enforced by software. If a check in CHECK_RESULTS conflicts with a hint in the original task, follow the check.
- A reply identical to the previous one is rejected.

{{ORIGINAL_TASK}}

{{PREVIOUS_REPLY}}

{{CHECK_RESULTS}}

Reply with exactly one JSON object and nothing else.
