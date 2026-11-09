# Start here (every session, every AI)

This repo is one part of the owner's system. Before changing anything, read
**`C:\Projects\AlwaysOnAgent\docs\AI_HANDOFF.md` in full** (on GitHub: example-org/AlwaysOnAgent,
`docs/AI_HANDOFF.md`). It covers how the parts fit, how to deploy (this repo reaches the Mini through
`AlwaysOnAgent/scripts/mini_pull_tools.py`; DeskAI is built on the laptop only), the rules, and what not to undo.
Then read this repo's README.

In short: verify before you claim; run the tests and read their exit code; edit with a file-editing tool; tests
never call real services; nothing public or on the owner's accounts without their tap; secrets never go to a
model, a log or git; free tiers only; ask before anything hard to undo; update the docs and comment the *why*.
