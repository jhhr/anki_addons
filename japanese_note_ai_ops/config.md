# Config

## General

- `log_level`: Default is "ERROR". Possible values from less logging to more: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
- `log_to_console` Default is `true`. If false, logs to files in `user_files/logs/` in the
  addon folder, one per op run, named after the op: `match_words_<time>.log`, and
  `match_words_add_note_phase_<time>.log` for its note adding.
- `multi_op_dialog_shortcut`: Default is `""` (none). A key sequence such as `"Ctrl+Shift+J"`
  for the browser's Edit > "Japanese AI ops..." dialog, which runs several ops in a row on the
  selected notes or on every note of the current search. Read when a browser window opens.
- `capture_calls`: Default is `true`. Records every AI call (prompt, answer, outcome, timing,
  the note and run it was for) in `user_files/capture.sqlite3`, for debugging and for
  building tests and evals from real runs. No API keys.
- `capture_keep_days`: Default is `90`. Runs older than this many days are deleted from that
  file, with their calls, when a profile opens; `0` or less, or `null`, keeps every run. A
  number in quotes (`"30"`) counts as that number; anything else (`true`, text) as `90`, with a
  warning in the log. Both are read when the profile opens: a change takes effect on the next
  profile load or restart.
- `capture_notes`: Default is `false`. With `capture_calls` on, each run also records every note
  it read, what it was about to write, and every note it saved or added as the collection held
  it after, with the words' match decisions, into the same file. That is a copy of much of the
  collection per run, for building replay tests; meant for a copied profile, not everyday runs.
  Read when a run starts.

## models

Needs to be one of the models that supports structured output

### OpenAI

- `gpt-4o` and `gpt-4o-*` models
- `gpt-4` and `gpt-4-*` models
- `gpt-3.5-turbo`

### Google

- `gemini-2.5-pro`
- `gemini-2.5-flash`
- `gemini-2.0-flash` (default, free)

## sentence_field / meaning_field / word_field

### model

Define which model to use for each task

- `word_meaning_model`
- `kanji_story_model`
- `translate_sentence_model`
- `kanjify_sentence_model`
- `extract_words_model`: no op of its own any more - "Extract words" builds the word array
  locally and its one call is the proper noun one. It is the fallback for the two below.
- `word_matching_judge_model` (falls back to `extract_words_model`)
- `proper_nouns_model` (falls back to `extract_words_model`), so also the model "Extract words"
  uses
- `match_words_model`

### temperature

- `kanjify_sentence_temperature`: Default is `0.1`. Passed through to provider temperature controls for kanjification requests only.
  Use a low value to reduce variation in kanji choice while still allowing the model a small amount of flexibility. `0.0` is supported by the major providers here, but is not guaranteed to be fully deterministic and can sometimes be more brittle than a very low non-zero setting.

### rate limits

Nothing to configure. Requests go out as fast as the concurrency limit allows; when a provider
rejects one for exceeding its rate limit, that model is put on a short cooldown and the request
is retried automatically. The wait comes from the provider's own response — Anthropic's
`retry-after` header, OpenAI's `Retry-After`, Gemini's `RetryInfo.retryDelay` — falling back to
exponential backoff when none is given.

Errors that retrying can't fix are not retried: an exhausted OpenAI billing quota
(`insufficient_quota`) or a used-up Gemini per-day quota fails immediately and is logged.

- `max_request_retries`: Default `5`. How many times to retry a request that failed for a
  retryable reason (rate limit, provider overload, timeout, connection error).
- `max_retry_wait_seconds`: Default `120`. If a provider asks to wait longer than this, the
  request is abandoned rather than stalling the whole run. Raise it if you regularly hit long
  token-per-minute cooldowns and would rather wait them out.

### memory use and concurrency

How many notes/words are processed at once is what determines memory use, and it is sized
automatically: available RAM divided by what one task of that op actually costs. A tablet gets a
lower limit than a desktop, and a heavy op gets a lower limit than a light one, with no
configuration. While a run is going, the limit is lowered if free memory gets low and raised
again when it recovers. The progress dialog shows the current value.

