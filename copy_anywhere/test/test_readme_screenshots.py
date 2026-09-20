"""The README's examples, and the harness that takes its screenshots.

Two halves, and only one of them runs in an ordinary test run.

`docs/examples/*.json` are the definitions the README quotes. They are loaded here the way
the addon loads a stored config -- through `Config` and the startup migration -- so an
example that would not survive a paste into `config.json` fails the suite rather than the
reader.

The screenshot harness is skipped unless `COPY_ANYWHERE_SCREENSHOTS` is set, because it
writes into the repository. One command regenerates every image in `docs/images/`:

    COPY_ANYWHERE_SCREENSHOTS=1 python -m pytest -q copy_anywhere/test/test_readme_screenshots.py

It renders the real dialogs on the offscreen platform plugin, with the Fusion style and a
fixed font so that two machines produce comparable pictures, and saves `widget.grab()`.
Nothing here asserts a pixel: what a shot is worth is a question for the reader of the
README, and a test that compared images would fail on every Qt or font update.

The one test that does run in the suite is the consistency check at the bottom: every image
the README links to exists, and every image generated here is linked to. It is what keeps a
renamed shot or a dropped section from leaving the README pointing at nothing.
"""

import json
import os
import re
from copy import deepcopy
from pathlib import Path

import pytest

import definitions as d
from conftest import DEFAULT_CONFIG, VOCAB
from copy_anywhere.configuration import Config, migrate_config
from copy_anywhere.logic.definition_migration import migrate_definition_v1_to_v2
from copy_anywhere.logic.definition_schema import (
    ALL_STAGE_TYPES,
    SYNTAX_VERSION_LEGACY,
    validate_definition_structure,
    value_expression,
)
from copy_anywhere.logic.flow_analysis import analyze_definition, make_lookup, refresh_effects
from copy_anywhere.ui.stage_document import StageDocument, default_stage
from copy_anywhere.ui.stage_edit_state import StageEditState
from copy_anywhere.ui.stage_editor_context import build_contexts, make_note_types_for
from copy_anywhere.ui.stage_editors import StageEditorEnvironment, make_stage_editor

#: Set to anything to have the harness below rewrite `docs/images/`.
SCREENSHOT_ENV = "COPY_ANYWHERE_SCREENSHOTS"

ADDON_DIR = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ADDON_DIR / "docs" / "examples"
IMAGES_DIR = ADDON_DIR / "docs" / "images"
README = ADDON_DIR / "README.md"

#: The example the README walks through, and the order its steps are shown in.
WALKTHROUGH = "collect-meanings.json"

#: Fusion renders the same everywhere Qt does; the platform styles do not. The font is
#: named rather than left to the platform for the same reason -- a shot whose text is
#: wider on one machine wraps differently and reads as a different dialog.
SCREENSHOT_STYLE = "Fusion"
SCREENSHOT_FONT = ("DejaVu Sans", 10)


def example_files() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.json"))


def read_examples() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in example_files()]


def example(name: str, definitions: list[dict]) -> dict:
    """The example loaded from `<name>.json`, by the guid the file carries."""
    guid = json.loads((EXAMPLES_DIR / name).read_text(encoding="utf-8"))["guid"]
    return next(one for one in definitions if one["guid"] == guid)


@pytest.fixture
def seeded(col):
    """A few notes of the trigger note type, in the deck the examples name.

    Empty, a collection makes every picker in the editor say it has nothing to offer --
    no decks for this note type, no tags -- which is a picture of an empty collection
    rather than of the editor.
    """
    from anki_shared.testing import real_anki

    for word, reading, meaning in (
        ("猫", "ねこ", "cat"),
        ("犬", "いぬ", "dog"),
        ("鳥", "とり", "bird"),
    ):
        real_anki.add_note(
            col,
            VOCAB,
            {"Word": word, "Reading": reading, "Meaning": meaning, "Freq": "120"},
            deck_name="JP vocab",
            tags=["jp"],
        )
    return col


@pytest.fixture
def examples(col, stub_mw):
    """Every example, as the addon has them after loading and migrating a stored config."""
    stored = dict(DEFAULT_CONFIG)
    stored["copy_definitions"] = read_examples()
    stub_mw.addonManager.configs["copy_anywhere"] = stored
    # What Anki runs at startup: it migrates whatever is stored and recomputes `effects`
    # across the whole list, which is the only way a call stage's effects can be right.
    migrate_config()
    config = Config()
    config.load()
    return config.copy_definitions


# -- the examples ----------------------------------------------------------------------


