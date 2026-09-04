from aqt import mw

tag = mw.addonManager.addonFromModule(__name__)

LOAD_BALANCE = "load_balance"
FREE_DAYS = "free_days"
DAYS_TO_RESCHEDULE = "days_to_reschedule"
AUTO_RESCHEDULE_AFTER_SYNC = "auto_reschedule_after_sync"
AUTO_DISPERSE_AFTER_SYNC = "auto_disperse_after_sync"
AUTO_DISPERSE = "auto_disperse"
MATURE_IVL = "mature_ivl"
DEBUG_NOTIFY = "debug_notify"
SCHEDULER_STATS = "scheduler_stats"
LEASH = "leash"
MAX_EASE = "max_ease"
MIN_EASE = "min_ease"
MOVING_AVERAGE_WEIGHT = "moving_average_weight"
STATS_ENABLED = "stats_enabled"
STATS_DURATION = "stats_duration"
TARGET_RATIO = "target_ratio"
REVIEWS_ONLY = "reviews_only"
AUTO_ADJUST_EASE_ON_REVIEW = "auto_adjust_ease_on_review"
AUTO_ADJUST_EASE_AFTER_REVIEW = "auto_adjust_ease_after_review"


def load_config():
    return mw.addonManager.getConfig(tag)


def save_config(data):
    mw.addonManager.writeConfig(tag, data)


def run_on_configuration_change(function):
    mw.addonManager.setConfigUpdatedAction(__name__, lambda *_: function())


class Config:
    def load(self):
        self.data = load_config()

    def save(self):
        save_config(self.data)

    @property
    def load_balance(self):
        return self.data[LOAD_BALANCE]

    @load_balance.setter
    def load_balance(self, value):
        self.data[LOAD_BALANCE] = value
        self.save()

    @property
    def free_days(self):
        return self.data[FREE_DAYS]

    @free_days.setter
    def free_days(self, day_enable):
        day, enable = day_enable
        if enable:
            self.data[FREE_DAYS] = sorted(set(self.data[FREE_DAYS] + [day]))
        else:
            if day in self.data[FREE_DAYS]:
                self.data[FREE_DAYS].remove(day)
        self.save()

    @property
    def days_to_reschedule(self):
        return self.data[DAYS_TO_RESCHEDULE]

    @days_to_reschedule.setter
    def days_to_reschedule(self, value):
        self.data[DAYS_TO_RESCHEDULE] = value
        self.save()

    @property
    def auto_reschedule_after_sync(self):
        return self.data[AUTO_RESCHEDULE_AFTER_SYNC]

    @auto_reschedule_after_sync.setter
    def auto_reschedule_after_sync(self, value):
        self.data[AUTO_RESCHEDULE_AFTER_SYNC] = value
        self.save()

    @property
    def auto_disperse_after_sync(self):
        return self.data[AUTO_DISPERSE_AFTER_SYNC]

    @auto_disperse_after_sync.setter
    def auto_disperse_after_sync(self, value):
        self.data[AUTO_DISPERSE_AFTER_SYNC] = value
        self.save()

    @property
    def auto_disperse(self):
        return self.data[AUTO_DISPERSE]

    @auto_disperse.setter
    def auto_disperse(self, value):
        self.data[AUTO_DISPERSE] = value
        self.save()

    @property
    def mature_ivl(self):
        return self.data[MATURE_IVL]

    @mature_ivl.setter
    def mature_ivl(self, value):
        self.data[MATURE_IVL] = value
        self.save()

    @property
    def debug_notify(self):
        return self.data[DEBUG_NOTIFY]

    @debug_notify.setter
    def debug_notify(self, value):
        self.data[DEBUG_NOTIFY] = value
        self.save()

    @property
    def scheduler_stats(self):
        return self.data[SCHEDULER_STATS]

    @scheduler_stats.setter
    def scheduler_stats(self, value):
        self.data[SCHEDULER_STATS] = value
        self.save()

    @property
    def leash(self):
        return self.data[LEASH]

    @leash.setter
    def leash(self, value):
        self.data[LEASH] = value
        self.save()

    @property
    def max_ease(self):
        return self.data[MAX_EASE]

    @max_ease.setter
    def max_ease(self, value):
        self.data[MAX_EASE] = value
        self.save()

    @property
    def min_ease(self):
        return self.data[MIN_EASE]

    @min_ease.setter
    def min_ease(self, value):
        self.data[MIN_EASE] = value
        self.save()

    @property
    def moving_average_weight(self):
        return self.data[MOVING_AVERAGE_WEIGHT]

    @moving_average_weight.setter
    def moving_average_weight(self, value):
        self.data[MOVING_AVERAGE_WEIGHT] = value
        self.save()

    @property
    def stats_enabled(self):
        return self.data[STATS_ENABLED]

    @stats_enabled.setter
    def stats_enabled(self, value):
        self.data[STATS_ENABLED] = value
        self.save()

    @property
    def stats_duration(self):
        return self.data[STATS_DURATION]

    @stats_duration.setter
    def stats_duration(self, value):
        self.data[STATS_DURATION] = value
        self.save()

    @property
    def target_ratio(self):
        return self.data[TARGET_RATIO]

    @target_ratio.setter
    def target_ratio(self, value):
        self.data[TARGET_RATIO] = value
        self.save()

    @property
    def reviews_only(self):
        return self.data[REVIEWS_ONLY]

    @reviews_only.setter
    def reviews_only(self, value):
        self.data[REVIEWS_ONLY] = value
        self.save()

    @property
    def auto_adjust_ease_on_review(self):
        return self.data[AUTO_ADJUST_EASE_ON_REVIEW]

    @auto_adjust_ease_on_review.setter
    def auto_adjust_ease_on_review(self, value):
        self.data[AUTO_ADJUST_EASE_ON_REVIEW] = value
        self.save()

    @property
    def auto_adjust_ease_after_review(self):
        return self.data[AUTO_ADJUST_EASE_AFTER_REVIEW]

    @auto_adjust_ease_after_review.setter
    def auto_adjust_ease_after_review(self, value):
        self.data[AUTO_ADJUST_EASE_AFTER_REVIEW] = value
        self.save()
