---
name: "skips-local-file-task"
tags: ["trigger", "regression"]
plugins: ["../.."]
runs: 2
max_turns: 6
timeout_seconds: 240
---
Read the file ./notes.txt in this directory and summarize its contents.
