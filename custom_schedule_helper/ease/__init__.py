import aqt
from aqt.gui_hooks import reviewer_will_answer_card, reviewer_did_answer_card
from .auto_ease_factor import adjust_factor_when_review, adjust_factor_after_review


def init_ease_adjust_review_hook():
    reviewer_will_answer_card.append(adjust_factor_when_review)
    reviewer_did_answer_card.append(adjust_factor_after_review)
