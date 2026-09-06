"""One dictionary scan per distinct word, however many notes and picks ask for it.

An MDX lookup is not the regex-over-loaded-indexes it looks like. `MDXDictionary.query` runs
`SELECT key_text FROM MDX_INDEX WHERE key_text LIKE ?` with leading wildcards, which SQLite
cannot index, so every call is a full scan of one dictionary's key table. `query_japanese`
runs up to four such strategies and `query_all_japanese` loops all seven dictionaries for
each: up to 28 scans of 3.36M rows, 283MB, to retrieve a handful of keys. Measured against
the real dictionaries, one lookup costs 0.9s at the median and 1.3s at the mean.

Measured on a full run, that lookup was the entire run: it drains a `cpu_bound_section` gate
of five slots at 1.0 lookups/sec, and run length equalled lookup count divided by that rate to
within 0.1%. So the cheapest lookup is the one that does not happen. The same run covered
1,382 distinct (word, reading) pairs across 2,159 asks - a 36% repeat rate.

This is sound for the same reason the collection-side word index is: nothing in the add-on
writes `MDX_INDEX`, or the `.mdx` files behind it. The key tables are read-only for the life
of the process, so a lookup's answer cannot change once given.

Three things beyond a plain dict, all of which matter at this concurrency:

- **Entries shared across `pick_dictionary`.** The two bulk callers disagree about the pick -
  `make_all_meanings_for_word` hardcodes "all", `clean_meaning_in_note` passes the config's
  value - and keying on the pick was measured to throw the memo away: of that run's 36% of
  repeats, 722 pairs were asked under *both* picks and only 3.6%/5.9% repeated within one, so
  the memo avoided 5.4% of lookups instead of 35%. The fix is `lookup` below, and it rests on
  "first" not being a different answer but a *prefix* of "all": both picks run the same loop
  over the same dictionaries in the same order, and "first" only returns early once the list
  is non-empty (`mdx_dictionary.query` and `query_all_japanese`). Verified on 40 real pairs
  asked under both picks: 40/40 prefix, 0 violations.

  The derivation is deliberately **one way**. An "all" result serves every pick; a "first"
  result serves only "first", and a "first" ask never upgrades itself to a full scan. Doing
  that - keying on (word, reading) alone and always scanning as "all" - avoids marginally
  more asks and costs more, because "first" short-circuits the dictionary loop and is 1.69x
  cheaper: simulated over the run's real ask sequence, one-way came to 2,890 scan-seconds
  against 3,347 for the unified key and 3,827 for keying on the pick. One-way never runs a
  scan the run is not already running, so it is strictly free. It works because the order is
  structural rather than lucky: inside a word's critical section `make_all_meanings_for_word`
  ("all") runs before the `clean_meaning_in_note` loops ("first"), and "all" was in fact asked
  first for 695 of the 722 shared pairs.

- **Single flight.** Several hundred tasks are in flight, so in principle the same word can be
  asked for while the first ask is still scanning; latecomers wait on the leader's result
  rather than starting their own scan, and they wait *outside* `cpu_bound_section`, so a
  waiter holds no core. Measured, this fires zero times: every MDX caller sits inside the
  per-word `asyncio.Lock` in `match_words_to_notes`, so same-pair asks are strictly serialised
  and there is never a second ask in flight to coalesce. It is kept because it is cheap, and
  because splitting that lock would make it live.

- **A bounded size.** Entries average 1.6K characters and a whole run's distinct pairs came to
  1.58M with 0 evictions against the cap, so a run costs single-digit megabytes; the cap is
  there for a long Anki session running many ops, not for one run. Least-recently-used goes
  first, which within a run is nothing at all.

What is memoised is the structured result list, not the formatted text: "first" is then
`results[:1]` and "shortest"/"longest" are one-line selections over it, whereas the split
points in the formatted text are not unambiguous enough to parse back. Truncation to
`max_length` and the text formatting stay on the way out, in the caller.
"""

import logging
import threading
from collections import OrderedDict
from typing import Callable, Hashable, NamedTuple, Optional

logger = logging.getLogger(__name__)

# What `MultiDictionaryQuery.query` and `query_all_japanese` return: one entry per dictionary
# that had something, with "dictionary" and "definition" keys, in dictionary order.
DefinitionResults = list[dict[str, str]]

# Characters of definition text to keep. A full run's distinct words measured 1.58M characters
# with nothing evicted, so this is roughly ten runs' worth and about 32MB of Python string.
# Small next to what the concurrency gate's memory estimator is already sizing tasks against.
MEMO_MAX_CHARS = 16_000_000

