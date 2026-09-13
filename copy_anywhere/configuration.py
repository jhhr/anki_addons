import html
import uuid
from typing import Literal, Optional, Sequence, TypedDict, Union

from aqt import mw
from typing_extensions import TypeGuard

from .shared.interpolate.interpolate_fields import (
    TARGET_NOTES_COUNT,
    QUERY_NOTE_INDEX,
    intr_format,
)
from .logic.definition_schema import Effects, is_format_2, read_effects
from .shared.jp_text_processing.kana.kana_highlight import FuriReconstruct
from .shared.utils.logger import LogLevel

tag = mw.addonManager.addonFromModule(__name__)


def load_config():
    return mw.addonManager.getConfig(tag)


def save_config(data):
    mw.addonManager.writeConfig(tag, data)


# def run_on_configuration_change(function):
#     mw.addonManager.setConfigUpdatedAction(__name__, lambda *_: function())


KANJIUM_TO_JAVDEJONG_PROCESS = "Pitch accent conversion: Kanjium to Javdejong"


class KanjiumToJavdejongProcess(TypedDict):
    guid: str
    name: str
    delimiter: str


REGEX_PROCESS = "Regex replace"


class RegexProcess(TypedDict):
    guid: str
    name: str
    regex: str
    replacement: str
    # Separators used when interpolation uses multiple notes
    regex_separator: str
    replacement_separator: str
    flags: Optional[str]
    use_all_notes: bool


def get_regex_process_label(regex_process):
    regex = regex_process["regex"]
    if len(regex) > 40:
        regex = regex[:20] + "..."
    return f"{REGEX_PROCESS}: <code>{html.escape(regex)}</code>"


FONTS_CHECK_PROCESS = "Fonts check"


class FontsCheckProcess(TypedDict):
    guid: str
    name: str
    fonts_dict_file: str
    limit_to_fonts: Optional[list[str]]
    character_limit_regex: Optional[str]


def get_fonts_check_process_label(fonts_check_process):
    fonts_limit = fonts_check_process.get("limit_to_fonts", None)
    if fonts_limit:
        fonts_limit = f", (limit {len(fonts_limit)} fonts)"
    else:
        fonts_limit = ""
    return f"{FONTS_CHECK_PROCESS}: {fonts_check_process['fonts_dict_file']}{fonts_limit}"


KANA_HIGHLIGHT_PROCESS = "Kana Highlight"


class KanaHighlightProcess(TypedDict):
    guid: str
    name: str
    kanji_field: str
    return_type: FuriReconstruct
    wrap_readings_in_tags: bool
    merge_consecutive_tags: bool
    onyomi_to_katakana: bool


WORD_HIGHLIGHT_PROCESS = "Word Highlight"


class WordHighlightProcess(TypedDict):
    guid: str
    name: str
    word_field: str


AnyProcess = Union[KanjiumToJavdejongProcess, RegexProcess, FontsCheckProcess, KanaHighlightProcess]


def is_kana_highlight_process(
    process: Union[dict, AnyProcess],
) -> TypeGuard[KanaHighlightProcess]:
    return process.get("name") == KANA_HIGHLIGHT_PROCESS


def is_word_highlight_process(
    process: Union[dict, AnyProcess],
) -> TypeGuard[WordHighlightProcess]:
    return process.get("name") == WORD_HIGHLIGHT_PROCESS


def is_regex_process(process: Union[dict, AnyProcess]) -> TypeGuard[RegexProcess]:
    return process.get("name") == REGEX_PROCESS


def is_fonts_check_process(
    process: Union[dict, AnyProcess],
) -> TypeGuard[FontsCheckProcess]:
    return process.get("name") == FONTS_CHECK_PROCESS


def is_kanjium_to_javdejong_process(
    process: Union[dict, AnyProcess],
) -> TypeGuard[KanjiumToJavdejongProcess]:
    return process.get("name") == KANJIUM_TO_JAVDEJONG_PROCESS


