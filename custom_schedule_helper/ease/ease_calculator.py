import math


def moving_average(value_list, weight, init=None) -> float:
    """Provide (float) weighted moving average for list of values."""
    assert len(value_list) > 0
    if init is None:
        mavg = sum(value_list) / len(value_list)
    else:
        mavg = init
    for this_item in value_list:
        mavg = mavg * (1 - weight)
        mavg += this_item * weight
    return mavg


# Define hard answers as halfway between a failure and success
REV_SUCCESS_MAP = {
    0: 0,
    1: 0,
    2: 0.5,
    3: 1,
    4: 1.25,
}


def get_success_rate(review_list, weight, init) -> float:
    success_list = [REV_SUCCESS_MAP[rev_ease] for rev_ease in review_list]
    return moving_average(success_list, weight, init)


def calculate_ease(
    config: dict, deck_starting_ease: int, card_settings: dict, leashed: bool = True
) -> tuple[int, float]:
    """Return next ease factor based on config and card performance."""
    leash = config.leash
    target = config.target_ratio
    max_ease = config.max_ease
    min_ease = config.min_ease
    weight = config.moving_average_weight

    review_list = card_settings["review_list"]
    factor_list = card_settings["factor_list"]
    valid_factor_list = [x for x in factor_list if x is not None] if factor_list else []
    current_ease_factor = None
    if len(valid_factor_list) > 0:
        current_ease_factor = valid_factor_list[-1]
    # If value wasn't set or was set to zero for some reason, use starting ease
    if not current_ease_factor:
        current_ease_factor = deck_starting_ease

    # if no reviews, just assume we're on target
    if review_list is None or len(review_list) < 1:
        success_rate = target
    else:
        success_rate = get_success_rate(review_list, weight, init=target)

    # Ebbinghaus formula
    if success_rate > 0.99:
        success_rate = 0.99  # ln(1) = 0; avoid divide by zero error
    if success_rate < 0.01:
        success_rate = 0.01
    delta_ratio = math.log(target) / math.log(success_rate)

    if len(valid_factor_list) > 0:
        average_ease = moving_average(valid_factor_list, weight)
    else:
        average_ease = deck_starting_ease
    suggested_factor = average_ease * delta_ratio

    # Prevent divide by zero
    if suggested_factor == 0:
        return current_ease_factor, success_rate

    if leashed:
        # factor will increase
        up_leash_multiplier = (
            ((max_ease / current_ease_factor) ** (1 / 3))
            # Smaller multiplier the closer we are to max ease
            * (1 - current_ease_factor / max_ease)
            # Higher multiplier when below starting ease
            * (deck_starting_ease / current_ease_factor)
        )
        # suggested_factor size of increase increases multiplier slightly
        if suggested_factor > current_ease_factor:
            up_leash_multiplier *= (suggested_factor / current_ease_factor) ** (1 / 3)

        # factor will decrease
        down_leash_multiplier = (
            (current_ease_factor / min_ease - 1)
            # Smaller multiplier when below starting ease
            * (current_ease_factor / deck_starting_ease)
        )

        ease_cap = min(max_ease, (current_ease_factor + leash * up_leash_multiplier))

        if suggested_factor > ease_cap:
            suggested_factor = ease_cap

        ease_floor = max(min_ease, (current_ease_factor - leash * down_leash_multiplier))
        if suggested_factor < ease_floor:
            suggested_factor = ease_floor

    # return int(round(suggested_factor + factor_offset)), success_rate
    return min(max(int(round(suggested_factor)), min_ease), max_ease), success_rate


def calculate_all(config_settings, card_settings) -> dict:
    """Recalculate all ease factors based on config and answers."""
    new_factor_list = [config_settings["starting_ease_factor"]]
    print(new_factor_list)
    for count in range(1, 1 + len(card_settings["review_list"])):
        tmp_review_list = card_settings["review_list"][:count]
        tmp_card_settings = {
            "review_list": tmp_review_list,
            "factor_list": new_factor_list,
        }
        new_factor_list.append(calculate_ease(config_settings, tmp_card_settings)[0])
    card_settings["factor_list"] = new_factor_list
    return card_settings
