"""Right-click menu options for the destination note in Across Notes mode.

``BASE_NOTE_MENU_DICT`` lives with the interpolation code because it describes
values any consumer of that code can interpolate.  A *destination* note, by
contrast, only exists because a copy definition has two ends, so the menu
entries for it belong to this addon rather than to the interpolation layer.
"""

from ..configuration import COPY_MODE_WITHIN_NOTE, CopyModeType
from ..shared.interpolate.interpolate_fields import (
    BASE_NOTE_MENU_DICT,
    DESTINATION_PREFIX,
    NOTE_CARD_COUNT,
    NOTE_HAS_TAG,
    NOTE_ID,
    NOTE_TAGS,
    NOTE_TYPE_ID,
    intr_format,
)

DESTINATION_NOTE_DATA_KEY = "__Destination_Note_Data"

# For across copy mode only, used for the destination note
DESTINATION_NOTE_MENU_DICT = {
    # The note being used to query
    DESTINATION_NOTE_DATA_KEY: {
        "Destination Note Type ID (mid:)": intr_format(f"{DESTINATION_PREFIX}{NOTE_TYPE_ID}"),
        "Destination Note ID (nid:)": intr_format(f"{DESTINATION_PREFIX}{NOTE_ID}"),
        "Destination note all tags": intr_format(f"{DESTINATION_PREFIX}{NOTE_TAGS}"),
        "Destination note has tag": intr_format(f"{DESTINATION_PREFIX}{NOTE_HAS_TAG}"),
        "Destination No. different card types": intr_format(
            f"{DESTINATION_PREFIX}{NOTE_CARD_COUNT}"
        ),
    },
}


def get_new_base_dict(copy_mode: CopyModeType) -> dict:
    if copy_mode == COPY_MODE_WITHIN_NOTE:
        return DESTINATION_NOTE_MENU_DICT.copy()
    return DESTINATION_NOTE_MENU_DICT | BASE_NOTE_MENU_DICT
