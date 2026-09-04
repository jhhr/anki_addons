## Interval scheduling config, in this screen

### `days_to_reschedule`

This sets the number of days in "Reschedule cards reviewed in the last n days"; the current day included(!). Works like [searching for "rated:" in the browser](https://docs.ankiweb.net/searching.html?highlight=rated#answered).

## Configure via menu bar: Tools > Custom Schedule Helper

### `free_days`

Load Balancing must be enabled for this. Select any day of the week (e.g., Sunday) that you do not want to study on. It will keep the selected day(s) free of reviews and instead select another day when rescheduling.

### `load_balance`

Fuzz must be enabled for this (default: enabled, set in the scheduler code). During rescheduling, it keeps the daily number consistent instead of fluctuating.

## Auto Ease Factor config, only in this screen

### `leash`

- Controls how much the algorithm can change ease after any single review
- default 100
- Note: This window expands after every review, so after we have some data on
the card the algorithm will get more aggressive. Also, this is per mille. So
if you set this to 100, and your starting ease is 250%, and you answer
perfectly, your ease will be 250, 260, 280, 310.... If you've had over 10
reviews this can still change your ease by over 1000 points.

### `max_ease`

- The maximum ease you want any of your cards to reach.
- default: 5000 (500%)
- Note that once you get over 5-7k the time savings are minimal and the risks
of miscalculation are higher.

### `min_ease`

- The minimum ease you want any of your cards to retreat to.
- default: 1000 (100%)

### `moving_average_weight`

- Specifies how much weight to place on more recent reviews over old reviews. Bigger -> more recent gets more weight. Smaller --> old reviews get more equal weight.
Note: this is very sensitive, values between 0.07 and 0.3 are about right for
most people.
- default: 0.2

### `reviews_only`

- *WARNING WARNING WARNING* This will limit the amount of data used by the
algorithm, sometimes severely. Cards that spend a significant amount of
time in learning or relearning (your hardest cards) will suddenly benefit
much less and adapt the most slowly to your actual performance.
*I STRONGLY ADVISE YOU DO NOT CHANGE THIS SETTING!*
- default: false

### `stats_duration`

- Length of time the tooltips persist (only relevant if show_stats is True)
- default: 5000

### `stats_enabled`

- Show debugging information about recent reviews in a tooltip. Includes
information on past reviews and suggested ease.
- default: True

### `target_ratio`

- Specifies your target success rate between 0 and 1. Note: Exponentially
higher numbers of reviews are required the closer you get to 1, most people
will find mid 80s or low 90s about right.
- default: 0.85