def test_the_examples_are_the_definitions_the_readme_quotes():
    # A file the README quotes and the harness never opens, or the other way round, is the
    # second source R1 exists to prevent.
    assert [path.name for path in example_files()] == [
        "archive-to-a-file.json",
        "collect-meanings.json",
        "fill-a-field.json",
        "flag-and-export.json",
    ]


def test_every_example_survives_the_load_path(examples, logger):
    lookup = make_lookup(examples)
    for definition in examples:
        name = definition.get("definition_name")
        assert validate_definition_structure(definition) == [], name
        assert analyze_definition(definition, lookup=lookup).problem_messages() == [], name
    # The migration logs what it could not convert; nothing here should have anything to say.
    assert logger.errors == []


def test_every_example_carries_the_effects_the_analyser_computes(examples):
    # The files ship `effects` because a stored definition has them and the hooks read the
    # stored copy: an example pasted into a config that is already current is never
    # migrated, so the effects it arrives with are the ones it keeps. `examples` has been
    # through the load path, which recomputes them across the whole list.
    assert refresh_effects(examples) == []
    computed = {definition["guid"]: definition["effects"] for definition in examples}
    for definition in read_examples():
        assert definition["effects"] == computed[definition["guid"]], definition["guid"]


def test_the_examples_cover_what_the_readme_has_to_explain(examples):
    fill = example("fill-a-field.json", examples)
    collect = example(WALKTHROUGH, examples)
    flag = example("flag-and-export.json", examples)
    archive = example("archive-to-a-file.json", examples)

    # A within-note fill and the walkthrough only touch the note that triggered them.
    assert fill["effects"]["add_note_compatible"]
    assert collect["effects"]["queries_collection"]
    assert collect["effects"]["add_note_compatible"]
    # The card action is on the trigger's own cards, which is possible everywhere but in
    # the Add dialog, so it does not cost the definition its compatibility.
    assert flag["effects"]["edits_trigger_cards"]
    assert flag["effects"]["add_note_compatible"]
    assert [export["name"] for export in flag["exports"]] == ["Status"]
    # And the one that calls it and writes a file may not run while a note is being added.
    assert archive["effects"]["calls_definitions"]
    assert archive["effects"]["writes_files"]
    assert not archive["effects"]["add_note_compatible"]


# -- the harness -----------------------------------------------------------------------

screenshots = pytest.mark.skipif(
    not os.environ.get(SCREENSHOT_ENV),
    reason=f"set {SCREENSHOT_ENV} to regenerate the README's images",
)


