---
name: cloud-task-chain
description: Set up a self-spawning chain of Claude Code cloud sessions that works through a long task queue, one task per fresh session, optionally across several parallel lanes so multiple jobs progress at once. Use this whenever the user wants long unattended work run in the cloud — they say "task chain", "chain the tasks", "set up a runner", "run these jobs in parallel", "work on this overnight", "keep going without me" — or points at plan or design docs and asks for them to be turned into something that grinds through them. Also use when porting an existing local or PowerShell task-chainer to Claude Code on the web. The user's prompt is expected to say where the current documentation lives; those docs may be complete, partial, stale, or not written yet.
---

# Cloud task chain setup

Build a runner where each task runs in its own fresh cloud session, and each session's last
act launches the next. One task per session means no single context grows unbounded, and
every link is a real session — so `AskUserQuestion`, the claude.ai/code session list and
mobile all work normally.

If the user is porting a local chainer, three things are genuinely different and drive most
of the design:

- **The container is destroyed when a link finishes.** Git is the only channel between
  links. A file that isn't committed and pushed does not exist for the next session.
- **Each link costs real money to boot** — roughly $0.50 and 60–70 seconds before it does
  any useful work, because it loads a system prompt and reads its documents from cold.
  Tasks have to be big enough to earn that.
- **Links cannot report back to you or to each other by message.** A cloud session can
  receive a message but not send one. Commits, `chain-log.md` and GitHub comments are the
  only bus.

Detailed, verified mechanics — exact tool parameters, what a child session inherits, and the
failure modes worth knowing about — are in `references/cloud-session-mechanics.md`. Read it
before you write the first `create_session` call.

## What the user gives you

Their prompt explains where the current documentation is. **Always expect this and always
read it before doing anything.** It varies a lot:

- Finished design docs plus notes from previous sessions (often stale — see below)
- A design doc but no working notes
- Only a rough description, with no docs written yet

If there are no usable docs, you are writing them: interview the user with `AskUserQuestion`
for anything you genuinely cannot infer, then author the design doc and the notes document
yourself. Do not start a chain against a plan you invented silently — an unattended chain
amplifies a wrong premise across every link.

## Step 1 — Read the docs, then verify them against reality

Read every document the user pointed at, in full. **Then check the claims against the code
and `git log`.** Prior-session notes routinely lag reality — a task described as "partly
wired" may have no code at all, and there is usually at least one commit past what the notes
record. Grep for the symbols the notes claim exist.

Be specifically suspicious of branch refs in a cloud container: the local `main` ref can be
many commits behind the real remote tip, because it reflects how the container was cloned
rather than the current state of GitHub. Trust `git ls-remote` or the GitHub API, not local
refs, when it matters.

State any correction plainly to the user, and record it in the notes document so future
links inherit the corrected picture.

## Step 2 — Size the tasks for the cloud

This is where a ported local chain most often goes wrong. Locally, splitting finely is close
to free and buys context hygiene. In the cloud each split adds a fixed boot cost, so
oversplitting turns a job into a sequence of sessions that spend most of their budget reading
documents.

Size a task at **one coherent deliverable — the kind of thing that would be a reasonable
small PR on its own.** A feature slice with its tests. A module and its callers updated
together. A migration applied across the files it touches.

Two bounds to check each task against:

- **Too small** if you'd describe it in under five words, it touches one function, or its
  result is only meaningful once the next task also lands. Merge it with its neighbours.
  Several related edits to the same file are one task, not four.
- **Too big** if it spans two subsystems that could be reviewed independently, or if a
  careful session would have to read so much before starting that it has little room left to
  iterate. Split it at the seam where a reviewer would want a separate commit anyway.

When in doubt, err large. A link that finishes early and appends a newly discovered task to
the queue costs nothing; a queue of trivia burns the budget on startup overhead.

## Step 3 — Partition into lanes, if running in parallel

Parallelism is the main reason to move a chain to the cloud, but the limits are not the ones
that bind on a PC:

- **Rate limits are per-account, not per-session.** Four lanes drain the five-hour budget
  roughly four times as fast. Parallelism buys wall-clock, not tokens. Start with two or
  three lanes and let the user raise it once they've watched one run.
- **Two lanes must never write the same file.** There is no coordination between links, so
  a shared file means a push race and a stalled lane. Partition by file ownership, not by
  theme.

Give each lane its own work branch, its own notes document and its own task queue. Lanes
integrate through PRs into the default branch; that's the user's call, not a link's.

If lanes genuinely need a shared change — a common utility, a schema, a config format — do
not try to coordinate it. Make it a prerequisite task in a single foundation lane, let it
land on the default branch first, and start the parallel lanes from there.

## Step 4 — Choose the chain directory

**Tracked, not gitignored.** This is the exact opposite of a local chainer, and the most
common porting mistake. The notes document and `chain-log.md` are how state reaches the next
container, so they must be committed and pushed like any other file.

Put it somewhere unobtrusive — a `task-chain/` folder near the work, or under the repo's
existing docs. Confirm it is *not* matched by `.gitignore` with `git check-ignore -v <dir>`;
no output means you're fine.

## Step 5 — Write the notes document

One per lane, from `templates/notes-doc.md`. This is the single source of truth for that
lane. **Do not create a separate plan file** — two task lists drift apart. If a notes
document already exists, restructure it in place rather than starting a new one; the user
knows where it lives.