The per-task cost is measured, not guessed, while the run is going. What changes as a run
proceeds is how many of the op's tasks are alive at once, and the addon fits Python's traced
allocation total against that count as it moves — so what comes out is what one more live task
adds, and everything allocated before the run started is left out of it. Nothing has to be
paused or emptied for that: the count rises and falls by itself as tasks start and finish, and
the traced total follows it down as well as up. The largest fit in a run wins, since what has to
fit in RAM is the peak. The result is remembered per op in `user_files/memory_estimates.json` and
blended with the previous value, so the first run of an op learns what it costs and later runs
start out sized correctly. Deleting that file just means the ops get measured again from the
default guess.

While nothing is configured there is also a backstop of 256 concurrent tasks, which only exists to
stop a very cheap op on a very empty machine from opening an absurd number of connections at once.
Memory is meant to be what limits concurrency in practice: with a couple of GB of budget, ops
costing more than about 8 MB per task are limited by memory rather than by the backstop.

- `max_concurrent_requests`: Default `0` (automatic). Any value above `0` takes the place of that
  backstop, whether it is below 256 or above it, but does **not** turn off the memory-based
  adjustment — the limit still drops under memory pressure, and what memory allows still caps it
  if that is lower than your value. So a large value raises what a run *may* grow to rather than
  what it will get, and a small one holds a device below what its free RAM would otherwise permit.
  Where free memory cannot be probed at all there is nothing to adapt against, and your value is
  used exactly as given (the default there is a static 8).
- `memory_limit`: Default `0` (disabled). Set a number for MB (for example `900`) or a percent
  string of total RAM (for example `"15%"`). Once the Anki process RSS passes that cap, the addon
  starts backing off concurrency. A value of `0` leaves this hard cap off.

The progress dialog shows the current limit, free memory, and the measured cost per task once
enough of the run has been measured to fit one.

### request_timeout

Default `180`. Seconds to wait for a single API response before giving up on that attempt.
Timeouts are retried, subject to `max_request_retries`.

### pausing and cancelling a run

Nothing to configure. The progress dialog of a bulk run has **Pause** and **Cancel** buttons.
Pause starts no new request, note or phase until you press Resume: requests already sent run to
completion, and one that needs a retry waits for the resume. While paused the dialog says so and
how many tasks are still finishing, and the paused time is left out of the ETA. Cancel does what
Escape does, and works while paused too. If a later Anki version changes its progress dialog the
buttons may be missing; Escape still cancels.

A cancelled run keeps what it finished: edited notes are saved, words a match or judge run had
already answered are kept, and the new notes a match run prepared are added and linked from
their sentences as after a full run, so the meanings already paid for are not lost. Words still
unanswered stay to be matched by the next run.

Adding the new notes can itself take a while, since every added note runs the note-adding hooks
(copy_anywhere's definitions among them). While it adds, Pause is greyed out and Cancel (or
Escape) stops the adding; a note already being added is finished first. Cancel is only live
while notes are being added; a press just before it, while the run saves its edits, cancels the
run, which keeps the notes it prepared. The notes not added are dropped, the words that were
linked to them are left to be matched again by the next run, and a numbering such as "(m1)" or
"(r1)" they put on the word's other notes is taken back. A note that failed to add keeps its
placeholder id in the word lists, which may help debugging, but only until the next match run
over those sentences puts the words back to be matched. The end message says how many new notes
were added, how many were left out by the cancel and how many failed.

Last, after every match run, cancelled or not, the sort field markers of the words the run
touched are tidied: a numbering with a gap ("(m1)", "(m3)") is closed up, and a "(m1)", "(r1)",
"(kun)" or "(on)" left on a note with nothing to tell it apart from is taken off. The numbers
follow the order the notes were created in, so a note you add by hand to a numbered word is
numbered after the others, and a numbering in some other order is put in that one. A note that
failed to add, a duplicate the run dropped, or a new reading whose meaning could not be made
leaves those behind. A note whose sort field holds any other marker, such as "(x1)", is left as
it is.

### terminal- models (claude CLI)

