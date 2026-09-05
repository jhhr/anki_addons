# Custom Schedule Helper

Custom Schedule Helper is an Anki add-on duplicating most of the functionalites of the F4SRSAnki Helper
but instead a different custom scheduling setup.

## Copied F4SRSAnki
- **Reschedule** cards based on their previous review.
- **Postpone** a selected number of due cards.
- **Advance** a selected number of undue cards.
- **Balance** the load during rescheduling (based on fuzz).
- **No Anki** on Free Days (such as weekends) during rescheduling (based on load balance).

## Added in this fork
- **Auto Ease Factor** changes the ease factor based on reviews very gently. Uses entire review history.

## Removed from this fork
- **Disperse** Siblings. Spreading out cards that shouldn't be reviewed on the same day is handled by
  the separate `related_card_disperse` add-on, which disperses by note *and* by configurable relations
  between notes.



# Installation

Just install the addon and use my scheduler.
Or...

1. Write your own custom scheduling js code and apply it. Mine is included in this repository in `custom_scheduler.js`
2. Edit this addon and duplicate that functionality in the `Scheduler.next_interval` function in `schedule/reschule.py`
3. Also edit the Auto Ease Factor logic in `ease/ease_calculator.py` if you don't like the
   aggressively attenuated ease changes.

# Usage

## Overview

| Feature name      | How does it work?                                            | When should I use it?                                        |
| ----------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| Reschedule        | Changes the due dates of cards using the scheduling logic. | When you update you scheduling logic and want to retroactively change the upcoming reviews. |
| Advance           | Decreases the intervals of undue cards based on elapsed time since last review and interval length to (maybe) minimize damage to long-term learning. | When you want to review your material ahead of time, for example, before a test. |
| Postpone          | Increases the intervals of cards that are due today based on elapsed time since last review and interval length in a way that (maybe) minimizes damage to long-term learning. | When you are dealing with a large number of reviews after taking a break from Anki or after rescheduling. |
| Load Balancing    | After the optimal interval is calculated, it is adjusted by a random amount to make the distribution of reviews over time more uniform. | Always. This feature makes your workload (reviews per day) more consistent. |
| Free Days         | After the optimal interval is calculated, it is slightly adjusted to change the due date. | If you don't want to study on some days of the week, for example, Sundays. |

## Reschedule

Rescheduling can calculate the memory states and intervals based on each card's review history and the parameters from the Scheduler code. These parameters can be personalized with the FSRS Optimizer.

**Note**: For cards that have been reviewed multiple times using Anki's default algorithm, rescheduling may give different intervals than the Scheduler because the Scheduler can't access the full review history when running. In this case, the intervals given by rescheduling will be more accurate. But after rescheduling once, there will be no difference between the two.

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/d59f5fef-ebe0-4741-bce6-941e9d6db7cf)

## Advance/Postpone

These two functions are very similar, so I'll talk about them together. You can set the number of cards to advance/postpone, and the Helper add-on will sort your cards and perform the advance/postpone in such a way that the deviation from the original review schedule is minimal while meeting the number of cards you set.

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/7dec9dc6-d6f7-44b0-a845-ae4b9605073d)

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/f9838010-cb00-44ce-aefc-10300f2a586e)

## Load Balance

Once the load balance option is enabled, rescheduling will make the daily review load as consistent and smooth as possible.

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/96f8bd20-0421-4138-8b58-00abbcb3e6d0)

Here's a comparison, the first graph is rescheduling before enabling it, and the second graph is after enabling:

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/1f31491c-7ee6-4eed-ab4a-7bc0dba5dff8)

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/1c4f430d-824b-4145-801e-68fc0329fbbd)

## Free days

You can choose any day or days from Monday to Sunday to take off. Once enabled, the Helper will try
to avoid these days when rescheduling. Note: Free days only works for review cards. Due to technical
limitations, Custom Schedule doesn't modify the interval and due date of (re)learning cards. And it also
doesn't reschedule cards whose interval is less than 3 days to respect the desired retention (maybe).

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/798dc25c-f06c-40fe-8866-ac28c8392273)

**Effect**:

![image](https://github.com/open-spaced-repetition/fsrs4anki-helper/assets/32575846/7fe6b4d0-ae99-40f8-8bd9-0f7c3ff1c638)

## Other features
- **Auto reschedule cards reviewed on other devices after sync:** This option is useful if you do some (or all) of your reviews on platforms that don't support FSRS such as AnkiDroid or AnkiWeb. If this option is enabled, the reviews synced from the other devices will be automatically rescheduled according to the FSRS algorithm. If you are relying on this feature, it is recommended to sync the reviews daily for the best results.
- **Reschedule all cards:** This option is used to reschedule all the cards in the decks in which Custom Schedule is enabled. It should only be used after you have installed Custom Schedule for the first time and/or updated your parameters.
- **Reschedule cards reviewed in the last 7 days:** This option can be used to reschedule the cards that were reviewed in the last few days. The number of days can be adjusted in the add-on config.

