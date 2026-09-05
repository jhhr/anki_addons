"""Tests for sync_local_ops/mdx_memo.py.

The memo exists because an MDX lookup is up to 28 full scans of 3.36M rows and a bulk run's
length was measured to be exactly the time those scans take. So what is worth pinning down is
not that a dict remembers things, but the three ways this one can quietly stop saving scans or
start returning the wrong answer:

- a key that does not distinguish two different lookups, or distinguishes two identical ones;
- a second thread starting its own scan for a word already being scanned, which is the common
  case at several hundred tasks in flight and invisible in a single-threaded test;
- an entry that is dropped, or kept, when the size cap says otherwise.

The compute function is a counter, not a dictionary: what is being checked is how often it is
called, which is the only thing the memo controls.
"""

import threading
import unittest

from addon_modules import load_addon_module  # type: ignore

memo_mod = load_addon_module("mdx_memo", subdir="sync_local_ops")


class Counter:
    """A stand-in lookup that records every call and can be told what to answer."""

    def __init__(self, answer: "str | None" = "definition"):
        self.answer = answer
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        return self.answer


class MemoisationTests(unittest.TestCase):
    """One scan per distinct lookup, and distinct means distinct."""

    def test_a_repeated_lookup_is_not_scanned_again(self):
        memo = memo_mod.DefinitionMemo()
        lookup = Counter("大辞泉")

        first = memo.get(("見る", "みる", "all", None), lookup)
        second = memo.get(("見る", "みる", "all", None), lookup)

        self.assertEqual(lookup.calls, 1)
        self.assertEqual((first.value, first.outcome), ("大辞泉", "computed"))
        self.assertEqual((second.value, second.outcome), ("大辞泉", "hit"))

    def test_a_word_with_no_entry_is_not_scanned_again_either(self):
        # The most expensive lookup there is: every strategy runs and every one of them scans
        # each dictionary to the end before concluding nothing is there. Not caching a None
        # would leave the worst case uncached.
        memo = memo_mod.DefinitionMemo()
        lookup = Counter(None)

        first = memo.get(("そんなわけで", "そんなわけで", "all", None), lookup)
        second = memo.get(("そんなわけで", "そんなわけで", "all", None), lookup)

        self.assertEqual(lookup.calls, 1)
        self.assertIsNone(first.value)
        self.assertIsNone(second.value)
        self.assertEqual(second.outcome, "hit")

    def test_every_part_of_the_key_separates_two_lookups(self):
        # pick_dictionary in particular: the two callers disagree about it - make_all_meanings
        # asks for "all" and clean_meaning_in_note for whatever the config says, "first" on the
        # machine this was measured on - and they get different text back for the same word.
        memo = memo_mod.DefinitionMemo()
        lookup = Counter()

        memo.get(("橋", "はし", "all", None), lookup)
        memo.get(("箸", "はし", "all", None), lookup)  # different word
        memo.get(("橋", "きょう", "all", None), lookup)  # different reading
        memo.get(("橋", "はし", "first", None), lookup)  # different dictionary pick
        memo.get(("橋", "はし", "all", 500), lookup)  # different truncation

        self.assertEqual(lookup.calls, 5)