# An empty result - the word is in no dictionary - costs nothing to store but is worth as much
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

    value: Optional[DefinitionResults]
    # "hit": served from the memo. "coalesced": waited on another thread's identical lookup.
    # "computed": actually scanned the dictionaries.
    outcome: str


def entry_chars(results: Optional[DefinitionResults]) -> int:
    """What an entry costs against the size cap: the definition text it holds."""
    if not results:
        return EMPTY_ENTRY_CHARS
    return sum(len(result.get("definition", "")) for result in results)


def apply_pick(
    results: Optional[DefinitionResults], pick_dictionary: str
) -> Optional[DefinitionResults]:
    """Narrow a complete result list the way the dictionaries would have, given the pick.

    This is the whole of what `pick_dictionary` does once every dictionary has been asked:
    `query` collects results in dictionary order and then keeps the head, the shortest or the
    longest. Anything else - including a config value that is none of the four - means all of
    them, which is what that function's default branch does too.
    """
    if not results:
        return results
    if pick_dictionary == "first":
        return results[:1]
    if pick_dictionary == "shortest":
        return [min(results, key=lambda r: len(r["definition"]))]
    if pick_dictionary == "longest":
        return [max(results, key=lambda r: len(r["definition"]))]
    return results


class _InFlight:
    """A lookup one thread is running and others are waiting on."""

    __slots__ = ("event", "value", "failed")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Optional[DefinitionResults] = None
        self.failed = False


class DefinitionMemo:
    """Result lists by key, computed at most once each while the process lives."""

    def __init__(self, max_chars: int = MEMO_MAX_CHARS) -> None:
        self._max_chars = max_chars
        self._lock = threading.Lock()
        self._done: "OrderedDict[Hashable, Optional[DefinitionResults]]" = OrderedDict()
        self._sizes: "dict[Hashable, int]" = {}
        self._chars = 0
        self._in_flight: "dict[Hashable, _InFlight]" = {}
        self.hits = 0
        self.coalesced = 0
        self.computed = 0
        self.evictions = 0

    def lookup(
        self,
        word: str,
        reading: Optional[str],
        pick_dictionary: str,
        scan: Callable[[str], Optional[DefinitionResults]],
    ) -> MemoResult:
        """The results for one ask, sharing entries between picks wherever that is free.

        `scan(pick)` runs the real dictionary scan under the pick it is handed, and is called
        at most once per (word, reading, pick actually scanned). See the module docstring for
        why a cached "all" answers every pick while a cached "first" answers only "first".
        """
        complete_key = (word, reading)
        complete = self.peek(complete_key)
        if complete is not None:
            return MemoResult(apply_pick(complete.value, pick_dictionary), complete.outcome)

        if pick_dictionary == "first":
            # Scanned as "first", so the dictionary loop still short-circuits and the entry
            # answers nothing but "first". It goes stale rather than wrong once some later ask
            # scans the same pair as "all": the peek above then serves everybody, and this
            # entry ages out of the LRU without being consulted again.
            return self.get((word, reading, "first"), lambda: scan("first"))

        # "all", "shortest" and "longest" all scan every dictionary to the end, so asking for
        # everything costs exactly what the narrower pick would have cost and keeps more.
        result = self.get(complete_key, lambda: scan("all"))
        return MemoResult(apply_pick(result.value, pick_dictionary), result.outcome)

    def peek(self, key: Hashable) -> Optional[MemoResult]:
        """The memoised value for `key`, or None if there is not one. Never computes.

        A present entry counts as a hit, exactly as it would through `get`; an absent one
        counts as nothing, because the caller goes on to ask for it under some other key.
        """
        with self._lock:
            return self._peek_locked(key)

    def _peek_locked(self, key: Hashable) -> Optional[MemoResult]:
        """`peek`, for a caller that already holds the lock."""
        if key not in self._done:
            return None
        self._done.move_to_end(key)
        self.hits += 1
        return MemoResult(self._done[key], "hit")

    def get(
        self, key: Hashable, compute: Callable[[], Optional[DefinitionResults]]
    ) -> MemoResult:
        """The memoised value for `key`, calling `compute` at most once per key at a time.

        `compute` runs on the calling thread and outside every lock this class holds, so it is
        free to take as long as an MDX scan takes and to acquire whatever else it needs.
        """
        with self._lock:
            cached = self._peek_locked(key)
            if cached is not None:
                return cached
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

    def _store(self, key: Hashable, value: Optional[DefinitionResults]) -> None:
        """Record a result and evict from the other end until it fits. Caller holds the lock."""
        size = entry_chars(value)
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