ALL_FIELD_TO_FIELD_PROCESS_NAMES = [
    KANJIUM_TO_JAVDEJONG_PROCESS,
    REGEX_PROCESS,
    FONTS_CHECK_PROCESS,
    KANA_HIGHLIGHT_PROCESS,
    WORD_HIGHLIGHT_PROCESS,
]
ALL_FIELD_TO_VARIABLE_PROCESS_NAMES = [
    REGEX_PROCESS,
    KANA_HIGHLIGHT_PROCESS,
    WORD_HIGHLIGHT_PROCESS,
]

NEW_PROCESS_DEFAULTS: dict[str, AnyProcess] = {
    KANJIUM_TO_JAVDEJONG_PROCESS: KanjiumToJavdejongProcess(
        name=KANJIUM_TO_JAVDEJONG_PROCESS,
        delimiter="・",
    ),
    REGEX_PROCESS: RegexProcess(
        name=REGEX_PROCESS,
        regex="",
        replacement="",
        flags="",
    ),
    FONTS_CHECK_PROCESS: FontsCheckProcess(
        name=FONTS_CHECK_PROCESS,
        fonts_dict_file="",
        limit_to_fonts=[],
        character_limit_regex="",
    ),
    KANA_HIGHLIGHT_PROCESS: KanaHighlightProcess(
        name=KANA_HIGHLIGHT_PROCESS,
        kanji_field="",
        return_type="kana_only",
        wrap_readings_in_tags=True,
        merge_consecutive_tags=True,
    ),
    WORD_HIGHLIGHT_PROCESS: WordHighlightProcess(
        name=WORD_HIGHLIGHT_PROCESS,
        word_field="",
    ),
}

MULTIPLE_ALLOWED_PROCESS_NAMES = [
    REGEX_PROCESS,
]

FlagValueType = Literal[0, 1, 2, 3, 4, 5, 6, 7]

CARD_TYPE_SEPARATOR = "<::>"


# Card action used by GUI or code
class CardActionDict(TypedDict):
    card_type_name: str
    change_deck: Optional[Union[str, int]]
    set_flag: Optional[FlagValueType]
    suspend: Optional[bool]
    bury: Optional[bool]
    set_desired_retention: Optional[Union[float, int, str]]


# Card action saved to definition, contains either a GUI set
# action or code that should return a CardActionDict
class CardAction(CardActionDict):
    guid: str
    action_code: Optional[str]
    use_code: bool


class CopyFieldToField(TypedDict):
    guid: str
    copy_into_note_field: str
    copy_from_text: str
    copy_as_code: str
    use_code: bool
    copy_if_empty: bool
    copy_on_unfocus_when_edit: bool
    copy_on_unfocus_when_add: bool
    copy_on_unfocus_trigger_field: str
    process_chain: Sequence[AnyProcess]


class CopyFieldToFile(TypedDict):
    guid: str
    copy_into_filename: str
    copy_from_text: str
    copy_as_code: str
    use_code: bool
    copy_if_empty: bool
    copy_on_unfocus_when_edit: bool
    copy_on_unfocus_when_add: bool
    copy_on_unfocus_trigger_field: str
    process_chain: Sequence[AnyProcess]


def split_tags(tags: Optional[str]) -> list[str]:
    """Split a stored tag list into tag names, dropping the empty ones.

    The stored form is the quoted-and-comma-joined shape the tag editor writes, and the
    common value is "" -- most definitions add no tags at all. A bare `.split('", "')` turns
    that into `[""]`, which then adds an empty tag to every destination note and marks it
    modified whether or not anything was copied, inflating the processed counts, the
    copied-into list and the undo entry.
    """
    if not tags:
        return []
    return [tag for tag in tags.strip('""').split('", "') if tag]


def get_field_to_field_unfocus_trigger_fields(
    field_to_field: CopyFieldToField, modifies_other_notes: bool
) -> list[str]:
    # A bare split turns an unset trigger into `[""]`, which is truthy and would keep the
    # destination-field fallback below from ever applying.
    trigger_value = field_to_field.get("copy_on_unfocus_trigger_field", "")
    trigger_fields = [name for name in trigger_value.strip('""').split('", "') if name]
    if modifies_other_notes:
        # source to destination mode is triggered by a field change in the trigger note
        # while the destination field is a different field in another note
        return trigger_fields
    else:
        # destination to sources mode or within note mode the destination and trigger fields
        # are in the same note
        return trigger_fields or [field_to_field.get("copy_into_note_field", "")]


