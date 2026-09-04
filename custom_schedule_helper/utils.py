import json
import math
import base64
import re
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import List, Union, Optional, TypedDict, Literal

from anki.cards import Card
from anki.stats import (
    REVLOG_LRN,
    REVLOG_REV,
    REVLOG_RELRN,
    REVLOG_CRAM,
)
from anki.stats_pb2 import CardStatsResponse
from aqt import mw
from aqt.utils import showWarning

SCHEDULER_NAME = "Custom Scheduler"
CUR_SCHEDULER_VERSION = (1, 0, 0)
CUR_SCHEDULER_VERSION_STR = ".".join(map(str, CUR_SCHEDULER_VERSION))
GLOBAL_DECK_CONFIG_NAME = "global config for Custom Scheduler"
DECK_NAME_PARAM = "deckName"
DAYS_UPPER_PARAM = "daysUpper"
MIN_AGAIN_MULT_PARAM = "minAgainMult"

ALL_PARAMS = [DAYS_UPPER_PARAM, MIN_AGAIN_MULT_PARAM]


def get_version(custom_scheduler):
    str_matches = re.findall(
        rf"// {SCHEDULER_NAME} v(\d+)\.(\d+)\.(\d+)", custom_scheduler
    )
    try:
        version = tuple(map(int, str_matches[0]))
    except IndexError:
        mw.taskman.run_on_main(
            lambda: showWarning(
                f"Please check whether the version of {SCHEDULER_NAME} matches {CUR_SCHEDULER_VERSION_STR}"
            )
        )
        return
    if version != CUR_SCHEDULER_VERSION:
        mw.taskman.run_on_main(
            lambda: showWarning(
                f"Please check whether the version of {SCHEDULER_NAME} matches {CUR_SCHEDULER_VERSION_STR}"
            )
        )
        return
    return version


class CustomSchedulerNotFoundError(BaseException):
    def __init__(self):
        self.message = (
            "Paste the custom_scheduler.js into custom scheduling in the deck config."
        )


def check_custom_scheduler(all_config):
    if "cardStateCustomizer" not in all_config:
        raise CustomSchedulerNotFoundError()
    custom_scheduler = all_config["cardStateCustomizer"]
    if f"// {SCHEDULER_NAME}" not in custom_scheduler:
        raise CustomSchedulerNotFoundError()
    get_version(custom_scheduler)
    return custom_scheduler


def _remove_comment_line(custom_scheduler):
    not_comment_line = "\n".join(
        [re.sub("^ *//..*$", "", _) for _ in custom_scheduler.split("\n")]
    )
    return not_comment_line


# Add base exception for all deck param errors
class DeckParamError(BaseException):
    def __init__(self):
        super().__init__(self.message)


class MalFormedDeckParamsError(DeckParamError):
    def __init__(self):
        self.message = f"""{SCHEDULER_NAME} ERROR: The deckParams are not properly formatted.
        Please check your deckParams in custom scheduler.
        """


class GlobalDeckParamsMissingError(DeckParamError):
    def __init__(self):
        self.message = f"""{SCHEDULER_NAME} ERROR: The global deckParams are not defined.
        Please check your deckParams in custom scheduler contain params for deckName="{GLOBAL_DECK_CONFIG_NAME}".
        """


class GlobalDeckSomeParamsMissingError(DeckParamError):
    def __init__(self):
        self.message = f"""{SCHEDULER_NAME} ERROR: The global deckParams or {DAYS_UPPER_PARAM} are not defined.
        Please check your deckParams in custom scheduler.
        """


class DeckNameParamMissingError(DeckParamError):
    def __init__(self):
        self.message = f"""{SCHEDULER_NAME} ERROR: The deckName parameter is missing.
        Please check that each deckParam in custom scheduler contains a deckName="..." parameter.
        """