Any `*_model` value starting with `terminal-` runs through the `claude` command line on your Claude
subscription instead of the HTTP API, e.g. `"word_matching_judge_model": "terminal-claude-haiku-4-5"`.
Each request starts one `claude -p` process (thinking off, no tools), so it is far slower than the
API: about 70 requests a minute on a 4-core PC. Put the API model back in the config to switch
back. Temperature settings are ignored for these models. `request_timeout`, `max_request_retries`
and `max_retry_wait_seconds` apply as for the API.

When the subscription's usage limit is hit during a bulk run, the run pauses until the reset time
the CLI states (read as your local time) and then carries on by itself: every request that hit the
limit is retried, without counting against `max_request_retries`, and the notes still queued are
done as usual. If the reset time cannot be read, or lies more than 20 hours ahead, the run retries
after `terminal_usage_limit_retry_minutes` instead, and pauses again if the limit still holds. The
progress dialog shows the pause and when it ends; its "Resume now" button retries at once. You can
cancel while paused, and the end message then says the run was cancelled while paused for the usage
limit. Outside a bulk run (a translation or story written when a field loses focus) the request
only fails. A login that has expired stops the run, leaving the remaining notes as they were: log
in again by running `claude` in a terminal, then rerun.

- `terminal_max_concurrent_requests`: Default `16`. How many `claude` processes run at once. Each
  takes a few hundred MB and a lot of CPU while it starts, on top of the normal concurrency limit.
- `terminal_usage_limit_retry_minutes`: Default `15`. How long a run paused by the usage limit
  waits before retrying when the CLI's message gives no reset time that can be read, or one more
  than 20 hours ahead. At least 1 minute; `0` uses the default.
- `claude_cli_path`: Default `""` (find `claude` on PATH). Path to the claude executable. The npm
  `claude.cmd`/`claude.ps1` shims are skipped for the native `claude.exe` they start.

## config fields per note type name

Add the fields by note type like this. You can set multiple different note types. You can't set
multiple fields per note type though.

```json
{
  "note type name A": {
    "meaning_field": "note A meaning field",
    "word_field": "note A word field",
    "sentence_field": "note A sentence field",
    "insert_deck": "My deck::sub deck 1"
  },
  "note type name B": {
    "meaning_field": "note B meaning field",
    "word_field": "note B word field",
    "sentence_field": "note B sentence field",
    "translation_field": "note B translation field",
    "insert_deck": "My deck::sub deck""
  },
  ...etc
}
```

You need to define

- for cleaning/generating word meanings:
  1. `meaning_field`
  2. `english_meaning_field`
  3. `word_field`
  4. `word_reading_field`
  5. `sentence_field`
  - `mdx_filenames`: array of filenames (with `.mdx` extension) for dictionary files in the addon's `user_files/` folder.
    - The `user_files/` folder is created automatically if it does not exist. Example: `["dict1.mdx", "dict2.mdx"]`
  - `mdx_pick_dictionary`: one of "all", "first", "shortest", "longest"
- for translating sentences
  1. `sentence_field`
  2. `translated_sentence_field`
- for generating kanji stories:
  1. `kanji_field`
  2. `story_field`
- for kanjifying sentences:
  1. `furigana_sentence_field`
  2. `kanjified_sentence_field`
- for extracting words:
  1. `word_extraction_sentence_field`
  2. `word_list_field`
- for matching extracted words:
  1. `word_list_field`
  2. `word_kanjified_field`
  3. `word_normal_field`
  4. `word_reading_field`
  5. `word_sort_field`
  6. `meaning_field`
  7. `english_meaning_field`
  8. `part_of_speech_field`
  9. `new_note_id_field`
  10. `insert_deck` (optional) The deck new notes are added to. If omitted or empty, they go
      into the "Default" deck

### sentence and vocab note types

A configured note type gets a role from the keys its block names:

- **sentence role**: the block names `word_list_field`. Its notes hold a sentence and its word
  array; words are extracted, judged and matched from them.
- **vocab role**: the block names `word_sort_field`. Its notes are the words the arrays link
  to; the match op finds its candidates among them and creates new ones in this type.
- A block with neither (e.g. "Kanji draw") has no role.

The roles can be in one note type or in two.