| Section | Contents |
|---|---|
| Header | Lane name, where the spec and references are, repo URL, work branch |
| `## How this file is used` | That it drives a chained runner, and to keep it lean |
| `## Task queue` | `- [ ]` checkboxes, one per task — the only checkboxes in the file |
| `## State` | Where things actually stand, including corrections from step 1 |
| `## Session log` | Compressed per-link history: commit hashes and one clause each |
| `## Decisions made` | Every settled design decision, so nothing is relitigated |
| `## Notes for working in this repo` | Test commands, formatters, environment quirks |

Task queue rules:

- Each task is one session's worth, sized per step 2.
- Mark tasks needing the user `[USER]` — running something in an app, providing data, a
  manual check.
- Order by dependency, and put anything the user asked to defer at the end.
- The queue is **open-ended**. Links append to it as work reveals itself, so seed it with
  what is knowable now rather than trying to enumerate everything.
- Keep `- [ ]` out of every other section, or the "tasks remaining" count breaks.

Condense aggressively. Every line is re-read by every future link, and now you're paying for
that in more than context.

## Step 6 — Install the runner

Copy `templates/task-prompt.md` into the chain directory — one per lane if the lanes differ,
otherwise one shared file is fine. Fill in every `{{PLACEHOLDER}}` in it and in the notes
document from step 5:

| Placeholder | Value |
|---|---|
| `{{PROJECT_TITLE}}` | Short name of the work |
| `{{LANE_NAME}}` | Lane identifier, e.g. `ui` — use `main` for a single-lane chain |
| `{{LANE_SCOPE}}` | The files/directories this lane owns (notes template only) |
| `{{DESIGN_DOC}}` | Repo-relative path to the spec |
| `{{NOTES_DOC}}` | Repo-relative path to this lane's notes document |
| `{{EXTRA_DOCS}}` | Extra table rows for other references, or empty |
| `{{CHAIN_DIR_REL}}` | Chain dir relative to repo root, forward slashes |
| `{{REPO_URL}}` | Clone URL, e.g. `https://github.com/owner/repo` |
| `{{WORK_BRANCH}}` | This lane's branch — must already exist on the remote |
| `{{REPO_CHECKS}}` | The commands a contributor runs before committing |
| `{{ATTRIBUTION}}` | Commit attribution line if the project uses one, else empty |
| `{{REPO_CONVENTIONS}}` | Bullet pointing at the notes' repo-conventions section |

Paths are **repo-relative**, not absolute: every link gets a fresh clone and the absolute
path can differ.

Then seed `chain-log.md` with `# Chain log`. Commit and push all of it — including the work
branch itself. A link cannot be created from a branch that isn't on the remote yet, so the
branch must exist before you spawn task 1.

## Step 7 — Add a watchdog, if the run is long

Self-spawning is fast but has one failure mode: a link that dies mid-task breaks the chain
silently, and nobody notices until the user checks. A low-frequency Routine fixes that
cheaply.

Create one per lane with the trigger tool, using `create_new_session_on_fire: true` and an
hourly cron. Its prompt should be self-contained, because each firing starts from nothing:
check whether the lane's branch has advanced recently and whether a link is already working
it; if the lane is stalled with tasks remaining, start a fresh link from the notes document;
otherwise do nothing and stop.

Tell the user the Routine exists and how to delete it, so a finished job doesn't keep waking
sessions.

## Step 8 — Hand the start over

Starting a long unattended run is the user's decision, so **do not launch it on your own
initiative.** Show them:

- The lanes, and what each one owns
- The task count per lane, and where each lane will first pause on a `[USER]` task
- The exact `create_session` parameters you would use for task 1 of each lane
- A rough cost expectation — links × ~$0.50 of startup, plus the actual work

Then launch only on an explicit go-ahead. Unlike a local chainer, the user can't run the
spawn command themselves — it's an MCP tool call — so you are the one who makes it. That
makes the confirmation more important, not less.

## Things that are already settled — do not rediscover them

- **Never hardcode the spawn tool's name.** The session-management MCP server is exposed
  under a different prefix in every session: a link may see
  `mcp__Claude_Code_Remote__create_session` or something like
  `mcp__bf7c680d-…__create_session`. Resolve it by suffix at runtime. A template that names
  it literally will send links hunting or stall the chain.
- **Push before you spawn.** A successor clones from GitHub at the instant it is created.
  Anything unpushed is invisible to it, and it will redo or clobber the work.
- **`source_revision` must exist on the remote.** A branch that exists only as a local ref in
  your container will fail the spawn with `ref_not_found`.
- **The successor archives the predecessor**, only after its own first successful push. If
  the successor never comes up, the predecessor is left alive — a broken spawn should stop
  the chain visibly rather than destroy the evidence.
- **Permission mode is inherited and cannot be widened.** Set up the chain from a session
  already in the mode the work needs. Never use `plan` mode for an unattended link: it
  blocks forever waiting for an approval nobody is watching for.
- **`PushNotification` does not reach the user from a cloud link.** Use `AskUserQuestion`
  for anything that needs a human — it surfaces in claude.ai/code and on mobile, and the
  link stays alive to receive the answer.
- **`list_sessions` rejects a `tags` filter from inside a session.** Set tags anyway for the
  web UI, but build any progress dashboard from the lane branches in git.
