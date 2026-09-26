"""The editable stage tree behind the format-2 editor.

The dialog holds one `StageDocument`. Every structural edit the user makes -- add, delete,
duplicate, move, enable -- goes through it, and nothing else touches the stage list. That
split is deliberate: the interesting parts of a stage editor are "where may this stage go",
"what is in scope here" and "may this be saved", and none of them need a widget to answer.
They are all testable without Qt, and they are tested that way.

The document owns a deep copy of the definition it was handed, so cancelling a dialog
cannot leave a half-edited definition behind in the config.
"""

from copy import deepcopy
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional
import uuid

from ..logic.definition_schema import (
    ALL_STAGE_TYPES,
    EMPTY_EFFECTS,
    Export,
    FORMAT_VERSION,
    STAGE_CALL_DEFINITION,
    STAGE_CARD_QUERY,
    STAGE_CONDITION,
    STAGE_EDIT_CARD,
    STAGE_EDIT_NOTE,
    STAGE_FOR_EACH_CARD,
    STAGE_FOR_EACH_NOTE,
    STAGE_LIST_VARIABLE,
    STAGE_NOTE_QUERY,
    STAGE_READ_FILE,
    STAGE_REDUCE,
    STAGE_STORE,
    STAGE_VARIABLE,
    STAGE_WRITE_FILE,
    SchemaProblem,
    Stage,
    TEXT,
    CopyDefinitionV2,
    expression_is_code,
    is_format_2,
    new_definition,
    stage_body_blocks,
    stage_result_name,
    value_expression,
    walk_stages,
)
from ..logic.flow_analysis import AnalysisResult, analyze_definition
from ..logic.unsaved_note_search import UnjudgeableSearch, UnsavedNoteSearchError, parse_search

#: Human labels for the Add Stage menu and the stage row headers, in menu order (§10).
STAGE_TYPE_LABELS: dict[str, str] = {
    STAGE_VARIABLE: "Variable",
    STAGE_NOTE_QUERY: "Query Notes",
    STAGE_CARD_QUERY: "Query Cards",
    STAGE_EDIT_NOTE: "Edit Note",
    STAGE_EDIT_CARD: "Edit Card",
    STAGE_READ_FILE: "Read File",
    STAGE_WRITE_FILE: "Write File",
    STAGE_LIST_VARIABLE: "List",
    STAGE_STORE: "Store",
    STAGE_FOR_EACH_NOTE: "Loop Over Notes",
    STAGE_FOR_EACH_CARD: "Loop Over Cards",
    STAGE_REDUCE: "Reduce",
    STAGE_CONDITION: "Condition",
    STAGE_CALL_DEFINITION: "Call Definition",
}

#: The order the Add Stage menu offers them in.
STAGE_TYPE_MENU_ORDER: tuple[str, ...] = tuple(STAGE_TYPE_LABELS)

#: A glyph per stage type. Qt themes cannot be relied on for icons across platforms, and a
#: character renders the same everywhere the addon runs.
STAGE_TYPE_ICONS: dict[str, str] = {
    STAGE_VARIABLE: "=",
    STAGE_NOTE_QUERY: "?",
    STAGE_CARD_QUERY: "?",
    STAGE_EDIT_NOTE: "✎",
    STAGE_EDIT_CARD: "✎",
    STAGE_READ_FILE: "←",
    STAGE_WRITE_FILE: "→",
    STAGE_LIST_VARIABLE: "[ ]",
    STAGE_STORE: "+",
    STAGE_FOR_EACH_NOTE: "↻",
    STAGE_FOR_EACH_CARD: "↻",
    STAGE_REDUCE: "Σ",
    STAGE_CONDITION: "⎇",
    STAGE_CALL_DEFINITION: "→()",
}

#: Labels for the child blocks of structural stages, shown as the block heading.
BODY_KEY_LABELS: dict[str, str] = {
    "body": "Do",
    "then": "Then",
    "else": "Otherwise",
}


class StageLocation(NamedTuple):
    """Where a stage sits: which block holds it, and at which index.

    `parent_guid` is None for the definition's root block. `body_key` names which of a
    structural stage's blocks -- a condition has two.
    """

    parent_guid: Optional[str]
    body_key: Optional[str]
    index: int