- **One note type** (the original layout): one block with both roles, naming no other type.
  "Japanese vocab note" holds the word and the sentence it was found in. A note the match op
  creates takes `sentence_field`, `furigana_sentence_field`, `kanjified_sentence_field` and
  `sentence_audio_field` from the note its word was found in, as it always has; a note it
  makes as a copy of another keeps that note's translation and extraction field.
- **Two note types**: a sentence note type holding the sentences and their arrays, and a vocab
  note type holding the words, each block naming the other:
  - `vocab_note_type` (sentence block): the note type the words of its arrays are notes of.
    The match op creates new vocab notes in it.
  - `sentence_note_type` (vocab block): the note type whose arrays link its notes.
  - `example_sentence_id_field` (vocab block, optional): the field a vocab note keeps the id of
    its **example sentence** in, the sentence note its word was found in.
  - `sentence_seen_count_field` (sentence block, optional): a field counting how many times the
    sentence has been seen in review on a vocab note. "Move sentences to sentence notes" sets it
    to the review count of the vocab note the sentence came from; nothing else writes it, and
    it is never copied to vocab notes.
  - `migration_move_tags` and `migration_copy_tags` (sentence block, optional): lists of tag
    names, read only by "Move sentences to sentence notes", for your own tags that describe the
    sentence. A sentence note it makes or joins gets:
    - each `migration_move_tags` tag (about the sentence alone: its audio, the show it was
      mined from) of the selected vocab notes given it as their example, and those notes lose
      it. A vocab note left unselected keeps its tags until a run selects it.
    - each `migration_copy_tags` tag (about the word too: a frequency band) of the vocab notes
      the sentence came from, selected or not, and they keep it. The copies the match op
      made (tagged `new_matched_jp_word`) give none: theirs are about their own word.

    A name matches as a `tag:` search does, in any case and with its `::` children
    (`Made_Up_Show` also matches `Made_Up_Show::ep01`, not `Made_Up_Show_2`), but with no
    wildcards. A tag under both lists moves. The run's report counts, per listed tag, the vocab
    notes it was moved or copied from. A moved tag no longer finds the vocab notes in a search
    (a filtered deck, another addon's search).

    How and when to run "Move sentences to sentence notes" is in "Moving to sentence notes"
    below.

  A vocab note keeps a copy of its example sentence's fields for each sentence key **both**
  blocks name: the sentence note's field is copied into the vocab note's, and its id into
  `example_sentence_id_field`. Of the sentence keys, name only `translated_sentence_field` and
  `sentence_audio_field` on the vocab block to keep just the translation and the audio. Each
  block names its own type's fields, so the two names need not match. Each block's
  `insert_deck` is where new notes of its type go.

```json
{
  "Sentence note": {
    "vocab_note_type": "Japanese vocab note",
    "word_list_field": "Word array",
    "sentence_field": "Sentence",
    "furigana_sentence_field": "Sentence furigana",
    "kanjified_sentence_field": "Sentence kanjified",
    "word_extraction_sentence_field": "Sentence for extraction",
    "translated_sentence_field": "Sentence translation",
    "sentence_audio_field": "Sentence audio",
    "sentence_seen_count_field": "Times seen",
    "migration_move_tags": ["redo-audio", "Made_Up_Show"],
    "migration_copy_tags": ["freq-band-a"],
    "insert_deck": "My deck::sentences"
  },
  "Japanese vocab note": {
    "sentence_note_type": "Sentence note",
    "example_sentence_id_field": "Example sentence id",
    "translated_sentence_field": "Example translation",
    "sentence_audio_field": "Example audio",
    "word_kanjified_field": "Word kanjified",
    "word_normal_field": "Word",
    "word_reading_field": "Word reading",
    "word_sort_field": "Word sort key",
    "meaning_field": "Meaning",
    ...the other keys for matching extracted words, as above
    "insert_deck": "My deck::vocab"
  }
}
```

An op refuses to run on a note type whose layout breaks one of these, with a message naming
both types:

- the two blocks name each other: a sentence block naming a vocab type that names no sentence
  type, or another one, is an error, and so is naming a type that has no block;
- a block names only one of `vocab_note_type` and `sentence_note_type` (a block naming its own
  type is the one-type layout);
- the sentence block names `word_list_field` and no `word_sort_field`, the vocab block
  `word_sort_field` and no `word_list_field`.

**The sentence note type must have no field named like the vocab block's
`word_kanjified_field`, `word_normal_field` or `word_sort_field`.** The match op's word lookup
reads every note type that has a field of one of those names, so sentence notes would become
candidates for the words of their own arrays. A sentence type made by copying the vocab type
has those fields: delete or rename them.

### Moving to sentence notes

From the one-type layout to the two-type one, once. The op "Move sentences to sentence notes"
(AI helper menu, sync ops) makes a sentence note of each sentence the vocab notes hold, with
their word array, and makes it the example sentence of every vocab note that showed it: the
vocab note gets its id and a copy of its translation and audio. It does not clear the vocab
notes' old sentence fields and array (you delete those fields at the end), and it can be run
again: a vocab note that already names an existing sentence note is skipped, and a sentence
already in a sentence note is joined rather than made twice. The field and tag names below
are illustrations, as in the example above.

1. **Back up.** Sync, then File > Create Backup. The run changes nearly every vocab note.
2. **Make the sentence note type.** Tools > Manage Note Types > Add > "Clone: Japanese vocab
   note" is the easy way: the clone has every sentence field under the name the vocab type
   has it, and the run reads the vocab notes' old fields by the names the sentence block
   gives them, so the names must be the same on both types until it has run. On the clone:
   - give it one minimal card (Cards...: a front showing the sentence field, say). Sentence
     notes are never studied, but Anki gives every note a card;
   - delete or rename the fields named like the vocab block's `word_kanjified_field`,
     `word_normal_field` and `word_sort_field` (see the warning above), and delete the other
     word fields it has no use for (meaning, part of speech, ...);
   - add the field for `sentence_seen_count_field` if you want one.
3. **Add the example id field to the vocab type** ("Example sentence id"), and fields for the
   translation and audio copies if they are not to go into the fields that hold them now
   (the vocab block may name the old translation and audio fields: the run copies the
   example's into them). Adding a field is a schema change: Anki warns, and the next sync has
   to upload the whole collection, so sync every other device first.
4. **Configure both blocks** as in the two-type example above. The sentence block names the
   sentence fields and `word_list_field` by the names both types have them now, `insert_deck`
   (a deck that exists; give it options with 0 new cards a day, below), and, if you use them,
   `sentence_seen_count_field`, `migration_move_tags` and `migration_copy_tags`. The vocab
   block names `sentence_note_type`, `example_sentence_id_field`, of the sentence keys only
   `translated_sentence_field` and `sentence_audio_field`, no `word_list_field`, and its word
   and meaning keys as before. Set `capture_notes` to `false` if it is on: a run recording its
   notes would copy most of the collection into the capture store.
5. **Practise on a copy.** Close Anki and copy the profile's `collection.anki2` somewhere else.
   From this addon's folder in the repository (`dev/` is left out of the released add-on),
   with the Python that has `anki` and `aqt` installed (the one the tests run with):

       python dev/sentence_migration_run.py --collection <the copy>

   It runs the op over every note of the vocab type in the copy, with the config Anki has
   saved for the addon (`--note-type "<vocab type>"` when not exactly one block names a
   `sentence_note_type`; `--set key=<json>` replaces one top-level config value for this run
   only). It refuses, saying why, whatever the menu's run would refuse: a broken layout, a
   field or deck the config names that the collection lacks, a tag list that is not a list of
   names. It writes **to the collection it is given**: never point it at a profile's own. At
   the end it prints a summary with the path of the report,
   `user_files/sentence_migration_<timestamp>.txt` in the addon's folder, which every run
   writes before it changes a note:
   - its counts (vocab notes read, sources, the copies the match op made, sentence notes to
     add and existing ones joined, and per listed tag the notes it was moved or copied from:
     a 0 shows a typo);
   - every case that needed a decision, with note ids: vocab notes given no example (no
     sentence text, no sentence linking a copy), arrays that could not be read or combined,
     sentences with two links to different notes on one word (tagged
     `sentence-migration-conflict`), sentences without an array (tagged
     `sentence-needs-extract`), and per vocab note whether its word was linked in its
     example's array ("Linked to its word by the link check", with the rule that found it)
     or why not ("Not linked: ...").

   Fix in the real collection what the report shows wrong, then copy and practise again. To
   look at the result, open the copy in a profile of its own, never synced.
6. **Run it.** In the browser search `"note:Japanese vocab note"`, open Edit > "Japanese AI
   ops...", choose "Move sentences to sentence notes" and "Use all notes from current search"
   (selecting every row of a large collection makes the browser slow), and Run. Or two runs:
   first `"note:Japanese vocab note" -tag:new_matched_jp_word`, which makes every sentence
   note, then the whole type again, which gives the copies the match op made their example
   from the sentence notes the first run made. The end message names the report. A cancelled run keeps
   the sentence notes it added, with their vocab notes; run it again for the rest. One Undo
   takes back the whole run, the suspension included.
7. **Check.** Read the report. In the browser: `tag:sentence-needs-extract` (run "Extract
   words + Judge matchability", then "Match extracted words to notes" on them, and take the
   tag off), `tag:sentence-migration-conflict`, vocab notes with no example
   (`"note:Japanese vocab note" "Example sentence id:"`) and sentence notes not suspended
   (`"note:Sentence note" -is:suspended`, none after the run). Look at some vocab cards.
8. **Update what reads the old fields.** A CopyAnywhere definition that reads the sentence or
   the word array off a vocab note reads them off its example sentence note now: a Query Notes
   search `nid:{{trigger.Example sentence id}}` finds it, and in its array the word is the
   element linked to the vocab note's id. A search on a tag the run moved (a filtered deck, a
   CopyAnywhere or related_card_disperse search) finds sentence notes now, not vocab notes.
9. **Delete the old fields** from the vocab type once you are satisfied: every field the
   sentence block names that the vocab block does not, the old word array among them. Take
   them out of the vocab card templates first. Another schema change: another one-way sync.
   With them gone the move cannot run again; no op of this addon reads them.

After the move:

- New sentences are notes of the sentence type. **An import (File > Import, e.g. a .tsv of
  mined sentences) runs no add hooks**: the notes arrive with nothing extracted and their
  cards not suspended. Find them in the browser (`"note:Sentence note" added:1`) and run the
  ops on them there: "Extract words + Judge matchability", then "Match extracted words to
  notes" (Edit > "Japanese AI ops..." runs them as one chain), then the CopyAnywhere
  definitions that fill their other fields. A vocab note the match op makes is of the vocab
  type, its example the sentence note its word was found in. An op run over notes of both
  types leaves out the notes of the type whose role is not its own, saying so once per note
  type ("Run all ops for new notes" gives each note its own role's steps).
- A sentence note added one at a time instead: in Anki's classic Add dialog its words are
  extracted and its cards suspended; through AnkiConnect its words are extracted but its
  cards are not suspended; the experimental new Add dialog does neither. An added vocab note
  gets nothing.
- **Suspension.** A sentence note's cards are suspended only by "Move sentences to sentence
  notes" and by the classic Add dialog, never by an import. Import into a sentence deck whose
  options give 0 new cards a day, which keeps sentence cards out of study whatever adds them,
  or suspend the imported notes' cards in the browser (Cards > Toggle Suspend).
- **"Refresh example sentences"** (sync ops, on vocab notes) copies each note's example again:
  run it after changing a sentence note's translation or audio by hand or by its editor's
  translation (the menu's "Translate sentence" updates the vocab notes itself), or after
  deleting sentence notes. A vocab note whose example is gone gets the oldest sentence note
  whose array links it; with none, its id field is emptied and it is tagged
  `example-sentence-missing`.
- Leaving an empty translation field translates on a sentence note, no longer on a vocab note.

## test data exports

Tools > "AI ops: generate test data" runs the browser-menu export, on the notes an Anki
search query finds (written to the addon's `output/` folder). An empty query skips it.

- `kanji_sentence_fine_tuning_data_query`: notes for "Export kanjify test data"
  (`kanjify_sentence_data.jsonl`, rows `{"sentence", "kanjified", "nids"}`: the furigana and
  kanjified sentence fields, one row per distinct sentence)

## optipnal specification

### `match_words_model` operation

- `replace_existing_matched_words`: (default: false) overwrite previously processed matched words?