def get_triggered_field_to_field_defs_for_field(
    field_to_field_defs: list[CopyFieldToField],
    field_name: str,
    modifies_other_notes: bool,
) -> list[CopyFieldToField]:
    """
    Get every field-to-field definition that field_name triggers in this mode, in config order.
    """
    return [
        field_def
        for field_def in field_to_field_defs
        if field_name in get_field_to_field_unfocus_trigger_fields(field_def, modifies_other_notes)
    ]


class CopyFieldToVariable(TypedDict):
    guid: str
    copy_into_variable: str
    copy_from_text: str
    copy_as_code: str
    use_code: bool
    process_chain: Sequence[AnyProcess]


CopyModeType = Literal["Within note", "Across notes"]
COPY_MODE_WITHIN_NOTE: CopyModeType = "Within note"
COPY_MODE_ACROSS_NOTES: CopyModeType = "Across notes"

DirectionType = Literal["Destination to sources", "Source to destinations"]
DIRECTION_DESTINATION_TO_SOURCES: DirectionType = "Destination to sources"
DIRECTION_SOURCE_TO_DESTINATIONS: DirectionType = "Source to destinations"

SELECT_CARD_BY_VALUES = ("None", "Random", "Least_reps")
SelectCardByType = Literal["None", "Random", "Least_reps"]


class CopyDefinition(TypedDict):
    guid: str
    definition_name: str
    copy_on_sync: bool
    copy_on_add: bool
    copy_on_review: bool
    copy_mode: CopyModeType
    copy_into_note_types: str
    across_mode_direction: Optional[DirectionType]
    field_to_field_defs: list[CopyFieldToField]
    field_to_file_defs: list[CopyFieldToFile]
    field_to_variable_defs: list[CopyFieldToVariable]
    card_actions: Optional[list[CardAction]]
    add_tags: Optional[str]
    remove_tags: Optional[str]
    only_copy_into_decks: Optional[str]
    include_subdecks: Optional[bool]
    copy_condition_query: Optional[str]
    condition_only_on_sync: Optional[bool]
    copy_from_cards_query: Optional[str]
    sort_by_field: Optional[str]
    select_card_by: SelectCardByType
    select_card_count: Optional[str]
    select_card_separator: Optional[str]
    show_error_if_none_found: Optional[bool]
    run_also_if_no_sources_found: Optional[bool]


def compare_versions(version1: str, version2: str) -> int:
    """
    Compare two version strings.
    Returns:
        -1 if version1 < version2
         0 if version1 == version2
         1 if version1 > version2
    """
    v1_parts = list(map(int, version1.split(".")))
    v2_parts = list(map(int, version2.split(".")))

    # Pad the shorter list with zeros
    while len(v1_parts) < len(v2_parts):
        v1_parts.append(0)
    while len(v2_parts) < len(v1_parts):
        v2_parts.append(0)

    return (v1_parts > v2_parts) - (v1_parts < v2_parts)


