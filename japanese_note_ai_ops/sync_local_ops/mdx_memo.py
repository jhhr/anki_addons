"""One dictionary lookup per distinct word, however many notes ask for it.

An MDX lookup is not the regex-over-loaded-indexes it looks like. `MDXDictionary.query` runs
`SELECT key_text FROM MDX_INDEX WHERE key_text LIKE ?` with leading wildcards, which SQLite
cannot index, so every call is a full scan of one dictionary's key table. `query_japanese`
runs up to four such strategies and `query_all_japanese` loops all seven dictionaries for
each: up to 28 scans of 3.36M rows, 283MB, to retrieve a handful of keys. Measured against
the real dictionaries, one lookup costs 0.9s at the median and 1.3s at the mean.

Measured on a full run, that lookup was the entire run: it drains a `cpu_bound_section` gate
of five slots at 1.0 lookups/sec, and run length equalled lookup count divided by that rate to
within 0.1%. So the cheapest lookup is the one that does not happen. The same run covered
2,067 distinct (word, reading) pairs with 3,228 sections - a 36% repeat rate before counting
`clean_meaning_in_note`, which looks the word up once per matched *note* inside a section and
issued more than twice as many lookups as the per-word path did.

This is sound for the same reason the collection-side word index is: nothing in the add-on
writes `MDX_INDEX`, or the `.mdx` files behind it. The key tables are read-only for the life
of the process, so a lookup's answer cannot change once given.

Two things beyond a plain dict, both of which matter at this concurrency:

- **Single flight.** Several hundred tasks are in flight and the hottest pair appeared 182
  times in one run, so the same word is very often asked for while the first ask is still
  running. Latecomers wait on the leader's result rather than starting their own scan, and
  they wait *outside* `cpu_bound_section` - a waiter holds no core.
- **A bounded size.** Entries average 1.6K characters and a whole run's distinct pairs come to
  about 3.3M, so a run costs single-digit megabytes; the cap is there for a long Anki session
  running many ops, not for one run. Least-recently-used goes first, which within a run is
  nothing at all.
"""

import logging
import threading
from collections import OrderedDict
from typing import Callable, Hashable, NamedTuple, Optional

logger = logging.getLogger(__name__)

# Characters of definition text to keep. A full run's distinct words measured ~3.3M characters
# (2,067 pairs, 1,618 characters mean), so this is roughly five runs' worth and about 32MB of
# Python string. Small next to what the concurrency gate's memory estimator is already sizing
# tasks against, and large enough that a single run never evicts.
MEMO_MAX_CHARS = 16_000_000

# A `None` result - the word is in no dictionary - costs nothing to store but is worth as much
# as any other entry, because a miss is the *most* expensive lookup: all four strategies run
# and all of them scan every dictionary to the end. Charge it a nominal size so a run made
# entirely of misses still respects the cap.
EMPTY_ENTRY_CHARS = 64

# How often to log the running hit rate. The per-lookup line names the word, which is what
# makes the `clean_meaning_in_note` path measurable at all, but it is DEBUG; this one is INFO
# so the rate shows up in a run logged at any level.
REPORT_EVERY = 100


class MemoResult(NamedTuple):
    """A value and how it was come by, so the caller can log the hit rate per key."""

    value: Optional[str]
    # "hit": served from the memo. "coalesced": waited on another thread's identical lookup.
    # "computed": actually scanned the dictionaries.
    outcome: str


class _InFlight:
    """A lookup one thread is running and others are waiting on."""

    __slots__ = ("event", "value", "failed")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Optional[str] = None
        self.failed = False


class DefinitionMemo:
    """Text results by key, computed at most once each while the process lives."""

    def __init__(self, max_chars: int = MEMO_MAX_CHARS) -> None:
        self._max_chars = max_chars
        self._lock = threading.Lock()
        self._done: "OrderedDict[Hashable, Optional[str]]" = OrderedDict()
        self._sizes: "dict[Hashable, int]" = {}
        self._chars = 0
        self._in_flight: "dict[Hashable, _InFlight]" = {}
        self.hits = 0
        self.coalesced = 0
        self.computed = 0
        self.evictions = 0

    def get(self, key: Hashable, compute: Callable[[], Optional[str]]) -> MemoResult:
        """The memoised value for `key`, calling `compute` at most once per key at a time.

        `compute` runs on the calling thread and outside every lock this class holds, so it is
        free to take as long as an MDX scan takes and to acquire whatever else it needs.
        """
        with self._lock:
            if key in self._done:
                self._done.move_to_end(key)
                self.hits += 1
                return MemoResult(self._done[key], "hit")
            already_running = self._in_flight.get(key)
            leading = already_running is None
            entry = _InFlight() if already_running is None else already_running
            if leading:
                self._in_flight[key] = entry
            else:
                self.coalesced += 1

        if not leading:
            entry.event.wait()
            if not entry.failed:
                return MemoResult(entry.value, "coalesced")
            # The leader raised. Nothing was cached and nothing is in flight any more, so this
            # thread does the work itself rather than passing on a result nobody produced.
            return MemoResult(compute(), "computed")

        try:
            value = compute()
        except BaseException:
            entry.failed = True
            raise
        else:
            entry.value = value
            return MemoResult(value, "computed")
        finally:
            # Cleared under the lock and only then signalled, so no waiter can wake to find
            # the key neither cached nor in flight.
            report = False
            with self._lock:
                self._in_flight.pop(key, None)
                if not entry.failed:
                    self._store(key, entry.value)
                    self.computed += 1
                    report = self.computed % REPORT_EVERY == 0
            entry.event.set()
            if report:
                logger.info(self.summary())

    def _store(self, key: Hashable, value: Optional[str]) -> None:
        """Record a result and evict from the other end until it fits. Caller holds the lock."""
        size = len(value) if value else EMPTY_ENTRY_CHARS
        self._done[key] = value
        self._sizes[key] = size
        self._chars += size
        while self._chars > self._max_chars and len(self._done) > 1:
            oldest, _ = self._done.popitem(last=False)
            self._chars -= self._sizes.pop(oldest, 0)
            self.evictions += 1

    def summary(self) -> str:
        """One line of hit rate and size, for the log."""
        asked = self.hits + self.coalesced + self.computed
        served = self.hits + self.coalesced
        share = (served / asked * 100) if asked else 0.0
        return (
            f"MDX memo: {asked} lookups asked, {self.computed} scanned,"
            f" {self.hits} from memo, {self.coalesced} coalesced ({share:.1f}% avoided);"
            f" {len(self._done)} entries, {self._chars / 1e6:.2f}M chars,"
            f" {self.evictions} evicted"
        )

    def clear(self) -> None:
        """Forget everything. For a change of dictionaries, and for tests."""
        with self._lock:
            self._done.clear()
            self._sizes.clear()
            self._chars = 0
            self.hits = self.coalesced = self.computed = self.evictions = 0
