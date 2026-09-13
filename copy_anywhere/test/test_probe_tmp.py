import definitions as d
from anki_shared.testing import real_anki
from conftest import VOCAB
from copy_anywhere.logic.copy_fields import copy_for_single_trigger_note


def test_probe(col, logger, tmp_path, monkeypatch):
    note = real_anki.add_note(col, VOCAB, {"Word": "neko", "Meaning": "cat"})
    definition = d.within_note(
        field_to_field_defs=[
            d.field_to_field("Note", "{{Word}}",
                             process_chain=[d.fonts_check_process("does_not_exist.json")]),
        ],
    )
    ok = copy_for_single_trigger_note(definition, note, logger=logger)
    print("OK:", ok)
    print("ERRORS:", logger.errors)
    print("DEBUGS:", [m for m in logger.debugs if "rocess" in m])