def get_deck_parameters(custom_scheduler):
    custom_scheduler = _remove_comment_line(custom_scheduler)

    params_array_pat = r"const deckParams = (\[[\s\S]*?\]);"
    hanging_comma_pat = r",(\s*?(?:\}|\]))"
    deck_params_str = re.search(params_array_pat, custom_scheduler).group(1)
    deck_params_str = re.sub(hanging_comma_pat, r"\1", deck_params_str)
    try:
        deck_parameters = json.loads(deck_params_str)
    except json.JSONDecodeError:
        raise MalFormedDeckParamsError()

    global_config = None
    for deck_param in deck_parameters:
        if deck_param[DECK_NAME_PARAM] == GLOBAL_DECK_CONFIG_NAME:
            global_config = deck_param
            if not all(param in global_config for param in ALL_PARAMS):
                raise GlobalDeckSomeParamsMissingError()
        else:
            if DECK_NAME_PARAM not in deck_param:
                raise DeckNameParamMissingError()
    if global_config is None:
        raise GlobalDeckParamsMissingError()

    # Fill in missing parameters with global config
    for deck_param in deck_parameters:
        for param in ALL_PARAMS:
            if param not in deck_param:
                deck_param[param] = global_config[param]

    # Sort the deck parameters by deck name, so that parent decks are sorted before sub-decks
    deck_parameters = sorted(deck_parameters, key=lambda x: x[DECK_NAME_PARAM])
    deck_parameters = OrderedDict(
        {deck_param[DECK_NAME_PARAM]: deck_param for deck_param in deck_parameters}
    )

    return deck_parameters


def get_current_deck_parameter(deckname, deck_parameters):
    """
    Get the deck parameters for the current deck.
    Will default to global deck parameters and then override with deck specific parameters.
    Additionally parent deck params will be applied first and then overridden by sub-deck params.
    """
    deck_params = deck_parameters[GLOBAL_DECK_CONFIG_NAME].copy()
    for name, params in deck_parameters.items():
        if deckname.startswith(name):
            deck_params.update(params)
            # continue looping to override with more specific deck params
    return deck_params


def get_skip_decks(custom_scheduler):
    pattern = r"[const ]?skipDecks ?= ?(.*);"
    str_matches = re.findall(pattern, custom_scheduler)
    try:
        names = str_matches[0].split(", ")
    except IndexError:
        mw.taskman.run_on_main(
            lambda: showWarning(
                "Skip decks are not found in the custom scheduler. Please always include it, even if empty"
            )
        )
        return []
    deck_names = list(map(lambda x: x.strip(']["'), names))
    non_empty_deck_names = list(filter(lambda x: x != "", deck_names))

    decks = []
    missing_decks = []
    for skip_deck_name in non_empty_deck_names:
        deck = mw.col.decks.by_name(skip_deck_name)
        if deck is not None:
            decks.append(deck)
        else:
            missing_decks.append(skip_deck_name)
    if len(missing_decks) > 0:
        mw.taskman.run_on_main(
            lambda: showWarning(
                f"Decks {missing_decks} are not found in the collection. Check the deck names."
            )
        )
    return decks


def RepresentsInt(s):
    try:
        return int(s)
    except ValueError:
        return None


def reset_ivl_and_due(cid: int, revlogs: List[CardStatsResponse.StatsRevlogEntry]):
    card = mw.col.get_card(cid)
    card.ivl = int(revlogs[0].interval / 86400)
    due = (
        math.ceil(
            (revlogs[0].time + revlogs[0].interval - mw.col.sched.day_cutoff) / 86400
        )
        + mw.col.sched.today
    )
    if card.odid:
        card.odue = max(due, 1)
    else:
        card.due = due
    mw.col.update_card(card)


def filter_revlogs(
    revlogs: List[CardStatsResponse.StatsRevlogEntry],
) -> List[CardStatsResponse.StatsRevlogEntry]:
    return list(filter(lambda x: x.review_kind != REVLOG_CRAM or x.ease != 0, revlogs))


def get_last_review_date(card: Card):
    revlogs = mw.col.card_stats_data(card.id).revlog
    try:
        last_revlog = list(filter(lambda x: x.button_chosen >= 1, revlogs))[0]
        last_review_date = (
            math.ceil((last_revlog.time - mw.col.sched.day_cutoff) / 86400)
            + mw.col.sched.today
        )
    except IndexError:
        due = card.odue if card.odid else card.due
        last_review_date = due - card.ivl
    return last_review_date


