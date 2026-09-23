# Config

## General

- `log_level`: Default is "ERROR". Possible values from less logging to more: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
- `log_to_console` Default is `true`. If false, logs to files in the logs/ dir in the addon folder

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
while notes are being added, but Escape pressed while a run that was not cancelled saves its
edits just before stops the adding too. The notes not added are dropped, the words that were linked to them
are left to be matched again by the next run, and a numbering such as "(m1)" or "(r1)" they put
on the word's other notes is taken back. A note that failed to add keeps its placeholder id in
the word lists, which may help debugging, but only until the next match run over those sentences
puts the words back to be matched. The end message says how many new notes were added, how many
were left out by the cancel and how many failed.

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
  2. `kanji_story_field`
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
  10. `insert_deck` (optional) Used when generating TSVs for inserting new notes. If omitted, the
      file will simply not specify the deck

## test data exports

Tools > "AI ops: generate test data" runs the browser-menu export, on the notes an Anki
search query finds (written to the addon's `output/` folder). An empty query skips it.

- `kanji_sentence_fine_tuning_data_query`: notes for "Export kanjify test data"
  (`kanjify_sentence_data.jsonl`, rows `{"sentence", "kanjified", "nids"}`: the furigana and
  kanjified sentence fields, one row per distinct sentence)

## optipnal specification

### `match_words_model` operation

- `replace_existing_matched_words`: (default: false) overwrite previously processed matched words?