class Shooter:
    """Saves one widget per call into `docs/images/`, at a size that widget is happy at."""

    def __init__(self, qapp) -> None:
        self.qapp = qapp
        #: Frames built by `part` below. Qt owns a widget only through its parent, so an
        #: orphaned frame would be collected -- with the widget being photographed in it.
        self._frames: list = []

    def settle(self, widget) -> None:
        """Let Qt finish with this widget, and do now what it meant to do later.

        Two deferrals matter. The stage editor re-analyses 300ms after the last edit, so a
        dialog grabbed before that is one edit behind in its status line, its pickers and
        its preview. The card actions editor builds its rows a few per tick, so a stage
        that arrived with actions shows a "Loading card actions..." line until it has
        finished; both editors offer a synchronous way to get there, which is what this
        takes.
        """
        from aqt.qt import QEvent
        from copy_anywhere.ui.card_actions_editor import CardActionsEditor

        deferred_delete = QEvent.Type.DeferredDelete
        timer = getattr(widget, "_refresh_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            widget.refresh_status()
        for actions in widget.findChildren(CardActionsEditor):
            actions.finish_loading_initial_actions()
        if isinstance(widget, CardActionsEditor):
            widget.finish_loading_initial_actions()
        for _ in range(3):
            self.qapp.processEvents()
            # A rebuilt panel drops its old rows with `deleteLater`, and a widget waiting
            # to be deleted is still a visible child at whatever geometry it had. The
            # application's event loop collects them between events; `processEvents` does
            # not, so a shot taken here would have the old rows painted over the new ones.
            self.qapp.sendPostedEvents(None, deferred_delete)

    def part(self, widget):
        """One sub-editor, in a frame of its own, ready to be sized like any other shot.

        A widget inside a dialog is sized by the layout it sits in, so resizing it does
        nothing: the next layout pass puts it back. Moving it into a frame of its own is
        what lets it be photographed at the width the README wants it at. The editor it
        came out of is not used again.
        """
        from aqt.qt import QVBoxLayout, QWidget

        frame = QWidget()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(widget)
        self._frames.append(frame)
        return frame

    def save(self, widget, name: str, width: int, height: int = 0) -> Path:
        """Photograph `widget` at this width, at the height it asks for when given none.

        `show()` on the offscreen plugin draws nothing, but it is what makes Qt lay the
        widget out for real: a widget that has never been shown keeps whatever geometry its
        children were built with, and the shot comes out with its labels on top of each
        other.
        """
        widget.resize(width, height or widget.sizeHint().height())
        widget.show()
        self.settle(widget)
        if not height:
            widget.resize(
                width, max(widget.sizeHint().height(), widget.minimumSizeHint().height())
            )
            self.settle(widget)
        path = IMAGES_DIR / f"{name}.png"
        assert widget.grab().save(str(path), "PNG"), path
        widget.hide()
        return path


@pytest.fixture
def shots(qapp):
    """The Fusion style and a fixed font, put back afterwards."""
    from aqt.qt import QFont, QStyleFactory

    previous_style = qapp.style().objectName()
    previous_font = qapp.font()
    qapp.setStyle(QStyleFactory.create(SCREENSHOT_STYLE))
    qapp.setFont(QFont(*SCREENSHOT_FONT))
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    yield Shooter(qapp)
    qapp.setStyle(QStyleFactory.create(previous_style))
    qapp.setFont(previous_font)


@pytest.fixture
def open_dialog(col, qapp):
    """Builds staged-definition dialogs and disposes of them the way the UI suite does."""
    from copy_anywhere.ui.edit_staged_definition_dialog import EditStagedDefinitionDialog

    built = []

    def make(definition, all_definitions=()):
        dialog = EditStagedDefinitionDialog(
            None, deepcopy(definition), [deepcopy(one) for one in all_definitions]
        )
        built.append(dialog)
        return dialog

    yield make
    for dialog in built:
        dialog._refresh_timer.stop()
        dialog.deleteLater()


def editor_for(definition: dict, guid: str, parent, all_definitions=()):
    """One stage's editor, built the way the stage list builds it."""
    document = StageDocument(deepcopy(definition))
    environment = StageEditorEnvironment(
        make_note_types_for(document.definition),
        [document.definition, *all_definitions],
        document.definition.get("guid", ""),
    )
    return make_stage_editor(
        parent, document.stage(guid), build_contexts(document)[guid], environment
    )


def expression_editor(definition: dict, guid: str, parent, expression, label: str):
    """A value expression editor in the scope of one stage of `definition`."""
    from copy_anywhere.ui.value_expression_editor import ValueExpressionEditor

    document = StageDocument(deepcopy(definition))
    context = build_contexts(document)[guid]
    state = StageEditState(context, selected_models=[], target_is_trigger=True)
    return ValueExpressionEditor(parent, expression, context, state, label)


@screenshots
def test_shoot_the_definitions_list(seeded, examples, shots):
    from copy_anywhere.ui.pick_copy_definition_dialog import (
        DefinitionRow,
        PickCopyDefinitionDialog,
    )

    width = 900
    dialog = PickCopyDefinitionDialog(None, deepcopy(examples), [], None)
    # Each row fixes its width from the dialog's, which the dialog took from the screen
    # before the rows existed. The offscreen plugin's screen is small enough to elide the
    # definition names, so the rows are given the width the shot is taken at instead.
    for row in dialog.findChildren(DefinitionRow):
        row.setFixedWidth(width - 50)
    try:
        shots.save(dialog, "definitions-list", width, 300)
    finally:
        dialog.deleteLater()


@screenshots
def test_shoot_the_stage_editor(seeded, examples, shots, open_dialog):
    dialog = open_dialog(example(WALKTHROUGH, examples), examples)
    shots.save(dialog, "stage-editor", 1300, 900)


@screenshots
def test_shoot_the_walkthrough_step_by_step(seeded, examples, shots, open_dialog):
    """The walkthrough definition as it is built, one stage at a time.

    Each step is the definition with one more stage than the last, its newest stage open,
    which is what the README's walkthrough section says in words.
    """
    walkthrough = example(WALKTHROUGH, examples)
    stages = walkthrough["stages"]
    for step, stage in enumerate(stages, start=1):
        partial = deepcopy(walkthrough)
        partial["stages"] = deepcopy(stages[:step])
        dialog = open_dialog(partial, examples)
        dialog.stage_tree.expand(stage["guid"])
        name = re.sub(r"[^a-z0-9]+", "-", (stage.get("name") or stage["type"]).lower())
        shots.save(dialog.inner_widget, f"walkthrough-{step}-{name.strip('-')}", 820, 700)


@screenshots
def test_shoot_every_stage_editor(seeded, examples, shots, widget_parent):
    """One shot per stage type: from an example where there is one, from a new stage where
    there is not.

    A new stage is what the user sees after Add Stage, so the types no example uses are
    worth showing that way rather than not at all.
    """
    seen = set()
    for definition in examples:
        for stage in definition["stages"]:
            for one in [stage, *stage.get("body", [])]:
                stage_type = one["type"]
                if stage_type in seen:
                    continue
                seen.add(stage_type)
                editor = editor_for(definition, one["guid"], widget_parent, examples)
                shots.save(shots.part(editor), f"stage-{stage_type}", 780)
    blank = d.staged("A definition", stages=[], note_types=[VOCAB])
    for stage_type in ALL_STAGE_TYPES:
        if stage_type in seen:
            continue
        definition = deepcopy(blank)
        definition["stages"] = [default_stage(stage_type, "s")]
        editor = editor_for(definition, "s", widget_parent, examples)
        shots.save(shots.part(editor), f"stage-{stage_type}", 780)


@screenshots
def test_shoot_the_sub_editors(seeded, examples, shots, widget_parent):
    flag = example("flag-and-export.json", examples)
    editor = editor_for(flag, "flag-note", widget_parent, examples)
    shots.save(shots.part(editor.card_actions), "card-actions-editor", 780)
    shots.save(shots.part(editor.tag_editor), "tag-editor", 780)

    from copy_anywhere.ui.stage_exports_editor import ExportsEditor

    exports = ExportsEditor(widget_parent, StageDocument(deepcopy(flag)))
    shots.save(shots.part(exports), "exports-panel", 780)


@screenshots
def test_shoot_the_value_expression_editor(seeded, examples, shots, widget_parent):
    """The one editor every computed value is written in, in each state worth explaining.

    The last two are the same picture from the reader's point of view -- a reference in
    red under the box -- and they are two shots because they are two mistakes: a name
    typed wrong, and a format-1 spelling left behind by a migrated expression whose text
    has since been edited.
    """
    collect = example(WALKTHROUGH, examples)

    def editor(expression):
        return shots.part(
            expression_editor(collect, "collect-write", widget_parent, expression, "Its value")
        )

    shots.save(
        editor(value_expression(text="{{trigger.Word}} ({{trigger.Reading}})")),
        "value-text",
        780,
    )
    shots.save(
        editor(value_expression(code='return trigger["Word"].upper()', mode="code")),
        "value-code",
        780,
    )
    shots.save(editor(value_expression(text="{{trigger.Wrod}}")), "value-unknown-reference", 780)
    shots.save(
        editor(value_expression(text="{{Word}}", syntax_version=SYNTAX_VERSION_LEGACY)),
        "value-legacy",
        780,
    )


@screenshots
def test_shoot_the_add_note_warnings(seeded, examples, shots, open_dialog):
    """The two amber notes the editor puts under its status line.

    `flag-and-export` earns the impossible one on its own: it flags the trigger's own card,
    which the note being added does not have yet. `archive-to-a-file` earns both, because
    it writes a file as well -- which is the forbidden half.
    """
    flag = open_dialog(example("flag-and-export.json", examples), examples)
    shots.save(shots.part(flag.status_label), "add-note-impossible", 780)
    archive = open_dialog(example("archive-to-a-file.json", examples), examples)
    shots.save(shots.part(archive.status_label), "add-note-both", 780)


@screenshots
def test_shoot_what_a_migrated_definition_looks_like(seeded, shots, widget_parent):
    """The one migration state the editor shows: a selection format 1 refused to make.

    The input is a format-1 definition, so it cannot be one of the examples -- those are
    what the editor writes, not what it reads on the way in.
    """
    legacy = d.destination_to_sources(
        definition_name="An old definition",
        field_to_field_defs=[d.field_to_field("Note", "{{Word}}")],
        copy_from_cards_query='note:"CA Vocab"',
        select_card_by="Blue-Random",
        select_card_count="3",
        sort_by_field="Freq",
    )
    migrated = migrate_definition_v1_to_v2(legacy)
    query = next(
        stage for stage in migrated["stages"] if stage["type"] in ("note_query", "card_query")
    )
    editor = editor_for(migrated, query["guid"], widget_parent)
    shots.save(shots.part(editor), "migrated-selection-refused", 780)


# -- the README and the images ---------------------------------------------------------

IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def test_the_readme_and_the_images_say_the_same_thing():
    """Every image the README links to is there, and every image here is linked to.

    Both directions matter: a link to a file that was never generated shows the reader a
    broken image, and an image nothing links to is a shot whose section was dropped.
    """
    referenced = {
        Path(link).name
        for link in IMAGE_LINK_RE.findall(README.read_text(encoding="utf-8"))
        if link.startswith("docs/images/")
    }
    generated = {path.name for path in IMAGES_DIR.glob("*.png")}

    assert referenced - generated == set(), "the README links to images that are not there"
    assert generated - referenced == set(), "these images are in docs/images/ unreferenced"
