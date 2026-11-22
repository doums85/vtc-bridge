## Skill `caveman` — mandatory invocation

Invoke `caveman` immediately at the start of every turn, before any other tool.

- First tool call of the turn = `Skill` with `skill: caveman`. No file read, search, Bash, or exploration before it.
- Then apply caveman rules to every response in the turn (terse replies, technical substance intact).
- Only the user's "stop caveman" or "normal mode" disables the style.
- Code, commits, PR text, docs and issues stay in normal prose.
