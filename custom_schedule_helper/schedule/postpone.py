import math
import random
import time

from anki.decks import DeckManager
from anki.stats import (
    QUEUE_TYPE_REV,
)
from anki.utils import ids2str
from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QDialogButtonBox,
)
from aqt.utils import tooltip, getText, showWarning

from ..utils import (
    write_custom_data,
    RepresentsInt,
    update_card_due_ivl,
    get_last_review_date,
)

WARNING_TEXT = (
    "You can postpone more cards if you wish, but it is not recommended.\nKeep in mind that"
    " whenever you use Postpone or Advance, you depart from the optimal scheduling.\n"
)
INFO_TEXT = "This feature only affects the cards that have been scheduled by Custom Schedule.\n"
INQUIRE_COUNT_TEXT = "Enter the number of cards to be postponed.\n"
INQUIRE_IVL_TEXT = "Or enter the interval above which the cards will be postponed.\n"


class PostPoneDialog(QDialog):
    def __init__(self, safe_cnt, did, cards, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Postpone cards")
        self.cards = cards
        self.main_layout = QVBoxLayout(self)
        self.form_layout = QGridLayout()
        self.main_layout.addLayout(self.form_layout)
        notification_text = (
            f"{'For this deck' if did else 'For this collection'}, it is relatively safe to"
            f" postpone up to {safe_cnt} cards.\n"
        )

        cnt_label = QLabel(notification_text + WARNING_TEXT + INFO_TEXT)
        self.form_layout.addWidget(cnt_label, 0, 0, 1, 2)
        self.count_line_edit = QLineEdit()
        self.form_layout.addWidget(QLabel(INQUIRE_COUNT_TEXT), 1, 0)
        self.form_layout.addWidget(self.count_line_edit, 1, 1)

        self.interval_line_edit = QLineEdit()
        self.form_layout.addWidget(QLabel(INQUIRE_IVL_TEXT), 2, 0)
        self.form_layout.addWidget(self.interval_line_edit, 2, 1)
        # Add a view showing the number of cards with interval greater than the entered interval.
        self.interval_line_edit.textChanged.connect(self.update_interval_view)
        self.interval_view = QLabel()
        self.form_layout.addWidget(self.interval_view, 3, 0, 1, 2)

        self.bottom_layout = QVBoxLayout()
        self.main_layout.addLayout(self.bottom_layout)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.bottom_layout.addWidget(self.button_box)

    def get_inputs(self):
        return (self.count_line_edit.text(), self.interval_line_edit.text())

    def update_interval_view(self):
        interval = self.interval_line_edit.text()
        if RepresentsInt(interval):
            interval = int(interval)
            self.interval_view.setText(
                f"{len(list(filter(lambda x: x[3] >= interval, self.cards)))} cards with interval"
                f" greater than {interval}."
            )
        else:
            self.interval_view.setText("")


def get_desired_postpone_def_with_response(safe_cnt, did, cards):
    dialog = PostPoneDialog(safe_cnt, did, cards)
    if dialog.exec():
        res = dialog.get_inputs()
        if res is not None:
            return RepresentsInt(res[0]), RepresentsInt(res[1])
        return None
    return None


def get_desired_postpone_cnt_with_response(safe_cnt, did):
    notification_text = (
        f"{'For this deck' if did else 'For this collection'}, it is relatively safe to postpone up"
        f" to {safe_cnt} cards.\n"
    )
    (s, r) = getText(
        INQUIRE_COUNT_TEXT + notification_text + WARNING_TEXT + INFO_TEXT, default="10"
    )
    if r:
        return (RepresentsInt(s), r)
    return (None, r)


def postpone(did=None, card_ids=None, parent=None):
    DM = DeckManager(mw.col)
    if did is not None:
        did_list = ids2str(DM.deck_and_child_ids(did))

    if card_ids is not None:
        cid_list = ids2str(card_ids)

    # json_extract(data, '$.dr')
    # WHERE data != ''
    cards = mw.col.db.all(f"""
        SELECT 
            id, 
            CASE WHEN odid==0
            THEN did
            ELSE odid
            END,
            factor,
            ivl,
            CASE WHEN odid==0
            THEN {mw.col.sched.today} - (due - ivl)
            ELSE {mw.col.sched.today} - (odue - ivl)
            END,
            CASE WHEN odid==0
            THEN due
            ELSE odue
            END
        FROM cards
        WHERE queue = {QUEUE_TYPE_REV}
        {f"AND due <= {mw.col.sched.today}" if card_ids is None else ""}
        AND json_extract(json_extract(data, '$.cd'), '$.v') != 'p'
        {"AND id IN %s" % cid_list if card_ids is not None else ""}
        {"AND did IN %s" % did_list if did is not None else ""}
    """)
    # x[0]: cid
    # x[1]: did
    # x[2]: factor
    # x[3]: interval
    # x[4]: elapsed days
    # x[5]: due
    # x[6]: max interval
    cards = map(
        lambda x: (
            x
            + [
                DM.config_dict_for_deck_id(x[1])["rev"]["maxIvl"],
            ]
        ),
        cards,
    )
    # sort by interval (ascending)
    cards = sorted(cards, key=lambda x: x[3])
    safe_cnt = len(list(filter(lambda x: x[4] / x[3] - 1 < 0.25, cards)))

    # If we're in the card browser, don't show the dialog as we're selecting the cards to postpone there
    if card_ids is None:
        res = get_desired_postpone_def_with_response(safe_cnt, did, cards)
        print(res)
        if res is None:
            showWarning(
                "Please enter the number of cards or interval by which you want to postpone."
            )
            return
        else:
            (desired_postpone_cnt, desired_postpone_interval) = res
            print(desired_postpone_cnt, desired_postpone_interval)
            if desired_postpone_cnt is not None and desired_postpone_interval is not None:
                showWarning("Please enter only either the number of cards or the interval.")
                return
            if (desired_postpone_cnt is not None and desired_postpone_cnt <= 0) or (
                desired_postpone_interval is not None and desired_postpone_interval <= 0
            ):
                showWarning("Please enter a positive integer.")
                return

        (desired_postpone_cnt, desired_postpone_interval) = res

        if desired_postpone_interval is not None:
            # filter cards after desired_postpone_interval
            cards = list(filter(lambda x: x[3] >= desired_postpone_interval, cards))
        else:
            # filter cards to desired_postpone_cnt, cutting off from the beginning
            if desired_postpone_cnt < len(cards):
                cards = cards[len(cards) - desired_postpone_cnt : len(cards)]

    undo_entry = mw.col.add_custom_undo_entry("Postpone")

    mw.progress.start()
    start_time = time.time()

    cnt = 0
    ivl_incr = 0

    for cid, _, fct, ivl, elapsed_days, due, max_ivl in cards:
        card = mw.col.get_card(cid)
        random.seed(cid + ivl)
        last_review = get_last_review_date(card)
        elapsed_days = mw.col.sched.today - last_review
        due_days = max(due - mw.col.sched.today, 0)
        # For cards with ivl < 30, postpone by a percentage of the interval
        delay = elapsed_days - ivl
        msg = f"fct={round(fct / 1000, 2)} Elapsed days: {elapsed_days}, IVL: {ivl}"
        if 0 < elapsed_days < 90:
            # Postpone the card between 5% to ~35% depending on the factor
            # the fct is a number between 1300 and 5000, generally ~2500,
            # so we divide by 15000 to get a number between 0.087 and 0.33, generally ~0.17
            if fct < 1100:
                # FSRS factor, convert to an approximate ease factor
                difficulty = (fct - 100) / 1000
                fct = 3000 - 1700 * difficulty
            base_mult = max(0.05, fct / 15000)
            # Randomly add a multiplier between 0 and 0.50, giving bigger variance with smaller elapsed days
            # and then reduce the randomness as we go past 7 days.
            random_mult = 0.25 * random.random() * max((min(1, 7 / elapsed_days)), 2)
            mult = 1 + (
                (
                    (base_mult + random_mult)
                    # Reduce the multiplier as elapsed days closes to 90 days, at  which point it'll be 1.
                    * (1 - elapsed_days / 90)
                    + (elapsed_days / 90) * 0.05
                    # Additionally reduce the multiplier when were close to small elapsed days like 3
                )
                * (min(1, elapsed_days / 7))
            )
            new_ivl = min(
                max_ivl,  # Don't go over the maximum interval
                max(
                    1,  # Don't set interval to less than 1
                    math.floor(elapsed_days * mult)
                    + due_days,  # Postpone by a percentage of elapsed days
                    elapsed_days + due_days,  # Don't lower the due date
                ),
            )
            # Set the increment to the maximum of the new interval and the elapsed days we've postponed
            # so far, thus when start postponing in 1 day increments, we'll start from the next day after
            # the last postponed card.
            ivl_incr = max(new_ivl - elapsed_days, ivl_incr)
            msg += (
                f" Percentage increase, Delay: {delay}, IVL mult: {round(mult, 2)}, Raw new IVL:"
                f" {round(elapsed_days * mult, 2)}, New IVL: {new_ivl}, IVL incr: {ivl_incr}"
            )
        else:
            ivl_incr += 1
            # This card is postponed by 1 more day, the next by 2 more days, and so on.
            new_ivl = min(elapsed_days + ivl_incr + due_days, max_ivl)
            msg += f" Fixed increment, New IVL: {new_ivl}, IVL incr: {ivl_incr}"
        print(msg)
        card = update_card_due_ivl(card, new_ivl)
        write_custom_data(card, "v", "p")
        mw.col.update_card(card)
        mw.col.merge_undo_entries(undo_entry)
        cnt += 1

    tooltip(f"""{cnt} cards postponed in {time.time() - start_time:.2f} seconds.""")
    mw.progress.finish()
    mw.reset()
