A rather complicated Anki addon for some custom AI prompts I use.

- Provides a framework of functions to use for adding new prompts. API calls are done asynchronously in parallel so hundreds of prompts can be run as fast the model's rate limit allows.
- Prompts are available in right-click menu of notes in Anki's card browser and some run automatically when editing a field or adding a note.

Prompts:

- `clean_meaning`: Automatically reduce a full jp dictionary entry gotten from MDX dictionaries into just the part that pertains to the specific example sentence used for the word.
  - Also generates new meanings when no dict entry is present. Applied on adding new notes, so I used it often. Intended to be used with a deck structure where you have multiple notes for different meanings of a word which each contains an example sentence for that particular meaning.
  - Also will update all meanings of the word, if there are multiple, using the dictionary entry looked up from the MDX dictionaries.
- `translate_field`: Translate a field from Japanese to English. Used rarely, since I mostly mine from anime and get the translation from the english subs.
- `make_kanji_story`: Write a mnemonic story for the components a kanji is written with, in Japanese. Used daily on new kanji drawing practice notes. Expects a JSON file `_kanji_story_component_words.json` to exist and be a simple dict of component phrases.
- `kanjify_sentence`: Take a furigana format sentence and kanjify each hiragana/katakana word if there's some valid kanji form for it. The kanjified words are wrapped with `<k>` tags
- `extract_words`: Partition the kanjified sentence into its dictionary words and write the word array into the note (`word_array/README.md`). Built by rules from SudachiPy and JMdict, so the only API call is the proper noun one (`proper_nouns_model`); the words are left unjudged. "Extract words + Judge matchability" runs the word matching judge over the new words as a second phase.
- `match_words_to_notes`: Match the extracted words in the list to an existing word note, using the word's meaning. If matching isn't possible, creates a new word note and comes up with a meaning that matches the usage of the word in the sentence.

## Running several ops at once

Selecting thousands of notes in the browser makes Anki lag, and right-clicking them lags again. The browser's Edit > "Japanese AI ops..." dialog (also "Run several ops..." at the top of the "AI helper" right-click submenu; an optional shortcut is set with `multi_op_dialog_shortcut` in the config) avoids that: leave one note selected, open it and click "Use all notes from current search". It always opens on "Use selected notes (N)", even with nothing selected, so the search is only ever run on after that click and a stray Enter cannot start a run over it. The label under the lists says how many notes will be processed. The search is the one the browser last ran, so the notes it shows, not text typed into the search box without pressing Enter.

Click ops in the left list ("Available ops") to add them, in that order, to the right list ("Run in this order"). Reorder them by dragging or with Up/Down; Remove or a double click takes one back out, Clear empties the list. Run (bottom left) runs them one after another on the same notes, under one progress dialog that stays open until the last has finished ("Step 1/3: ..." in its title). Each op is a normal run with its own entry in Edit > Undo. Cancelling a step keeps what it had finished and skips the rest; so does a step that fails or stops (usage limit, expired login). One summary at the end lists what each step did and which did not run; for a step that failed it gives the error message (the full traceback is logged). Escape cancels only while the Cancel button is enabled. In the "Japanese AI ops" dialog, Enter presses Close (bottom right), never Run.

## Installing dependencies

First install mdict-query manually from GitHub (repo has no setup.py):

**Windows (PowerShell):**

```powershell
cd lib
New-Item -ItemType Directory -Force -Path mdict_query
cd mdict_query
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mmjang/mdict-query/master/mdict_query.py" -OutFile "__init__.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mmjang/mdict-query/master/readmdict.py" -OutFile "readmdict.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mmjang/mdict-query/master/ripemd128.py" -OutFile "ripemd128.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mmjang/mdict-query/master/pureSalsa20.py" -OutFile "pureSalsa20.py"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/mmjang/mdict-query/master/lzo.py" -OutFile "lzo.py"
cd ..\..
```

**Linux/macOS:**

```bash
cd lib
mkdir -p mdict_query
cd mdict_query
curl -o __init__.py https://raw.githubusercontent.com/mmjang/mdict-query/master/mdict_query.py
curl -O https://raw.githubusercontent.com/mmjang/mdict-query/master/readmdict.py
curl -O https://raw.githubusercontent.com/mmjang/mdict-query/master/ripemd128.py
curl -O https://raw.githubusercontent.com/mmjang/mdict-query/master/pureSalsa20.py
curl -O https://raw.githubusercontent.com/mmjang/mdict-query/master/lzo.py
cd ../..
```

Then install other dependencies:

```bash
pip3 install --upgrade -t lib --no-cache-dir --python-version 3.9 --only-binary=:all: -r requirements.txt
```

## Running the tests

`test/` covers the two modules that carry the tricky concurrent behaviour - the rate-limit
and retry handling in `async_api_ops/api_client.py`, and the memory-aware gate in
`async_api_ops/concurrency.py`. Both are deliberately free of `aqt`/`anki` imports, so the
suite loads them straight from their files and runs outside Anki, with no network and no
waiting: a fake session supplies responses, a fake clock makes backoffs pass instantly, and
the memory probes are stubbed.

```bash
pytest test              # from the add-on root
python -m unittest discover -s test -t test     # same tests, no pytest needed
```

Run it as `pytest test`, not bare `pytest`: the add-on directory is itself a package whose
`__init__.py` imports `aqt`, and pytest imports the `__init__.py` of any package directory in
the collection tree. `test/pytest.ini` keeps the rootdir below that.

Tests are plain `unittest.TestCase` classes so both runners work. Anything that needs a real
collection, `mw`, or a running Anki belongs in a manual check instead - `base_ops.py` and the
ops themselves are not covered here.