def new_guid() -> str:
    return str(uuid.uuid4())


def default_stage(stage_type: str, guid: Optional[str] = None) -> Stage:
    """A structurally complete stage of this type, with the parts the user must fill blank.

    Blank required names and bindings are left blank rather than guessed: the analyser then
    reports exactly what is missing, which is more useful than a plausible-looking default
    that silently reads the wrong note.
    """
    if stage_type not in ALL_STAGE_TYPES:
        raise ValueError(f"unknown stage type {stage_type!r}")
    stage: Stage = {"guid": guid or new_guid(), "type": stage_type, "enabled": True}
    if stage_type == STAGE_VARIABLE:
        stage.update({"result": "", "value": value_expression()})  # type: ignore[typeddict-item]
    elif stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
        stage.update(  # type: ignore[typeddict-item]
            {
                "result": "",
                "query": value_expression(),
                "selection": {
                    "strategy": "all",
                    "count": None,
                    "sort_field": None,
                    "sort_order": "descending",
                },
                "if_empty": "continue",
            }
        )
    elif stage_type == STAGE_EDIT_NOTE:
        stage.update(  # type: ignore[typeddict-item]
            {
                "target": {"binding": "trigger"},
                "fields": [],
                "tags": {"add": [], "remove": []},
                "card_actions": [],
                "read_semantics": "stage_snapshot",
            }
        )
    elif stage_type == STAGE_EDIT_CARD:
        stage.update({"target": {"binding": ""}, "card_actions": []})  # type: ignore[typeddict-item]
    elif stage_type == STAGE_READ_FILE:
        stage.update(  # type: ignore[typeddict-item]
            {"result": "", "filename": value_expression(), "if_missing": "empty"}
        )
    elif stage_type == STAGE_WRITE_FILE:
        stage.update(  # type: ignore[typeddict-item]
            {
                "filename": value_expression(),
                "content": value_expression(),
                "overwrite": False,
            }
        )
    elif stage_type == STAGE_LIST_VARIABLE:
        stage.update({"result": "", "item_type": TEXT})  # type: ignore[typeddict-item]
    elif stage_type == STAGE_STORE:
        stage.update(  # type: ignore[typeddict-item]
            {"target": {"kind": "list", "binding": ""}, "value": value_expression()}
        )
    elif stage_type == STAGE_FOR_EACH_NOTE:
        stage.update(  # type: ignore[typeddict-item]
            {"input": {"binding": ""}, "item_binding": "note", "body": []}
        )
    elif stage_type == STAGE_FOR_EACH_CARD:
        stage.update(  # type: ignore[typeddict-item]
            {
                "input": {"binding": ""},
                "item_binding": "card",
                "note_binding": "note",
                "body": [],
            }
        )
    elif stage_type == STAGE_REDUCE:
        stage.update(  # type: ignore[typeddict-item]
            {
                "input": {"binding": ""},
                "result": "",
                "initial": value_expression(),
                "item_binding": "item",
                "accumulator_binding": "accumulator",
                "value": value_expression(mode="code"),
            }
        )
    elif stage_type == STAGE_CONDITION:
        stage.update(  # type: ignore[typeddict-item]
            {"predicate": value_expression(mode="code"), "then": [], "else": []}
        )
    elif stage_type == STAGE_CALL_DEFINITION:
        stage.update(  # type: ignore[typeddict-item]
            {"definition_guid": "", "trigger": {"binding": "trigger"}, "outputs": []}
        )
    return stage


def reguid_stage(stage: Stage, make_guid: Callable[[], str] = new_guid) -> Stage:
    """A deep copy of a stage subtree with every guid replaced.

    Duplicating a stage has to renumber its whole subtree: two stages sharing a guid would
    make trace correlation and editor focus ambiguous, and exports reference producers by
    guid.
    """
    copied = deepcopy(stage)
    copied["guid"] = make_guid()
    for _key, block in stage_body_blocks(copied):
        for index, child in enumerate(block):
            block[index] = reguid_stage(child, make_guid)
    return copied


