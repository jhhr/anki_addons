# Kanjify golden set: running it in a cloud session

You are the lead of one share of the kanjify golden set's work (the scripts are described in
`japanese_note_ai_ops/word_array/research/kanjify_golden.py`). A cloud session has no `claude`
CLI for `agent_queue.py` to start, so you run each item as one of your own subagents: take items
with `agent_items.py`, hand each to the subagent type it names, save each answer. The user tells
you which queue and which shard (`I/N`) are yours; never take another shard's items, since other
sessions work on them at the same time. Nothing here touches an Anki collection.

All commands run from the repo root. `$R` is `japanese_note_ai_ops/word_array/research`, `$G` is
`test_data/japanese_note_ai_ops/evals/kanjify_golden`.

## Setup, once per session

1. The session-start hook has made the venv (`python -c "import sudachipy"` works). If not,
   say so and stop.
2. Clone the private test data repo to `test_data/` at the repo root, if it isn't there:
   `git clone https://github.com/jhhr/anki_addons_test_data.git test_data`. If this session
   cannot reach it, stop and ask the user to add it to the environment.
3. Dictionaries: `cd japanese_note_ai_ops && python word_array/research/setup_resources.py;
   cd ..`. If JMdict's download is refused, copy it from the test data and run it again:
   `mkdir -p japanese_note_ai_ops/user_files/jmdict && cp
   test_data/japanese_note_ai_ops/resources/JMdict_e.gz japanese_note_ai_ops/user_files/jmdict/`.
4. Build the lookup's sense index before any subagent needs it (a minute; several subagents
   building it at once would race): `python $R/kanjify_lookup.py jmdict test`.
5. `python $R/agent_items.py status words pilot_batch batches --shard I/N` shows what is left.

## The loop

Keep K subagents running (6 is a good start, 10 for a session that is the only one; fewer if
the session slows). Your own context gets one line per item, so the loop can run for hours:

1. `python $R/agent_items.py take QUEUE --shard I/N --count K` prints one line per item: its
   id, the subagent type, the prompt file and the answer file. It claims them, so they are
   never handed out twice. Leave `--shard` out when yours is the only session (it is 0/1).
2. For each line start a background subagent of that type (`kanjify-word`,
   `kanjify-batch-high`, ...; they are in `.claude/agents/` and fix model, effort and tools)
   with the prompt: `Read <prompt file> and do what it says.` Do not paste the prompt itself:
   it is long, and the file is what it reads.
3. A subagent writes its answer to its answer file and replies `written <id>`: then run
   `python $R/agent_items.py save QUEUE <id>`. It checks the answer and writes the result
   file. If a subagent replied with the JSON itself instead, write that reply, unchanged, to a
   file and pass it: `save QUEUE <id> <file>`. If `save` says to ask again, send the subagent
   the reason; if that fails too, leave the item: its claim expires after 90 minutes and a
   later `take` hands it out again. Rows `save` warns will be rejected are kept; collate sorts
   them out.
4. Take one more item for each one finished.
5. Every 10 saves, and before you stop, push the results (only your result files: never
   `collated/`, `decisions.jsonl`, a queue you rendered, or anything under `output/`):

       cd test_data
       git add japanese_note_ai_ops/evals/kanjify_golden/results/QUEUE/*.json
       git commit -m "japanese_note_ai_ops: kanjify golden set, QUEUE shard I/N results"
       git pull --rebase && git push
       cd ..

   Result files are one per item, so another session's pushes never conflict with yours.

When the usage limit stops you, stop: everything saved is pushed or will be by the next
session; a claim expires on its own. Report to the user how many items you saved, and any
subagent that failed twice.

## What to run, in order

1. `words`, step 1: one xhigh word decision per item. Its queue is committed in the test data
   (rendered from the current policy by the user's own session): use it as it is, never render
   it again here.
2. The pilot, once every one of its words is decided (`python $R/kanjify_golden_render.py
   pilot` says which are missing, and writes `pilot_batch` only when none is). Only one
   session does this: run collate (`python $R/kanjify_golden_collate.py`), render the pilot,
   run the `pilot_batch` items (shard 0/1: there are four), collate again, and give the user
   the "Pilot" part of `$G/collated/report.md` with the cost and time per item of each way.
   The user then picks step 2's batch size and effort.
3. `batches`, step 2, once the user has chosen. For a batch size other than the inventory's 20,
   first `python $R/kanjify_golden_inventory.py --batch-size S` (the same on every session: it
   reads only the committed dump). Then `python $R/kanjify_golden_collate.py`, `python
   $R/kanjify_golden_render.py batches --effort E`, and the loop with `batches`.
   Render again (after a collate) when many new word decisions have come in: an item not yet
   taken gets its new prompt, a saved one keeps its result.

Collate writes files other sessions write too (`decisions.jsonl`, `collated/`): run it to render
and to read the report, but never commit what it writes. The user or the local session commits
those once all sessions have stopped.
