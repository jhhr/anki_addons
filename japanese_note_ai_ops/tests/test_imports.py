"""Every op module the add-on loads at startup actually imports.

Compiling a module is not importing it. `python -m py_compile` builds the bytecode and stops,
so anything that only goes wrong while the module body runs - a name used above where it is
defined, an annotation evaluated eagerly, a circular import - compiles perfectly and then
brings Anki down at load with a bare traceback in the add-on manager.

That is not hypothetical. A TypedDict field was annotated with an alias defined six hundred
lines further down: fine to the compiler, since a function's annotations are not evaluated
when the module runs, and fatal for a TypedDict, whose class body is. The unit tests missed it
too, because none of them import the modules that reach for aqt.

anki_stubs can load those, so this walks the list and imports each one. It asserts nothing
about behaviour on purpose - it is here to fail at import, which is the whole of what it
catches and exactly the failure that reaches the user as a crash.
"""

import unittest

# Imported for the side effect: it puts the add-on's vendored lib/ on sys.path, which the ops
# need for json_repair, rapidfuzz and requests
import addon_modules  # noqa: F401
from anki_stubs import load_ops_module

# Every module __init__.py pulls in at startup, plus the two they rest on
STARTUP_MODULES = [
    "api_client",
    "concurrency",
    "collection_access",
    "word_index",
    "note_cache",
    "diagnostics",
    "base_ops",
    "clean_meaning",
    "make_all_meanings",
    "match_words_to_notes",
    "extract_words",
    "kanjify_sentence",
    "make_kanji_story",
    "translate_field",
    "migrate_compound_verbs",
    "new_note_all_ops",
]


class StartupImportTests(unittest.TestCase):
    def test_every_startup_module_imports(self):
        for name in STARTUP_MODULES:
            with self.subTest(module=name):
                self.assertIsNotNone(load_ops_module(name))


if __name__ == "__main__":
    unittest.main()