class StageDocument:
    """One definition being edited, plus the analysis of its current state."""

    def __init__(
        self,
        definition: Optional[CopyDefinitionV2] = None,
        lookup: Optional[Callable[[str], Optional[CopyDefinitionV2]]] = None,
        make_guid: Callable[[], str] = new_guid,
        known_fields: Optional[Callable[[CopyDefinitionV2], Mapping[str, set[str]]]] = None,
    ) -> None:
        self._make_guid = make_guid
        if definition is None:
            self.definition: CopyDefinitionV2 = new_definition(make_guid())
        elif not is_format_2(definition):
            raise ValueError("StageDocument edits format 2 definitions only")
        else:
            self.definition = deepcopy(definition)
        self.definition.setdefault("stages", [])
        self.definition.setdefault("exports", [])
        self.definition.setdefault("format_version", FORMAT_VERSION)
        self._lookup = lookup
        # Asked again for every analysis rather than captured: the trigger note types are
        # edited in the same dialog, so the fields a `trigger.X` reference is checked
        # against have to be the ones chosen now. Without it the analyser checks the head
        # of a reference and no further.
        self._known_fields = known_fields
        self._analysis: Optional[AnalysisResult] = None

    # -- analysis ------------------------------------------------------------------------

    def invalidate(self) -> None:
        """Drop the cached analysis. Called by every mutation, and by the dialog when a
        stage editor writes a change back into the stage dict in place."""
        self._analysis = None

    @property
    def analysis(self) -> AnalysisResult:
        if self._analysis is None:
            self._analysis = analyze_definition(
                self.definition,
                self._lookup,
                known_fields=(
                    self._known_fields(self.definition) if self._known_fields else None
                ),
            )
        return self._analysis

    def problems_for(self, guid: str) -> list[SchemaProblem]:
        return _unique(
            problem for problem in self.analysis.problems if problem.stage_guid == guid
        )

    def warnings_for(self, guid: str) -> list[SchemaProblem]:
        return _unique(
            problem for problem in self.analysis.warnings if problem.stage_guid == guid
        )

    def definition_problems(self) -> list[SchemaProblem]:
        """Problems that belong to no stage -- exports, the definition's own shape."""
        guids = {stage.get("guid") for stage in walk_stages(self.definition["stages"])}
        return [
            problem
            for problem in self.analysis.problems
            if problem.stage_guid is None or problem.stage_guid not in guids
        ]

    # -- navigation ----------------------------------------------------------------------

    def root_block(self) -> list[Stage]:
        return self.definition["stages"]

    def stage(self, guid: str) -> Optional[Stage]:
        for stage in walk_stages(self.definition["stages"]):
            if stage.get("guid") == guid:
                return stage
        return None

    def block(self, parent_guid: Optional[str], body_key: Optional[str]) -> Optional[list[Stage]]:
        if parent_guid is None:
            return self.root_block()
        parent = self.stage(parent_guid)
        if parent is None:
            return None
        for key, block in stage_body_blocks(parent):
            if key == body_key:
                # `stage_body_blocks` substitutes an empty list for a missing key, so write
                # it back before handing it out or an append would go nowhere.
                if not isinstance(parent.get(key), list):
                    parent[key] = block  # type: ignore[literal-required]
                return parent[key]  # type: ignore[literal-required]
        return None

    def location(self, guid: str) -> Optional[StageLocation]:
        def search(
            block: list[Stage], parent_guid: Optional[str], body_key: Optional[str]
        ) -> Optional[StageLocation]:
            for index, stage in enumerate(block):
                if stage.get("guid") == guid:
                    return StageLocation(parent_guid, body_key, index)
                for key, child_block in stage_body_blocks(stage):
                    found = search(child_block, stage.get("guid"), key)
                    if found is not None:
                        return found
            return None

        return search(self.root_block(), None, None)

    def ancestors(self, guid: str) -> list[Stage]:
        """The structural stages containing this one, outermost first."""
        chain: list[Stage] = []
        location = self.location(guid)
        while location is not None and location.parent_guid is not None:
            parent = self.stage(location.parent_guid)
            if parent is None:
                break
            chain.insert(0, parent)
            location = self.location(location.parent_guid)
        return chain

    def path_of(self, guid: str) -> str:
        """A readable path for listing an offending stage, e.g. "Loop Over Notes > Edit Note"."""
        stage = self.stage(guid)
        if stage is None:
            return guid
        parts = [stage_label(parent) for parent in self.ancestors(guid)]
        parts.append(stage_label(stage))
        return " > ".join(parts)

    def is_at_root(self, guid: str) -> bool:
        location = self.location(guid)
        return location is not None and location.parent_guid is None

    # -- mutation ------------------------------------------------------------------------

    def add_stage(
        self,
        stage_type: str,
        parent_guid: Optional[str] = None,
        body_key: Optional[str] = None,
        index: Optional[int] = None,
    ) -> Optional[Stage]:
        block = self.block(parent_guid, body_key)
        if block is None:
            return None
        stage = default_stage(stage_type, self._make_guid())
        block.insert(len(block) if index is None else index, stage)
        self.invalidate()
        return stage

    def insert_stage(
        self,
        stage: Stage,
        parent_guid: Optional[str] = None,
        body_key: Optional[str] = None,
        index: Optional[int] = None,
    ) -> bool:
        block = self.block(parent_guid, body_key)
        if block is None:
            return False
        block.insert(len(block) if index is None else index, stage)
        self.invalidate()
        return True

    def remove_stage(self, guid: str) -> Optional[Stage]:
        location = self.location(guid)
        if location is None:
            return None
        block = self.block(location.parent_guid, location.body_key)
        if block is None:
            return None
        removed = block.pop(location.index)
        # Exports naming a stage that is gone would be invisible corruption: the analyser
        # reports a missing producer, but the entry belongs to no row the user can see.
        gone = {stage.get("guid") for stage in walk_stages([removed])}
        self.definition["exports"] = [
            export
            for export in self.definition.get("exports", [])
            if export.get("stage_guid") not in gone
        ]
        self.invalidate()
        return removed

    def duplicate_stage(self, guid: str) -> Optional[Stage]:
        location = self.location(guid)
        stage = self.stage(guid)
        if location is None or stage is None:
            return None
        copy = reguid_stage(stage, self._make_guid)
        # Result names are unique per block, so a duplicate cannot keep the original's.
        # Blanking is the honest move: the analyser then asks for a name.
        _blank_result_names(copy)
        block = self.block(location.parent_guid, location.body_key)
        if block is None:
            return None
        block.insert(location.index + 1, copy)
        self.invalidate()
        return copy

    def set_enabled(self, guid: str, enabled: bool) -> bool:
        stage = self.stage(guid)
        if stage is None:
            return False
        stage["enabled"] = enabled
        self.invalidate()
        return True

    def move_within_block(self, guid: str, offset: int) -> bool:
        """Move a stage up or down among its siblings. Blocks do not spill into each other:
        a stage leaves its block only through `move_into` / `move_out`, where the user says
        so explicitly."""
        location = self.location(guid)
        if location is None:
            return False
        block = self.block(location.parent_guid, location.body_key)
        if block is None:
            return False
        target = location.index + offset
        if target < 0 or target >= len(block):
            return False
        block.insert(target, block.pop(location.index))
        self.invalidate()
        return True

    def move_targets(self, guid: str) -> list[tuple[Optional[str], Optional[str], str]]:
        """Every block this stage could be moved into, as (parent_guid, body_key, label).

        A stage may move into any structural stage that is not itself, not inside itself,
        and not the block it already sits in.
        """
        location = self.location(guid)
        if location is None:
            return []
        own = {stage.get("guid") for stage in walk_stages([self.stage(guid) or {}])}
        targets: list[tuple[Optional[str], Optional[str], str]] = []
        if location.parent_guid is not None:
            targets.append((None, None, "the definition's top level"))
        for stage in walk_stages(self.root_block()):
            if stage.get("guid") in own:
                continue
            for key, _block in stage_body_blocks(stage):
                if stage.get("guid") == location.parent_guid and key == location.body_key:
                    continue
                label = stage_label(stage)
                block_name = BODY_KEY_LABELS.get(key, key)
                targets.append((stage.get("guid"), key, f"{label} → {block_name}"))
        return targets

    def move_to(
        self, guid: str, parent_guid: Optional[str], body_key: Optional[str], index: Optional[int] = None
    ) -> bool:
        if parent_guid is not None:
            own = {stage.get("guid") for stage in walk_stages([self.stage(guid) or {}])}
            if parent_guid in own:
                return False
        # `remove_stage` strips the exports naming what it removed, which is right for a
        # delete and wrong here: the stage is still in the definition afterwards, so the
        # export is not dangling and the name the user chose is theirs to keep. An export
        # naming a stage that has landed inside a block is reported by the analyser and
        # stays visible in the Exports panel, where it can be cleared or ridden out until
        # the stage comes back to the top level.
        exports = list(self.exports())
        stage = self.remove_stage(guid)
        if stage is None:
            return False
        if not self.insert_stage(stage, parent_guid, body_key, index):
            # Put it back where it was rather than dropping the user's work on the floor.
            self.insert_stage(stage, None, None, None)
            self.set_exports(exports)
            return False
        self.set_exports(exports)
        return True

    # -- exports -------------------------------------------------------------------------

    def exportable_stages(self) -> list[tuple[str, str]]:
        """Root-block stages that produce a result, as (stage_guid, result name).

        Only the root block: a branch- or loop-local result may not run at all, and §5.9
        keeps exports free of maybe-defined values.
        """
        found: list[tuple[str, str]] = []
        for stage in self.root_block():
            if not stage.get("enabled", True):
                continue
            name = stage_result_name(stage)
            if name:
                found.append((stage.get("guid", ""), name))
            if stage.get("type") == STAGE_CALL_DEFINITION:
                for output in stage.get("outputs", []) or []:
                    if isinstance(output, dict) and output.get("result"):
                        found.append((stage.get("guid", ""), output["result"]))
        return found

    def exports(self) -> list[Export]:
        return self.definition.get("exports", []) or []

    def set_exports(self, exports: Iterable[Export]) -> None:
        self.definition["exports"] = list(exports)
        self.invalidate()

    # -- saving --------------------------------------------------------------------------

    def add_note_compatible(self) -> bool:
        return bool(self.analysis.effects.get("add_note_compatible", False))

    def wants_add_note(self) -> bool:
        triggers = self.definition.get("triggers", {}) or {}
        unfocus = triggers.get("on_unfocus", {}) or {}
        return bool(triggers.get("on_add")) or bool(unfocus.get("add_fields"))

    def save_blockers(self) -> list[str]:
        """Everything standing between this definition and a save, in the order to show it.

        Warnings -- mixed note types, an add-note trigger on a definition that edits other
        notes or cards -- are deliberately absent: §10 says they do not block.
        """
        blockers = [self._describe(problem) for problem in _unique(self.analysis.problems)]
        if not (self.definition.get("definition_name") or "").strip():
            blockers.append("The definition needs a name.")
        return blockers

    def can_save(self) -> bool:
        return not self.save_blockers()

    def warnings(self) -> list[str]:
        """What is worth telling the user about this definition without refusing the save."""
        found = [problem.message for problem in _unique(self.analysis.warnings)]
        found.extend(self.add_note_warnings())
        return found

    def add_note_warnings(self) -> list[str]:
        """What running while a note is being added means for this definition.

        Different things can be worth saying, and one definition can deserve several, so
        they are separate messages rather than one sentence bent around whichever applies.

        *Impossible.* A card action on the note being added has no card to reach -- the
        note has id 0 and no cards until the add goes through -- so the executor skips it
        with a log line and nothing runs it later. The stage is named so the user knows
        which action that is. It is not a reason to hold the definition back: a skipped
        action leaves nothing behind.

        *Unanswerable.* A search condition on the note being added is judged against that
        note, and a term that needs its cards or history (`is:due`) fails the definition
        there; see `unjudgeable_trigger_conditions`.

        *Forbidden.* An edit to another note, to a card that already exists, or to a file
        would outlive a cancelled add, so a definition doing any of those may not run as
        part of one. That is a warning and not a refusal because refusing protected
        nothing. The add hook runs such a definition after the trigger-only ones, under its
        own undo entry; the unfocus hook skips it while a note is being added; and the
        commit refuses anything but trigger-note changes from a definition that claims
        compatibility, whatever the stored `effects` say. All three read the flag, not the
        editor, so the behaviour is the same whether or not this dialog would have let the
        definition through -- including for every definition that arrived by migration and
        never passed through here at all.
        """
        if not self.wants_add_note():
            return []
        messages: list[str] = []
        card_paths = self.trigger_card_action_paths()
        if card_paths:
            messages.append(
                "The card action on "
                + "; ".join(card_paths)
                + " needs a card the note being added does not have yet, so it will not"
                " run for that note, and nothing runs it later."
            )
        on_add = bool((self.definition.get("triggers", {}) or {}).get("on_add"))
        if on_add or self.add_note_compatible():
            # Only a definition that runs for the note being added judges a search there;
            # an unfocus-only one that reaches beyond the note is skipped in the Add dialog.
            for path, error in self.unjudgeable_trigger_conditions():
                messages.append(
                    f"The search condition on {path} uses '{error.term}', which cannot be"
                    f" judged for a note that is not added yet ({error.reason}), so the"
                    " definition fails there while a note is being added."
                )
        if self.add_note_compatible():
            return messages
        if on_add:
            message = (
                "This definition runs when a note is added, and it reaches beyond the note"
                " being added — another note, a card that already exists, or a file. That"
                " work happens after the add rather than as part of it, with its own undo"
                " entry for the notes and cards it touches."
            )
        else:
            # An unfocus-only trigger is skipped in the Add dialog outright; the add trigger
            # is what would run the definition there.
            message = (
                "This definition runs when you leave a field while adding a note, and it"
                " reaches beyond the note being added — another note, a card that already"
                " exists, or a file — so it is skipped there. Turn on 'Run when adding a new"
                " note' to have it run when the note is added."
            )
        paths = self.incompatible_stage_paths()
        if paths:
            message += " Because of: " + "; ".join(paths) + "."
        messages.append(message)
        return messages

    def trigger_card_action_paths(self) -> list[str]:
        """The stages whose card actions reach the trigger note's own cards, by path.

        These are the actions that cannot run while the note is being added: a note with
        id 0 has no cards. Card actions on other notes' cards, and Edit Card stages (whose
        card came from a query or a loop, which cannot yield the new note's cards), are not
        listed -- they run.
        """
        paths: list[str] = []
        for stage in walk_stages(self.root_block(), include_disabled=False):
            if stage.get("type") != STAGE_EDIT_NOTE or not stage.get("card_actions"):
                continue
            if (stage.get("target") or {}).get("binding") == "trigger":
                paths.append(self.path_of(stage.get("guid", "")))
        return paths

    def unjudgeable_trigger_conditions(self) -> list[tuple[str, UnjudgeableSearch]]:
        """The trigger's search conditions that a note being added cannot answer, by path.

        A note being added has id 0, so its search conditions are judged against the note
        itself, and a term that needs cards, review history or an id fails the definition.
        Only a search that is already that way as written is listed: once a `{{...}}` part or
        a process chain is in it, what is searched is known only at run time. A condition
        checked only on sync, or on another binding's note, is never judged for the note
        being added.
        """
        found: list[tuple[str, UnjudgeableSearch]] = []
        for stage in walk_stages(self.root_block(), include_disabled=False):
            if stage.get("type") != STAGE_CONDITION:
                continue
            if stage.get("predicate_kind") != "note_query" or stage.get("only_on_sync"):
                continue
            target = stage.get("predicate_target")
            if isinstance(target, dict) and target.get("binding") not in (None, "", "trigger"):
                continue
            predicate = stage.get("predicate")
            if not isinstance(predicate, dict):
                continue
            text = (predicate.get("text") or "").strip()
            if (
                expression_is_code(predicate)
                or predicate.get("process_chain")
                or not text
                or "{{" in text
            ):
                continue
            try:
                # The same parentheses the executor judges it in.
                parse_search(f"({text})")
            except UnjudgeableSearch as error:
                found.append((self.path_of(stage.get("guid", "")), error))
            except UnsavedNoteSearchError:
                # A search Anki would refuse fails everywhere, not just while adding.
                continue
        return found

    def incompatible_stage_paths(self) -> list[str]:
        """The stages that keep this definition out of the add hook's trigger-only pile, by path.

        Everything here is something that would outlive a cancelled add: another note, a
        card that already exists, a file. A card action on the note being added is not one
        of them -- `trigger_card_action_paths` is where that is reported, and it is a
        different thing to say.

        A called definition is asked the same question through its stored `effects` rather
        than walked, so a caller is told that the call is the problem.
        """
        paths: list[str] = []
        for stage in walk_stages(self.root_block(), include_disabled=False):
            guid = stage.get("guid", "")
            stage_type = stage.get("type")
            if stage_type == STAGE_EDIT_CARD:
                paths.append(f"{self.path_of(guid)} — edits a card")
            elif stage_type == STAGE_WRITE_FILE:
                # A file is on disk whether or not the user goes through with the add.
                paths.append(f"{self.path_of(guid)} — writes a file")
            elif stage_type == STAGE_EDIT_NOTE:
                target = (stage.get("target") or {}).get("binding")
                # An action on the cards of the note being added is skipped, not refused,
                # so only one reaching another note's cards belongs here. An unresolved
                # target counts as another note's, as it does in the analyser.
                if stage.get("card_actions") and target != "trigger":
                    paths.append(f"{self.path_of(guid)} — has card actions")
                if target and target != "trigger":
                    paths.append(f"{self.path_of(guid)} — edits '{target}', not the trigger")
            elif stage_type == STAGE_CALL_DEFINITION and self._lookup is not None:
                callee = self._lookup(stage.get("definition_guid", ""))
                if callee is None:
                    continue
                effects = callee.get("effects", {}) or {}
                if (
                    effects.get("edits_other_notes")
                    or effects.get("edits_other_cards")
                    or effects.get("writes_files")
                ):
                    name = callee.get("definition_name") or stage.get("definition_guid", "")
                    paths.append(
                        f"{self.path_of(guid)} — calls '{name}', which reaches beyond"
                        " the trigger note"
                    )
        return paths

    def _describe(self, problem: SchemaProblem) -> str:
        if problem.stage_guid and self.stage(problem.stage_guid) is not None:
            return f"{self.path_of(problem.stage_guid)}: {problem.message}"
        return problem.message

    def to_definition(self) -> CopyDefinitionV2:
        """The definition as it should be persisted, with `effects` rewritten from the
        current analysis. `effects` is derived and never edited by hand (§4)."""
        result = deepcopy(self.definition)
        result["format_version"] = FORMAT_VERSION
        result["effects"] = dict(self.analysis.effects) or dict(EMPTY_EFFECTS)  # type: ignore[typeddict-item]
        return result


