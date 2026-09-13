# JNAIO review fixes — lane `review-fixes`, chained session instructions

You are one link in a chain of cloud Claude Code sessions. You do **exactly one task**,
record what happened, push, launch the next link, and stop.

Your container is destroyed when you finish. **Git is the only thing that carries state to
the next link** — anything you do not commit and push does not exist for your successor.

## The documents

| What | Where |
| --- | --- |
| **Design doc** (the spec — the user owns it, don't edit) | `docs/code-review-jnaio-auto-rate-limit.md` |
| **Notes + task queue** (yours to maintain) | `docs/task-chain/notes-review-fixes.md` |
| Chain log | `docs/task-chain/chain-log.md` |

Paths are relative to your clone's root. The **notes document is the single source of truth
for the task queue**; there is no separate plan file.

The design doc here is a **code review**, and every task in the queue names the finding it
closes. Read that finding's section of the review in full — it gives the exact wrong result,
the path the code takes to it, and a concrete scenario you can turn into a regression test.
Ignore the review's "Checked and clean" and "Noted, not filed as a finding" sections; they are
deliberately not work.

## First: find your spawn tool

The MCP server that creates sessions is exposed under a **different prefix in every
session** — you may see `mcp__Claude_Code_Remote__create_session`, or something opaque like
`mcp__bf7c680d-…__create_session`. Do not assume a name.

Look through your available tools for one whose name ends in `__create_session`, and another
ending in `__archive_session`. If neither is visible, search your deferred tools for
`create_session`. Establish this now, before you start work — if you cannot find it you will
not be able to continue the chain, so go straight to "When you can't continue".

## Steps

1. Confirm you are on `copilot/jnaio-auto-rate-limit2` (`git rev-parse --abbrev-ref HEAD`). If not,
   `git checkout copilot/jnaio-auto-rate-limit2`. Everything you do lands on this branch.
2. Read the notes document in full. It is kept short on purpose — read all of it.
3. Read the section of the **design doc** that covers your task. Read large reference files
   only if your task needs them, and only the relevant part.
4. Take the **first unchecked `- [ ]` task under `## Task queue`**. That is your task.
   - If it is marked **`[USER]`**, do not attempt it. Go to "Blocked" below.
5. Do exactly that one task. Check `git log --oneline -10` if you need to know what the
   previous link actually landed — the notes can lag reality by a commit.
6. Run the repo's own checks before committing. A fresh clone cannot even *collect* the
   tests until you do the one-time container setup:

   ```
   git submodule update --init --recursive
   python -m pip install -q pytest requests json-repair rapidfuzz psutil
   python build.py link
   ```

   Then `python -m pytest japanese_note_ai_ops/test -q`, plus
   `python -m pytest anki_shared/test -q` if your task touches `anki_shared/` or `build.py`.
   **Compare against the known-good baseline in the notes' repo section.** Some failures there
   are environmental and are not yours to fix; only a *new* failure is. A link that pushes
   broken code costs the next link its whole budget diagnosing it, so this is worth the time.
7. Commit. One commit per logical change. End the commit message with whatever attribution lines your own harness guidance specifies.
8. Update the notes document:
   - Tick your task `- [x]` and append a one-line result to it.
   - Add a short entry under `## Session log` — commit hashes and what changed, in the
     compressed style already there. Do not write an essay; every future link pays for it.
   - Record any **decision** the user made, or that you made and they should know about,
     under `## Decisions made`.
   - **Append any new tasks you discovered** at the right spot in the queue. This is
     expected — the queue is not fixed. If your task turned out to be bigger than one
     session, split it and leave the rest queued. Size what you add the way the existing
     entries are sized: a coherent deliverable, not a single edit.
9. Append one line to `docs/task-chain/chain-log.md`:
   `review-fixes task-N finished <ISO timestamp> — <one-line result>`
10. **Push**: `git push -u origin copilot/jnaio-auto-rate-limit2`. Check that it actually succeeded rather
    than assuming — this is the step the whole chain depends on. Retry network failures up
    to four times (2s, 4s, 8s, 16s). If the push is *rejected* rather than failing on the
    network, something else has written to your branch: go to "When you can't continue".
11. Archive your predecessor, if you were given one, with the tool ending in
    `__archive_session`. Do this only now — after your own push has landed — so that a chain
    which breaks leaves its evidence alive.
12. Count the remaining `- [ ]` entries under `## Task queue`.
    - **If one or more remain**, spawn the next link with the tool ending in
      `__create_session`, using the parameters below. Confirm you got a session id back.
    - **If none remain**, append `LANE COMPLETE` to `chain-log.md`, commit, push, and spawn
      nothing.
13. Say in one line what you did and whether you spawned a successor. Stop.

## The spawn call

Get your own session id first (the tool ending in `__get_session`, called with no session id,
describes you). Then:

```
title:           "JNAIO review fixes · review-fixes · task-<N+1>"
tags:            ["chain:review-fixes", "config:auto-create-pr:off"]
source_url:      "https://github.com/jhhr/anki_addons"
source_revision: "copilot/jnaio-auto-rate-limit2"
outcome_branch:  "copilot/jnaio-auto-rate-limit2"
prompt:          "Read docs/task-chain/task-prompt.md and follow it exactly. You are
                  task-<N+1> in lane review-fixes. Your predecessor is <your own session
                  id> — archive it once your own push has succeeded."
```

`<N+1>` is your own task number plus one.

**Never spawn before your push has landed.** Your successor clones from GitHub at the moment
it is created, so a successor spawned early starts from stale code and will redo or undo
your work.

## Blocked tasks

Some tasks need the user to do something you cannot. They are marked `[USER]` in the queue.

When the first unchecked task is `[USER]`:

1. Do **not** tick it and do **not** spawn a successor — a successor would hit the same wall
   and burn a link for nothing.
2. Append to `chain-log.md`: `CHAIN PAUSED — waiting on user: <what they need to do>`.
   Commit and push it *before* you ask, so the pause survives even if your container is
   reclaimed while waiting.
3. Use **`AskUserQuestion`** to ask for exactly what you need. It reaches the user in
   claude.ai/code and on their phone, and your session stays alive for the answer — so if
   they reply you can simply carry on from step 5. Record the answer under
   `## Decisions made`.
4. If you are still blocked after that, stop. Do not spawn.

`PushNotification` does not reach the user from a cloud session — don't rely on it.

## When you can't continue

If your task fails, your push is rejected, or you cannot find the spawn tool, do the same
thing: write what broke into the notes document and `chain-log.md`, push if you can, ask the
user with `AskUserQuestion`, and **do not spawn a successor**. A stalled chain the user can
see beats a chain that spawns links which fail one after another.

## Rules

- **One task only.** After step 13 you are finished. Do not pick up the next task even if it
  looks trivial — that is the whole point of the chain.
- **Push before spawn.** Every time.
- **Ask when it matters.** If the task is ambiguous or you hit a real design fork, use
  `AskUserQuestion`. A question costs far less than a wrong implementation that the next
  three links build on.
- **Stay in your lane.** Other lanes are working other files on other branches at the same
  time. Touching a file your lane doesn't own causes a push race and stalls someone.
- **Keep the notes lean.** They are re-read in full by every future link.
- **Read `## Notes for working in this repo`** in the notes document before you run anything.
  It has the container setup, the baseline test results, and the formatting convention.
- **Every fix ships with a regression test.** The review states a concrete failing scenario
  for each finding; reproduce that scenario in a test, watch it fail, then fix it.
- **Do not open a pull request.** All work lands on the work branch; the user opens PRs.
