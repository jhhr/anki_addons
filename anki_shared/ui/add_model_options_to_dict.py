from aqt import mw

from ..interpolate.interpolate_fields import CARD_VALUES, intr_format
from typing import Mapping, Optional, Union

from anki.consts import MODEL_CLOZE
from anki.models import NotetypeId


def get_max_cloze_ords() -> dict[int, int]:
    """
    The highest card ordinal in use for each cloze note type that has cards, by note type id.
    One query for every cloze note type, for a caller building menus for several of them;
    pass it to add_model_options_to_dict as `max_cloze_ords`.
    """
    cloze_ids = [model["id"] for model in mw.col.models.all() if model.get("type") == MODEL_CLOZE]
    if not cloze_ids:
        return {}
    assert mw.col.db is not None
    rows = mw.col.db.all(
        "SELECT notes.mid, MAX(cards.ord) FROM cards JOIN notes ON notes.id = cards.nid"
        f" WHERE notes.mid IN ({','.join(str(int(mid)) for mid in cloze_ids)})"
        " GROUP BY notes.mid"
    )
    return {int(mid): int(max_ord) for mid, max_ord in rows}


def add_model_options_to_dict(
    model_name: str,
    model_id: Union[NotetypeId, int],
    target_dict: dict,
    prefix: Optional[str] = None,
    max_cloze_ords: Optional[Mapping[int, int]] = None,
):
    """
    Add the field names and card values to the target_dict.
    :param model_name:
    :param model_id:
    :param target_dict: Could be the base SOURCE_NOTE_DATA_DICT or a sub-dict
    :param prefix: Optional prefix to add to the field names, used to separate
        source vs destination fields. Not necessary if PasteableTextEdit is used
        and the prefix is acquired from the menu group names.

    :param max_cloze_ords: get_max_cloze_ords(), for a caller adding several note types;
        without it a cloze note type queries its own highest ordinal
    :return: void, modifies target_dict in place
    """
    model = mw.col.models.get(NotetypeId(model_id))
    if model is None:
        # Can't add fields if model doesn't exist, this shouldn't happen but let's not throw
        # an error if it does
        print(f"Error: add_model_options_to_dict - Note type {model_name} not found")
        return
    fields_key = "Note fields"
    cards_key = "Card types"
    # Field replacement values will be 2-level menu
    model_key = model_name
    target_dict[model_key] = {fields_key: {}, cards_key: {}}
    cards_target = target_dict[model_key][cards_key]
    fields_target = target_dict[model_key][fields_key]
    card_templates = model["tmpls"]
    is_cloze = model.get("type") == MODEL_CLOZE

    if is_cloze:
        # Cloze notes have a single template but generate one card per cloze ordinal.
        # Query the highest ordinal that exists for notes of this type so the menu
        # shows every cloze number that is actually in use (minimum: "Cloze 1").
        if max_cloze_ords is not None:
            max_ord = max_cloze_ords.get(int(model_id))
        else:
            assert mw.col.db is not None
            max_ord = mw.col.db.scalar(
                "SELECT MAX(ord) FROM cards WHERE nid IN (SELECT id FROM notes WHERE mid = ?)",
                model_id,
            )
        max_ord = max_ord if max_ord is not None else 0
        template_name = card_templates[0]["name"] if card_templates else "Cloze"
        for cloze_num in range(1, max_ord + 2):
            cloze_key = f"{template_name} {cloze_num}"
            cards_target[cloze_key] = {}
            for card_value in CARD_VALUES:
                value = f"{cloze_key}{card_value}"
                if prefix:
                    value = f"{prefix}{value}"
                cards_target[cloze_key][card_value] = intr_format(value)
    else:
        # Card replacement values will be 3-level menu, model: card_type: card_value
        for card_template in card_templates:
            cards_target[card_template["name"]] = {}
            for card_value in CARD_VALUES:
                value = f'{card_template["name"]}{card_value}'
                if prefix:
                    value = f"{prefix}{value}"
                cards_target[card_template["name"]][card_value] = intr_format(value)

    for field_name in mw.col.models.field_names(model):
        field_value = field_name if prefix is None else f"{prefix}{field_name}"
        fields_target[field_name] = intr_format(field_value)