def _unique(problems: Iterable[SchemaProblem]) -> list[SchemaProblem]:
    """Drop repeats, keeping the first of each.

    A missing result name is reported twice -- once by the structural validator and once by
    the analyser as it tries to declare the binding -- which is right for a log and wrong
    for a row that has one line to spend on it.
    """
    seen: set[tuple] = set()
    unique: list[SchemaProblem] = []
    for problem in problems:
        key = (problem.message, problem.stage_guid, problem.stage_type)
        if key not in seen:
            seen.add(key)
            unique.append(problem)
    return unique


def stage_label(stage: Any) -> str:
    """What a stage row and a path segment call this stage: its own name if it has one."""
    if not isinstance(stage, dict):
        return "stage"
    name = (stage.get("name") or "").strip()
    if name:
        return name
    return STAGE_TYPE_LABELS.get(stage.get("type", ""), stage.get("type", "stage"))


def _blank_result_names(stage: Stage) -> None:
    """Blank the names the copy would collide on -- its own, and no deeper.

    A duplicate lands in the same block as its original, so a result name at that level is
    necessarily taken. Names inside a structural stage's body are not: the copy's body is
    its own scope, so a duplicated loop keeps a working body and only its own result needs
    renaming. References inside the body to a blanked outer name still resolve to the
    original stage, which is the value the user was duplicating.
    """
    if stage_result_name(stage) is not None:
        stage["result"] = ""  # type: ignore[typeddict-unknown-key]
    if stage.get("type") == STAGE_CALL_DEFINITION:
        for output in stage.get("outputs", []) or []:
            if isinstance(output, dict):
                output["result"] = ""