def migrate_config():
    """
    Migrates old copy definitions to newer formats going through all
    version migrations.
    """
    config = Config()
    config.load()
    if compare_versions(config.version, "0.2.0") < 0:
        updated_definitions = []
        for definition in config.copy_definitions:
            if "guid" not in definition:
                definition["guid"] = str(uuid.uuid4())
            new_field_to_fields = []
            for field_to_field in definition.get("field_to_field_defs", []):
                if "guid" not in field_to_field:
                    field_to_field["guid"] = str(uuid.uuid4())
                new_processes = []
                for process in field_to_field.get("process_chain", []):
                    if "guid" not in process:
                        process["guid"] = str(uuid.uuid4())
                    new_processes.append(process)
                field_to_field["process_chain"] = new_processes
                new_field_to_fields.append(field_to_field)
            definition["field_to_field_defs"] = new_field_to_fields
            new_field_to_files = []
            for field_to_file in definition.get("field_to_file_defs", []):
                if "guid" not in field_to_file:
                    field_to_file["guid"] = str(uuid.uuid4())
                new_processes = []
                for process in field_to_file.get("process_chain", []):
                    if "guid" not in process:
                        process["guid"] = str(uuid.uuid4())
                    new_processes.append(process)
                field_to_file["process_chain"] = new_processes
                new_field_to_files.append(field_to_file)
            definition["field_to_file_defs"] = new_field_to_files
            new_field_to_variables = []
            for field_to_variable in definition.get("field_to_variable_defs", []):
                if "guid" not in field_to_variable:
                    field_to_variable["guid"] = str(uuid.uuid4())
                new_processes = []
                for process in field_to_variable.get("process_chain", []):
                    if "guid" not in process:
                        process["guid"] = str(uuid.uuid4())
                    new_processes.append(process)
                field_to_variable["process_chain"] = new_processes
                new_field_to_variables.append(field_to_variable)
            definition["field_to_variable_defs"] = new_field_to_variables
            updated_definitions.append(definition)
        config.data["copy_definitions"] = updated_definitions

    # Finished, set the version to latest
    config.data["version"] = "0.2.0"
    config.save()


def get_variables_dict_from_variable_defs(
    copy_mode: CopyModeType,
    variable_defs: Union[Sequence[CopyFieldToVariable], Sequence[str]],
) -> dict[str, str]:
    variable_menu_dict: dict[str, str] = {}
    # Always include the target notes count variable as it will be generated
    # in any across notes mode copy operation
    if copy_mode == COPY_MODE_ACROSS_NOTES:
        variable_menu_dict[TARGET_NOTES_COUNT] = intr_format(TARGET_NOTES_COUNT)
        variable_menu_dict[QUERY_NOTE_INDEX] = intr_format(QUERY_NOTE_INDEX)
    for variable_def in variable_defs:
        if isinstance(variable_def, str):
            # If the variable definition is just a string, use it directly
            variable_name = variable_def
        else:
            # Otherwise, extract the variable name from the definition
            variable_name = variable_def["copy_into_variable"]
        if variable_name is not None:
            variable_menu_dict[variable_name] = intr_format(variable_name)
    return variable_menu_dict


# --------------------------------------------------------------------------------------
# Trigger settings, in whichever format a definition is stored
# --------------------------------------------------------------------------------------
#
# Format 1 keeps these as flat keys with quoted, comma-joined name lists; format 2 keeps
# them as JSON arrays under `triggers` (§4). Everything that decides whether a definition
# applies to a note -- the hooks, the picker, the bulk operation -- goes through these, so
# a config holding both formats behaves the same either way.


def definition_note_type_names(copy_definition: Union[CopyDefinition, dict]) -> list[str]:
    """The note type names a definition triggers on."""
    if is_format_2(copy_definition):
        return list((copy_definition.get("triggers") or {}).get("note_types") or [])
    stored = copy_definition.get("copy_into_note_types") or ""
    if not stored or stored == "-":
        return []
    # Split by comma and remove the first wrapping " but keeping the last one
    return [name for name in stored.strip('""').split('", "') if name]


def definition_note_types_label(copy_definition: Union[CopyDefinition, dict]) -> Optional[str]:
    """The note type names as the error messages have always spelled them, or None."""
    if is_format_2(copy_definition):
        names = (copy_definition.get("triggers") or {}).get("note_types")
        return '", "'.join(names) if names else None
    return copy_definition.get("copy_into_note_types", None)


def definition_deck_names(copy_definition: Union[CopyDefinition, dict]) -> list[str]:
    """The decks a definition is limited to, empty meaning no limit."""
    if is_format_2(copy_definition):
        return list((copy_definition.get("triggers") or {}).get("deck_names") or [])
    stored = copy_definition.get("only_copy_into_decks") or ""
    if not stored or stored == "-":
        return []
    return [name for name in stored.strip('""').split('", "') if name]