class SingleFlightTests(unittest.TestCase):
    """What happens when the same word is asked for while the first ask is still scanning.

    This is the ordinary case, not the corner: several hundred tasks are in flight and one
    (word, reading) pair appeared 182 times in a single measured run. A plain dict would let
    every one of those start its own scan, because none of them has finished to write an entry
    yet - so the memo would remove nothing from precisely the words that cost the most.
    """

    def test_latecomers_wait_for_the_running_scan_instead_of_starting_their_own(self):
        memo = memo_mod.DefinitionMemo()
        scanning = threading.Event()
        finish = threading.Event()
        calls = []

        def slow_lookup():
            calls.append(1)
            scanning.set()
            finish.wait(5)
            return "大辞泉"

        results = {}

        def ask(name):
            results[name] = memo.get(("見る", "みる", "all", None), slow_lookup)

        # daemon: if the coalescing ever deadlocks, this fails the test rather than hanging the
        # whole suite on a join that never returns
        leader = threading.Thread(target=ask, args=("leader",), daemon=True)
        leader.start()
        self.assertTrue(scanning.wait(5), "the first lookup never started")

        latecomers = [
            threading.Thread(target=ask, args=(f"late{i}",), daemon=True) for i in range(4)
        ]
        for thread in latecomers:
            thread.start()
        # Still inside the leader's scan: nobody may have returned, and nobody may have started
        # a second scan
        for thread in latecomers:
            thread.join(0.2)
            self.assertTrue(thread.is_alive(), "a latecomer returned before the scan finished")
        self.assertEqual(len(calls), 1)

        finish.set()
        for thread in [leader, *latecomers]:
            thread.join(5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(len(calls), 1, "a latecomer ran its own scan")
        self.assertEqual(results["leader"].outcome, "computed")
        for i in range(4):
            self.assertEqual(results[f"late{i}"].value, "大辞泉")
            self.assertEqual(results[f"late{i}"].outcome, "coalesced")
        self.assertEqual(memo.coalesced, 4)

        # And the answer outlives the scan that produced it
        after = memo.get(("見る", "みる", "all", None), slow_lookup)
        self.assertEqual((after.value, after.outcome), ("大辞泉", "hit"))
        self.assertEqual(len(calls), 1)

    def test_two_different_words_do_not_wait_on_each_other(self):
        # Coalescing must be per key. A single global lock around the compute would serialise
        # the whole run onto one thread, which is worse than the problem the memo solves.
        memo = memo_mod.DefinitionMemo()
        both_in = threading.Barrier(2, timeout=5)

        def lookup():
            both_in.wait()  # only returns if the other thread got in too
            return "definition"

        threads = [
            threading.Thread(
                target=lambda w=word: memo.get((w, w, "all", None), lookup), daemon=True
            )
            for word in ("橋", "箸")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "two different words serialised on each other")


class FailureTests(unittest.TestCase):
    """A lookup that raises must leave nothing behind - not a cached answer, not a blocked
    waiter, and not a key that no thread will ever compute again."""

    def test_a_raising_lookup_is_not_cached_and_is_retried(self):
        memo = memo_mod.DefinitionMemo()
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("dictionary file went away")
            return "大辞泉"

        with self.assertRaises(RuntimeError):
            memo.get(("見る", "みる", "all", None), flaky)
        retried = memo.get(("見る", "みる", "all", None), flaky)

        self.assertEqual(retried.value, "大辞泉")
        self.assertEqual(len(attempts), 2)

    def test_a_waiter_on_a_failing_lookup_computes_rather_than_hanging(self):
        memo = memo_mod.DefinitionMemo()
        scanning = threading.Event()
        fail_now = threading.Event()

        def leader_lookup():
            scanning.set()
            fail_now.wait(5)
            raise RuntimeError("dictionary file went away")

        def leader_body():
            try:
                memo.get(("見る", "みる", "all", None), leader_lookup)
            except RuntimeError:
                pass

        leader = threading.Thread(target=leader_body, daemon=True)
        leader.start()
        self.assertTrue(scanning.wait(5))

        waiter_result = {}
        waiter = threading.Thread(
            target=lambda: waiter_result.update(
                r=memo.get(("見る", "みる", "all", None), Counter("大辞泉"))
            ),
            daemon=True,
        )
        waiter.start()
        fail_now.set()

        waiter.join(5)
        self.assertFalse(waiter.is_alive(), "the waiter was never woken by the failing leader")
        self.assertEqual(waiter_result["r"].value, "大辞泉")


class SizeCapTests(unittest.TestCase):
    """The cap is for a long Anki session, not for one run - a run's whole working set was
    measured at about 3.3M characters against a 16M budget. What matters is that when it does
    bite, it drops the coldest entry and keeps the rest."""

    def test_the_least_recently_used_entry_goes_first(self):
        memo = memo_mod.DefinitionMemo(max_chars=25)
        ten_chars = Counter("0123456789")

        memo.get("a", ten_chars)
        memo.get("b", ten_chars)
        memo.get("a", ten_chars)  # a is now the more recently used of the two
        memo.get("c", ten_chars)  # 30 > 25, so one has to go

        self.assertEqual(memo.evictions, 1)
        self.assertEqual(memo.get("c", ten_chars).outcome, "hit")
        self.assertEqual(memo.get("a", ten_chars).outcome, "hit")
        self.assertEqual(memo.get("b", ten_chars).outcome, "computed")

    def test_an_entry_larger_than_the_whole_budget_is_still_kept(self):
        # The newest entry is never the one evicted, however far over the budget it puts the
        # memo. Dropping it instead would mean the largest results - which come from the words
        # with the most dictionary entries, i.e. the most-looked-up ones - are the only ones
        # never memoised, and would empty the memo to do it.
        memo = memo_mod.DefinitionMemo(max_chars=5)
        huge = Counter("x" * 100)

        first = memo.get("a", huge)
        second = memo.get("a", huge)

        self.assertEqual(first.value, "x" * 100)
        self.assertEqual(second.outcome, "hit")
        self.assertEqual(huge.calls, 1)


class BookkeepingTests(unittest.TestCase):
    """The counters are how the hit rate gets measured on a real run, so they have to add up."""

    def test_the_summary_counts_every_lookup_once(self):
        memo = memo_mod.DefinitionMemo()
        lookup = Counter()
        memo.get("a", lookup)
        memo.get("a", lookup)
        memo.get("b", lookup)

        self.assertEqual((memo.computed, memo.hits, memo.coalesced), (2, 1, 0))
        self.assertIn("3 lookups asked, 2 scanned", memo.summary())
        self.assertIn("33.3% avoided", memo.summary())

    def test_clearing_forgets_the_answers_and_the_counters(self):
        memo = memo_mod.DefinitionMemo()
        lookup = Counter()
        memo.get("a", lookup)
        memo.clear()

        self.assertEqual(memo.get("a", lookup).outcome, "computed")
        self.assertEqual(lookup.calls, 2)
        self.assertEqual(memo.hits, 0)


if __name__ == "__main__":
    unittest.main()
