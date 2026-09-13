# Chain-skill A/B harness

Compares two ways of working through the same code-review findings:

- **Arm A (baseline)** — `copilot/jnaio-auto-rate-limit`, one session, prompted to read the
  findings and fix them.
- **Arm B (skill)** — `copilot/jnaio-auto-rate-limit2`, the same branch plus
  `.claude/skills/cloud-task-chain/`, prompted to use the skill on the findings.

Both branches carry a byte-identical `docs/code-review-jnaio-auto-rate-limit.md`
(blob `b763141`) with 7 findings across 6 files.

## Record the pre-run SHAs first

Scoring is a diff against the branch state *before* the run. Capture it now — once a run has
pushed, the original tip is harder to name.

```
A_BASE=bdc9fe36a3626b6b9639b9b1da2ba42379eb4d76
B_BASE=2bf37d72454cd4fccbb8de9d969a789a09247969
```

## Score a run

```bash
python eval/score_findings.py \
  --findings docs/code-review-jnaio-auto-rate-limit.md \
  --base "$A_BASE" --head origin/copilot/jnaio-auto-rate-limit \
  --label baseline
```

`--json` emits the same data for scripting. `--window` (default 40 lines) sets how far from a
cited line a hunk still counts as "near".

Both numbers it reports are proxies, deliberately. A fix can be right and land nowhere near
the cited line; a file can be touched without the finding being addressed; and a finding can
be *correctly declined*, which is not a miss. Use the table to decide which diffs to read,
not to declare a winner.

## Tests

The suite for the addon under review lives in `japanese_note_ai_ops/test/`, with its own
`pytest.ini` (see the comment in that file — rootdir placement is deliberate):

```bash
cd japanese_note_ai_ops && pytest test
```

Run it on each arm's final state. A run that fixes six findings and breaks the suite has not
beaten one that fixes five and stays green.

## Cost

Arm B's cost is **the whole chain**, not the session you started: the setup session plus
every link it spawned. Sum `external_metadata.usage.cost_usd` from `get_session` over all of
them. Measured floor is ~$0.50 per link before any useful work, so an over-fragmented queue
shows up here.

## What counts as a result

Arm A has a 1M context window and 7 localized findings. It can very plausibly do the whole
job in one session, in which case the skill *should* lose on cost — a true result that says
nothing about the case the skill exists for (long, parallelizable work).

So the useful question here is whether the skill declines to over-engineer:

| | Pass | Fail |
|---|---|---|
| Queue shape | 2–4 tasks | ~7 tasks, one per finding |
| Findings 6 & 7 | same task (same file) | split across links → push race |
| Overhead vs arm A | under ~$2 | several dollars of boot cost |
| Coverage | comparable to arm A, suite green | findings lost between links |

Findings 6 and 7 are both in `match_words_to_notes.py`. The skill's file-ownership rule says
they must land in one task; splitting them is the specific failure mode to watch for.