def definition_trigger_flag(
    copy_definition: Union[CopyDefinition, dict], format_2_key: str, format_1_key: str
) -> bool:
    if is_format_2(copy_definition):
        return bool((copy_definition.get("triggers") or {}).get(format_2_key, False))
    return bool(copy_definition.get(format_1_key, False))


def definition_include_subdecks(copy_definition: Union[CopyDefinition, dict]) -> bool:
    return definition_trigger_flag(copy_definition, "include_subdecks", "include_subdecks")


def definition_runs_on_add(copy_definition: Union[CopyDefinition, dict]) -> bool:
    return definition_trigger_flag(copy_definition, "on_add", "copy_on_add")


def definition_runs_on_sync(copy_definition: Union[CopyDefinition, dict]) -> bool:
    return definition_trigger_flag(copy_definition, "on_sync", "copy_on_sync")


def definition_runs_on_review(copy_definition: Union[CopyDefinition, dict]) -> bool:
    return definition_trigger_flag(copy_definition, "on_review", "copy_on_review")


def definition_unfocus_fields(
    copy_definition: Union[CopyDefinition, dict], is_new_note: bool
) -> list[str]:
    """The editor fields whose unfocus runs a format-2 definition, in whole (§8).

    Format 1 has no equivalent: there, unfocus is stored per field write and runs only the
    writes that field triggers, which is why that path keeps its own per-write gating and
    this returns nothing for it.
    """
    if not is_format_2(copy_definition):
        return []
    unfocus = (copy_definition.get("triggers") or {}).get("on_unfocus") or {}
    return list(unfocus.get("add_fields" if is_new_note else "edit_fields") or [])


def definition_effects(copy_definition: Union[CopyDefinition, dict]) -> Effects:
    """What this definition does to the collection, in whichever format it is stored.

    A format-2 definition carries its `effects` object, computed by the flow analyser
    transitively through the definitions it calls (§4, §6). A format-1 definition has no
    such object, and inspecting its mode and direction is the exact answer for it, so that
    is what the two predicates below still do.
    """
    if is_format_2(copy_definition):
        return read_effects(copy_definition)
    modifies_other = definition_modifies_other_notes(copy_definition)
    return {
        "edits_trigger": definition_modifies_trigger_note(copy_definition),
        "edits_other_notes": modifies_other,
        "edits_cards": bool(copy_definition.get("card_actions")),
        "reads_files": False,
        "writes_files": bool(copy_definition.get("field_to_file_defs")),
        "queries_collection": copy_definition.get("copy_mode") == COPY_MODE_ACROSS_NOTES,
        "calls_definitions": False,
        "add_note_compatible": not modifies_other,
    }


def definition_is_add_note_compatible(copy_definition: Union[CopyDefinition, dict]) -> bool:
    """Whether this definition can run against a note that has not been added yet.

    A note being added has id 0 and no cards, so a definition that writes to any other note
    or to any card has nothing to write to. The add hook does not drop those definitions --
    it defers them until the note exists and runs them under their own undo entry -- but it
    is this flag that decides which pile a definition goes in.
    """
    return bool(definition_effects(copy_definition).get("add_note_compatible", False))


def definition_modifies_trigger_note(
    copy_definition: CopyDefinition,
) -> bool:
    if is_format_2(copy_definition):
        return bool(definition_effects(copy_definition).get("edits_trigger", False))
    targets_trigger_note = (
        copy_definition.get("copy_mode", None) == COPY_MODE_WITHIN_NOTE
        or copy_definition.get("across_mode_direction", None) == DIRECTION_DESTINATION_TO_SOURCES
    )
    # definition might only save stuff to files
    has_field_to_field_defs = len(copy_definition.get("field_to_field_defs", [])) > 0
    return targets_trigger_note and has_field_to_field_defs


