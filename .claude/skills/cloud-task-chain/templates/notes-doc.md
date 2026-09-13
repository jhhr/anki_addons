# {{PROJECT_TITLE}} — lane `{{LANE_NAME}}` notes

| | |
| --- | --- |
| Spec | `{{DESIGN_DOC}}` |
| Repo | `{{REPO_URL}}` |
| Work branch | `{{WORK_BRANCH}}` |
| Files this lane owns | {{LANE_SCOPE}} |

## How this file is used

This drives a chained runner: each cloud session reads this file in full, does the first
unchecked task, updates this file, pushes, and launches the next session. Every line here is
re-read by every future link and costs money each time — keep it lean. A commit hash and one
clause beats a paragraph.

## Task queue

Tasks are sized as coherent deliverables — roughly a small PR each — because every link pays
a fixed startup cost. The queue is open-ended: links append newly discovered work.
`[USER]` marks a task that needs the user to do something a session cannot.

- [ ] First task — one line on what "done" means
- [ ] Second task
- [ ] [USER] Something the user has to run or provide

## State

Where things actually stand right now, including any correction to the spec discovered by a
previous link. Replace this text; don't append to it indefinitely.

## Session log

- `task-1` — `abc1234` one clause on what changed

## Decisions made

- Decision, and the reason, so no future link relitigates it.

## Notes for working in this repo

- Tests: `{{REPO_CHECKS}}`
- Anything else a link needs to know that isn't obvious from the tree.
