# Oversized current tool fixture

`oversized_current_tool.json` is a sanitized copy of request 26 from the deterministic synthetic CSV audit used to diagnose the one-token clamp in 0.3.17 / 0.3.18. Its system prompt is replaced with a generic instruction and host paths are replaced with `/workspace/fixture`; CSV records are synthetic. It is regression input, not new model output.

`tests.test_current_tool_emergency` runs the real Manager, archive and recall code without inference. Its explicitly calibrated counter reproduces the recorded initial prompt estimate (32854) and framed current tool size (24074); it does not reproduce the original tokenizer. Sanitizing the path changes the placeholder length, so the post-mask estimate is 8885 rather than the historical replay's 8906. Neither is a tokenizer-exact claim. The actual preserved question, archive content and recall result are asserted independently of those counts.

The historical diagnostic replay (maintained with task t_633392d7 rather than copied into this repository) also forces a cut before the current question on the old implementation: the question is retained but the response limit remains 1. Thus a cut-only change is not an accepted fix. These tests do not assert that a model will choose recall correctly or produce the right answer.