def definition_modifies_other_notes(
    copy_definition: CopyDefinition,
) -> bool:
    if is_format_2(copy_definition):
        return bool(definition_effects(copy_definition).get("edits_other_notes", False))
    # Destination to sources is Across notes too, but its only destination is the trigger note
    targets_other_notes = (
        copy_definition.get("copy_mode", None) == COPY_MODE_ACROSS_NOTES
        and copy_definition.get("across_mode_direction", None) == DIRECTION_SOURCE_TO_DESTINATIONS
    )
    # definition might only save stuff to files
    has_field_to_field_defs = len(copy_definition.get("field_to_field_defs", [])) > 0
    # Tagging the found notes edits them as much as a field copy does, so a tags-only
    # definition still has to wait for the note to exist and be written like one
    has_tag_edits = bool(
        (copy_definition.get("add_tags") or "").strip()
        or (copy_definition.get("remove_tags") or "").strip()
    )
    return targets_other_notes and (has_field_to_field_defs or has_tag_edits)


class Config:
    def load(self):
        self.data = load_config()

    def save(self):
        save_config(self.data)

    @property
    def version(self) -> str:
        return self.data.get("version", "0.1.0")

    @property
    def log_level(self) -> LogLevel:
        return self.data.get("log_level", "error")

    @property
    def copy_fields_shortcut(self):
        return self.data["copy_fields_shortcut"]

    @copy_fields_shortcut.setter
    def copy_fields_shortcut(self, value):
        self.data["copy_fields_shortcut"] = value
        self.save()

    @property
    def copy_definitions(self):
        return self.data["copy_definitions"] or []

    def get_definition_by_name(self, name) -> Union[CopyDefinition, None]:
        # find the definition in the list of definitions
        for definition in self.data["copy_definitions"]:
            if definition["definition_name"] == name:
                return definition
        return None

    def add_definition(self, definition: CopyDefinition):
        if "guid" not in definition:
            definition["guid"] = str(uuid.uuid4())
        self.data["copy_definitions"].append(definition)
        self.save()

    def insert_definition_at_index(self, index: int, definition: CopyDefinition):
        """Insert a definition at a specific index in the list"""
        if "guid" not in definition:
            definition["guid"] = str(uuid.uuid4())
        self.data["copy_definitions"].insert(index, definition)
        self.save()

    def remove_definition_by_name(self, name: str):
        definition = self.get_definition_by_name(name)
        if definition is None:
            return
        if definition:
            self.data["copy_definitions"].remove(definition)
            self.save()

    def remove_definition_by_index(self, index: int):
        self.data["copy_definitions"].pop(index)
        self.save()

    def remove_definition_by_guid(self, guid: str):
        for index, definition in enumerate(self.data["copy_definitions"]):
            if definition["guid"] == guid:
                self.remove_definition_by_index(index)
                return

    def update_definition_by_name(
        self, name: str, new_definition: CopyDefinition
    ) -> Union[int, None]:
        for index, definition in enumerate(self.data["copy_definitions"]):
            if definition["definition_name"] == name:
                self.update_definition_by_index(index, new_definition)
                return index
        return None

    def update_definition_by_index(self, index: int, definition: CopyDefinition):
        self.data["copy_definitions"][index] = definition
        self.save()

    def update_definition_by_guid(
        self, guid: str, new_definition: CopyDefinition
    ) -> Union[int, None]:
        for index, definition in enumerate(self.data["copy_definitions"]):
            if definition["guid"] == guid:
                self.update_definition_by_index(index, new_definition)
                return index
        return None

    def reorder_definition(self, source_guid: str, target_guid: str, drop_below: bool):
        """Reorder definitions by moving source before or after target"""
        # Find source and target indices
        source_index = None
        target_index = None

        for i, definition in enumerate(self.data["copy_definitions"]):
            if definition["guid"] == source_guid:
                source_index = i
            elif definition["guid"] == target_guid:
                target_index = i

        if source_index is None or target_index is None:
            return

        # Remove the source definition
        source_definition = self.data["copy_definitions"].pop(source_index)

        # Adjust target index if source was before target
        if source_index < target_index:
            target_index -= 1

        # Insert at the new position
        new_index = target_index + 1 if drop_below else target_index
        self.data["copy_definitions"].insert(new_index, source_definition)

        self.save()