def update_card_due_ivl(card: Card, new_ivl: int):
    # Don't change ivl, it leads to ever-increasing ivl when reschedule is applied repeatedly
    # card.ivl = new_ivl
    last_review_date = get_last_review_date(card)
    if card.odid:
        card.odue = max(last_review_date + new_ivl, 1)
    else:
        card.due = last_review_date + new_ivl
    return card


def has_again(revlogs: List[CardStatsResponse.StatsRevlogEntry]):
    for r in revlogs:
        if r.button_chosen == 1:
            return True
    return False


def has_manual_reset(revlogs: List[CardStatsResponse.StatsRevlogEntry]):
    last_kind = None
    for r in revlogs:
        if r.button_chosen == 0:
            return True
        if (
            last_kind is not None
            and last_kind in (REVLOG_REV, REVLOG_RELRN)
            and r.review_kind == REVLOG_LRN
        ):
            return True
        last_kind = r.review_kind
    return False


def get_fuzz_range(interval, elapsed_days):
    min_ivl = max(2, int(round(interval * 0.95 - 1)))
    max_ivl = int(round(interval * 1.05 + 1))
    if interval > elapsed_days:
        min_ivl = max(min_ivl, elapsed_days + 1)
    return min_ivl, max_ivl


def due_to_date(due: int) -> str:
    offset = due - mw.col.sched.today
    today_date = datetime.today()
    return (today_date + timedelta(days=offset)).strftime("%Y-%m-%d")


def power_forgetting_curve(elapsed_days, stability):
    return (1 + elapsed_days / (9 * stability)) ** -1


def add_dict_key_value(
    dict: dict,
    key: str,
    value: Optional[str] = None,
    new_key: Optional[str] = None,
):
    if new_key is not None and value is None:
        # rename key
        dict[new_key] = dict.pop(key, None)
    elif new_key is not None and value is not None:
        # rename key and change value
        dict.pop(key, None)
        dict[new_key] = value
    elif value is not None:
        # set value for key
        dict[key] = value
    else:
        # remove key
        dict.pop(key, None)


class KeyValueDict(TypedDict):
    key: str
    value: Optional[Union[str, int, float, bool]]
    new_key: Optional[str]


def write_custom_data(
    card: Card,
    key: Optional[str] = None,
    value: Optional[Union[str, int, float, bool]] = None,
    new_key: Optional[str] = None,
    key_values: Optional[list[KeyValueDict]] = None,
):
    """
    Write custom data to the card.
    :param card: The card to write the custom data to.
    :param key: The key to write the value to.
    :param value: The value to write to the key. If None, the key will be removed.
    :param new_key: The new key to rename the key to.
                If value is None, the key will be renamed while keeping the old value.
                If value is not None, the key will be renamed and the value will changed.
    :param key_values: A list of (key, value, new key) tuples. Used for performance as calling
                this function multiple times would perform json.loads and json.dumps multiple times.
    """
    if card.custom_data != "":
        custom_data = json.loads(card.custom_data)
    else:
        custom_data = {}
    if key_values is not None:
        for kv in key_values:
            add_dict_key_value(
                custom_data,
                kv.get("key"),
                kv.get("value"),
                kv.get("new_key"),
            )
    else:
        add_dict_key_value(custom_data, key, value, new_key)
    compressed_data = json.dumps(custom_data, separators=(",", ":"))
    if len(compressed_data) > 100:
        raise ValueError("Custom data exceeds 100 bytes after compression.")
    card.custom_data = compressed_data


def rotate_number_by_k(N, K):
    num = str(N)
    length = len(num)
    K = K % length
    rotated = num[K:] + num[:K]
    return int(rotated)