def _expression_summary(expression: Any, limit: int = 48) -> str:
    if not isinstance(expression, dict):
        return ""
    source = expression.get("code") if expression.get("mode") == "code" else expression.get("text")
    source = " ".join((source or "").split())
    if len(source) > limit:
        source = source[: limit - 1] + "…"
    return source


def stage_summary(stage: Any) -> str:
    """One line saying what this stage does, for the collapsed row.

    It reads the stage rather than the analysis on purpose: a row has to say something
    while the stage is still half-written, and "→ ?" is more use than a blank line.
    """
    if not isinstance(stage, dict):
        return ""
    stage_type = stage.get("type")
    target = (stage.get("target") or {}).get("binding") or "?"
    source = (stage.get("input") or {}).get("binding") or "?"
    result = stage.get("result") or "?"
    if stage_type == STAGE_VARIABLE:
        return f"{result} = {_expression_summary(stage.get('value')) or '?'}"
    if stage_type in (STAGE_NOTE_QUERY, STAGE_CARD_QUERY):
        what = "notes" if stage_type == STAGE_NOTE_QUERY else "cards"
        selection = stage.get("selection") or {}
        strategy = selection.get("strategy", "all")
        count = selection.get("count")
        how = "all" if strategy == "all" else f"{strategy} {count}" if count else strategy
        return f"{result} = {how} {what} matching {_expression_summary(stage.get('query')) or '?'}"
    if stage_type == STAGE_EDIT_NOTE:
        parts = []
        fields = [write.get("field") for write in stage.get("fields") or [] if isinstance(write, dict)]
        if fields:
            parts.append(", ".join(field for field in fields if field))
        tags = stage.get("tags") or {}
        if tags.get("add") or tags.get("remove"):
            parts.append("tags")
        if stage.get("card_actions"):
            parts.append("card actions")
        return f"{target}: {'; '.join(parts) if parts else 'nothing yet'}"
    if stage_type == STAGE_EDIT_CARD:
        count = len(stage.get("card_actions") or [])
        return f"{target}: {count} card action{'' if count == 1 else 's'}"
    if stage_type == STAGE_READ_FILE:
        return f"{result} = contents of {_expression_summary(stage.get('filename')) or '?'}"
    if stage_type == STAGE_WRITE_FILE:
        where = _expression_summary(stage.get("filename")) or "the code's own filenames"
        return f"write {where}{' (overwriting)' if stage.get('overwrite') else ''}"
    if stage_type == STAGE_LIST_VARIABLE:
        return f"{result}: an empty list of {stage.get('item_type', TEXT)}"
    if stage_type == STAGE_STORE:
        return f"append {_expression_summary(stage.get('value')) or '?'} to {target}"
    if stage_type == STAGE_FOR_EACH_NOTE:
        return f"each note in {source} as '{stage.get('item_binding') or 'note'}'"
    if stage_type == STAGE_FOR_EACH_CARD:
        return (
            f"each card in {source} as '{stage.get('item_binding') or 'card'}'"
            f", its note as '{stage.get('note_binding') or 'note'}'"
        )
    if stage_type == STAGE_REDUCE:
        return f"{result} = fold {source}"
    if stage_type == STAGE_CONDITION:
        return _expression_summary(stage.get("predicate")) or "no condition yet"
    if stage_type == STAGE_CALL_DEFINITION:
        outputs = [
            output.get("result")
            for output in stage.get("outputs") or []
            if isinstance(output, dict) and output.get("result")
        ]
        trigger = (stage.get("trigger") or {}).get("binding") or "?"
        tail = f", keeping {', '.join(outputs)}" if outputs else ""
        return f"run another definition on {trigger}{tail}"
    return ""
