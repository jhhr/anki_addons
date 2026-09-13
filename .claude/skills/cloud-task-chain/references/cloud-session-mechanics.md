# Cloud session mechanics

Verified facts about spawning and managing Claude Code cloud sessions, established by
running a live parent → child → grandchild chain rather than from documentation. Read this
before writing the first `create_session` call.

## Finding the tools

The session-management MCP server is exposed under a **different prefix in every session**.
The parent that sets up a chain may see `mcp__Claude_Code_Remote__create_session`, while its
own children see the same tool as `mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__create_session`.
Searching for the literal name `mcp__Claude_Code_Remote__create_session` from inside a child
returns nothing.

Resolve by suffix — `__create_session`, `__archive_session`, `__get_session`,
`__create_trigger` — never by full name. This is the single most likely thing to break a
ported chain.

## `create_session` parameters that matter for chaining

| Parameter | Notes |
|---|---|
| `prompt` | The link's opening instruction. Write it standalone: the child shares no context with you. |
| `title` | Shows in the session list and on mobile. Include lane and task number so concurrent chains stay distinguishable. |
| `tags` | Free-form. Worth setting for the web UI; see the `list_sessions` limitation below. |
| `source_url` | Clone URL of the repo. |
| `source_revision` | Branch/tag/commit to check out. **Must exist on the remote.** |
| `outcome_branch` | Branch the session pushes to, with no session-derived suffix appended. Set it to the lane's work branch. |
| `environment_id` | Defaults to the caller's environment. Leave it unless the user has several. |
| `model` | Defaults to the caller's model. |
| `permission_mode` | Inherited from the caller; **cannot be more permissive**. Never `plan` for an unattended link — it blocks forever on an approval prompt. |

The result includes the new session's id and sets `parent_session_id` to the caller.

## What a child inherits

Confirmed by direct report from spawned sessions:

- A **fresh clone** of the repo at `source_revision`, clean working tree, no leftovers from
  any other session. A child spawned after its parent pushed saw the parent's commit at
  HEAD.
- The same **environment** — network policy, environment variables, setup scripts.
- `permission_mode` (`auto` in the test), the full `mcp__github__*` toolset, and the `Agent`
  tool, so a link can fan out subagents inside its own task.
- **Not** the parent's filesystem. `~/.claude/uploads/` does not exist in a child, so files
  the user attached to the setup conversation are invisible to every link. Anything a chain
  needs must be committed to the repo.

## Cost and latency

From the smoke test, two links doing essentially no work:

| | link 1 | link 2 |
|---|---|---|
| cost | $0.53 | $0.62 |
| cache-read tokens | 352k | 333k |
| output tokens | 4,083 | 2,835 |

That is the floor: roughly **$0.50 and 60–70 seconds** per link before any real work
happens, spent loading the system prompt and reading documents cold. Handoff from one link
pushing to the next being live was about 60 seconds.

This is why task sizing matters more in the cloud than locally. It also means the notes
document's length is a recurring cash cost, not just a context cost.

## Rate limits

`get_session` returns `external_metadata.rate_limit_info` with `status`, `rateLimitType`
(e.g. `five_hour`), `resetsAt` and `isUsingOverage`. The limit is **per-account**, so
parallel lanes share one budget — N lanes drain it roughly N times faster. A supervisor can
read this before deciding whether to start another lane.

## Communication between sessions

- A cloud session can **receive** a cross-session message but cannot send one. Links cannot
  report to the setup session, to each other, or back to the user this way.
- `PushNotification` has nowhere to go from a cloud link — no terminal, no Remote Control.
- `AskUserQuestion` **does** work: it surfaces in claude.ai/code and on mobile, and the
  asking session stays alive to receive the answer. This is strictly better than a local
  chainer, where a blocked link has to stop and be restarted by hand.
- A parent can read a child's *status* (`get_session` shows `status_bucket`,
  `post_turn_summary`, usage) but not its transcript.

So: commits, `chain-log.md`, and GitHub issue/PR comments are the message bus. Design every
link on the assumption that anything it wants to say must be written into the repo.

## Lifecycle

- `archive_session` transitions a session to read-only and releases its container.
  `unarchive_session` reverses it.
- Containers are reclaimed after a period of inactivity anyway, so archiving is tidiness and
  explicit sequencing rather than a resource necessity.
- Having the **successor** archive the **predecessor** after its own first successful push
  reproduces a local reaper's safety property: if the successor never comes up, the
  predecessor stays alive and the failure is visible.
- `interrupt_session` stops a session mid-turn, for a lane that has gone off-track.

## Known sharp edges

- **`ref_not_found` on spawn.** `source_revision` must name a ref that exists on GitHub. A
  branch that exists only as a local ref inside your container — including the branch a web
  session was started on, which may never have been pushed — will fail.
- **Stale local refs.** A container's local `main` can be many commits behind the real remote
  tip. Verify against `git ls-remote` or the GitHub API before branching from it.
- **`list_sessions` rejects a `tags` filter** from inside a session (`tags filter is not
  currently available` — OAuth callers only). Build progress views from lane branches in git
  instead.
- **Destructive git is blocked under `auto` permission mode.** `git push --force-with-lease`
  and `git checkout -B` over an existing branch are refused by the auto-mode classifier.
  Links should only ever fast-forward and push normally; design the chain so no link ever
  needs to rewrite history.

## Scheduled links

`create_trigger` with `create_new_session_on_fire: true` spawns a fresh session per firing,
which makes a good watchdog for a self-spawning chain: if a link dies mid-task, the next
firing notices the lane is stalled and restarts it. Cron is evaluated in UTC and the minimum
interval is normally hourly, so it complements self-spawning rather than replacing it —
self-spawn for throughput, the Routine for recovery.

Each firing starts from nothing, so its prompt must be completely self-contained.
`delete_trigger` removes it when the job is done.