def get_rev_conf(card: Card):
    deck_id = card.did
    if card.odid:
        deck_id = card.odid
    try:
        deck_easy_fct = mw.col.decks.config_dict_for_deck_id(deck_id)["rev"]["ease4"]
    except KeyError:
        deck_easy_fct = 1.3
    try:
        deck_hard_fct = mw.col.decks.config_dict_for_deck_id(deck_id)["rev"][
            "hardFactor"
        ]
    except KeyError:
        deck_hard_fct = 1.2
    try:
        deck_max_ivl = mw.col.decks.config_dict_for_deck_id(deck_id)["rev"]["maxIvl"]
    except KeyError:
        deck_max_ivl = 3650
    try:
        deck_again_fct = mw.col.decks.config_dict_for_deck_id(deck_id)["lapse"]["mult"]
    except KeyError:
        deck_max_ivl = 0
    return {
        "deck_easy_fct": deck_easy_fct,
        "deck_hard_fct": deck_hard_fct,
        "deck_max_ivl": deck_max_ivl,
        "deck_again_fct": deck_again_fct,
    }


def compress_review_list(review_list: List[Literal[1, 2, 3, 4]]) -> str:
    """
    Compress a list of ease values into a base64-encoded string.
    This can be used to compress dozens of ease values such that the 100 byte limit of the
    card.custom_data is not exceeded.

    The list is compressed by packing 2-bit integers into bytes.
    Each integer is between 1 and 4, so it can be represented by 2 bits.
    The integers are packed into bytes, with the least significant bits being filled first.
    If the number of integers is not a multiple of 4, the last byte is padded with zeroes.

    The compressed string is then encoded to base64 to reduce its size.

    The original list can be recovered by decoding the base64 string and unpacking the bytes.

    :param review_list: A list of integers between 1 and 4
    :return: A base64-encoded string representing the compressed
    """
    # Ensure all integers are between 1 and 4
    if not all(1 <= x <= 4 for x in review_list):
        raise ValueError("All integers must be between 1 and 4")

    # Truncate the list if it is too long
    # If this happens, the list's length will no match a total rep count, which makes it no longer
    # possible to know, if the list is padded with zeroes or not
    # To avoid this, truncate the list to a multiple of 4
    if len(review_list) > MAX_REVIEWS:
        review_list = review_list[-MAX_REVIEWS:]
        review_list = review_list[: len(review_list) - len(review_list) % 4]

    # Pack 2-bit integers into bytes
    packed_bytes = bytearray()
    current_byte = 0
    bits_filled = 0

    for num in review_list:
        current_byte = (current_byte << 2) | (num - 1)
        bits_filled += 2
        if bits_filled == 8:
            packed_bytes.append(current_byte)
            current_byte = 0
            bits_filled = 0

    # If there are remaining bits, pad the last byte
    # NOTE: this effectively adds zeroes to the end of the list which were not originally there
    # To get the original list back, get a rep count and truncate the list to that length
    if bits_filled > 0:
        current_byte <<= 8 - bits_filled
        packed_bytes.append(current_byte)

    # Encode the bytes to a base64 string
    compressed_str = base64.b64encode(packed_bytes).decode("utf-8")
    return compressed_str


def calculate_max_review_list_length(fixed_size):
    """
    Given a some fixed size in bytes that would already take space in the card.custom_data field,
    estimate the maximum length of a review list that can be stored in the card.custom_data field.
    """
    max_size = 100 - fixed_size
    # Each base64 character encodes 6 bits, so we need to account for base64 encoding overhead
    max_bits = max_size * 6 // 8 * 8
    max_reviews = max_bits // 2
    return max_reviews


# Calculate the maximum length of a review list that can be stored in card.custom_data
# assuming that the the existing values in card.custom_data already take up some space
FIXED_SIZE = len(
    json.dumps(
        {
            # auto ease factor modified marker, value is a single char, always present
            "e": "0",
            # reschedule/postpone/advance/disperse marker, value is a single char, always present
            "v": "0",
            # seed, set in the js custom scheduler, not entirely always present
            "s": 1234,
            # success_rate cache, added by auto ease factor, always present
            "sr": 0.956,
        },
        separators=(",", ":"),
    )
)
MAX_REVIEWS = calculate_max_review_list_length(FIXED_SIZE)
