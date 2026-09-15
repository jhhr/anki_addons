"""Tests for sync_local_ops/mdx_memo.py.

The memo exists because an MDX lookup is up to 28 full scans of 3.36M rows and a bulk run's
length was measured to be exactly the time those scans take. So what is worth pinning down is
not that a dict remembers things, but the four ways this one can quietly stop saving scans or
start returning the wrong answer:

- a key that does not distinguish two different lookups, or distinguishes two identical ones -
  which is what a measured run caught it doing, keying on `pick_dictionary` and so avoiding
  5.4% of lookups where 35% were there to take;
- serving a pick from an entry that cannot answer it, or scanning again for one that can;
- a second thread starting its own scan for a word already being scanned, which is the common
  case at several hundred tasks in flight and invisible in a single-threaded test;
- an entry that is dropped, or kept, when the size cap says otherwise.

The scan function is a counter, not a dictionary: what is being checked is how often it is
called and under which pick, which is the only thing the memo controls.
"""

import threading
import unittest

from addon_modules import load_addon_module  # type: ignore

memo_mod = load_addon_module("mdx_memo", subdir="sync_local_ops")


def entries(*pairs):
    """Result rows as `MultiDictionaryQuery` hands them over: dictionary order, one per hit."""
    return [{"dictionary": name, "definition": text} for name, text in pairs]


class Scanner:
    """A stand-in dictionary scan that records every call and the pick it was made under.

    It short-circuits on "first" the way the real one does - `query` returns the moment the
    result list is non-empty - which is the property the whole cross-pick sharing rests on.
    """

    def __init__(self, *pairs):
        self.results = entries(*pairs)
        self.picks = []
        self._lock = threading.Lock()

    def __call__(self, pick):
        with self._lock:
            self.picks.append(pick)
        if pick == "first":
            return self.results[:1]
        return list(self.results)

    @property
    def calls(self):
        return len(self.picks)


class Counter:
    """A stand-in lookup for the plain `get` path, which knows nothing about picks."""

    def __init__(self, answer=None):
        self.answer = entries(("大辞泉", "definition")) if answer is None else answer
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        return self.answer


class PickDerivationTests(unittest.TestCase):
    """`apply_pick` on its own: what the dictionaries would have returned for each pick."""

    def test_first_is_the_head_of_the_list(self):
        results = entries(("大辞泉", "long one"), ("新明解", "short"))

        self.assertEqual(memo_mod.apply_pick(results, "first"), results[:1])

    def test_shortest_and_longest_measure_the_definition(self):
        results = entries(("大辞泉", "xxxxx"), ("新明解", "x"), ("広辞苑", "xxx"))

        self.assertEqual(memo_mod.apply_pick(results, "shortest"), [results[1]])
        self.assertEqual(memo_mod.apply_pick(results, "longest"), [results[0]])

    def test_all_and_anything_unrecognised_keep_every_result(self):
        # PickDictionaryResult is a Literal widened with str, so a config typo reaches here.
        # `query` falls through to returning everything; so must this.
        results = entries(("大辞泉", "a"), ("新明解", "b"))

        self.assertEqual(memo_mod.apply_pick(results, "all"), results)
        self.assertEqual(memo_mod.apply_pick(results, "evrything"), results)

    def test_a_word_in_no_dictionary_stays_empty_under_every_pick(self):
        for pick in ("first", "all", "shortest", "longest"):
            self.assertEqual(memo_mod.apply_pick([], pick), [])
            self.assertIsNone(memo_mod.apply_pick(None, pick))


class SharedEntryTests(unittest.TestCase):
    """One scan serving both callers' picks - the whole of what this step is for.

    `make_all_meanings_for_word` asks for "all" and `clean_meaning_in_note` for whatever the
    config says. Measured, 722 of one run's 1,382 distinct pairs were asked under both, and
    that overlap *was* the memo's missing hit rate. The derivation only runs one way, which is
    the other half of these tests: an "all" result answers everything, a "first" result answers
    only "first", and a "first" ask never upgrades itself to the 1.69x more expensive scan.
    """

    def test_a_first_ask_is_served_from_an_earlier_all(self):
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "long one"), ("新明解", "short"))

        everything = memo.lookup("橋", "はし", "all", scan)
        just_one = memo.lookup("橋", "はし", "first", scan)

        self.assertEqual(scan.picks, ["all"], "the first ask ran its own scan")
        self.assertEqual(everything.value, scan.results)
        self.assertEqual(just_one.value, scan.results[:1])
        self.assertEqual(just_one.outcome, "hit")

    def test_shortest_and_longest_are_served_from_the_same_entry(self):
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "xxxxx"), ("新明解", "x"))

        memo.lookup("橋", "はし", "all", scan)
        shortest = memo.lookup("橋", "はし", "shortest", scan)
        longest = memo.lookup("橋", "はし", "longest", scan)

        self.assertEqual(scan.picks, ["all"])
        self.assertEqual(shortest.value, [scan.results[1]])
        self.assertEqual(longest.value, [scan.results[0]])

    def test_a_narrowing_pick_scans_for_everything_so_the_entry_serves_the_rest(self):
        # "shortest" and "longest" ask every dictionary before choosing, so scanning as "all"
        # costs them nothing and leaves an entry the other picks can use.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "xxxxx"), ("新明解", "x"))

        shortest = memo.lookup("橋", "はし", "shortest", scan)
        everything = memo.lookup("橋", "はし", "all", scan)

        self.assertEqual(scan.picks, ["all"])
        self.assertEqual(shortest.value, [scan.results[1]])
        self.assertEqual(everything.value, scan.results)

    def test_a_first_ask_does_not_upgrade_itself_to_a_full_scan(self):
        # The unified key - always scan "all" - avoids marginally more asks and costs more,
        # because "first" short-circuits the dictionary loop. Simulated over a real run it was
        # 3,347 scan-seconds against 2,890 for scanning each pick as it was asked.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "long one"), ("新明解", "short"))

        first_ask = memo.lookup("橋", "はし", "first", scan)
        repeat = memo.lookup("橋", "はし", "first", scan)

        self.assertEqual(scan.picks, ["first"], "a first ask paid for a full scan")
        self.assertEqual(first_ask.value, scan.results[:1])
        self.assertEqual(repeat.outcome, "hit")

    def test_an_all_ask_after_a_first_ask_scans_again(self):
        # The one case the one-way derivation gives up on: 27 of the 722 shared pairs in the
        # measured run, words that needed cleaning without meaning generation. A "first" entry
        # holds only the head of the list, so serving "all" from it would silently drop every
        # other dictionary.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "long one"), ("新明解", "short"))

        memo.lookup("橋", "はし", "first", scan)
        everything = memo.lookup("橋", "はし", "all", scan)

        self.assertEqual(scan.picks, ["first", "all"])
        self.assertEqual(everything.value, scan.results)

    def test_the_all_entry_takes_over_once_it_exists(self):
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "long one"), ("新明解", "short"))

        memo.lookup("橋", "はし", "first", scan)
        memo.lookup("橋", "はし", "all", scan)
        after = memo.lookup("橋", "はし", "first", scan)

        self.assertEqual(scan.picks, ["first", "all"], "the stale first entry was rescanned")
        self.assertEqual(after.value, scan.results[:1])

    def test_the_scan_is_only_ever_asked_for_first_or_all(self):
        # Anything else would be a narrowed result cached under the complete key.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "xxxxx"), ("新明解", "x"))

        for word, pick in zip("橋箸端映", ("shortest", "longest", "first", "evrything")):
            memo.lookup(word, "はし", pick, scan)

        self.assertEqual(sorted(set(scan.picks)), ["all", "first"])

    def test_a_word_in_no_dictionary_is_shared_across_picks_too(self):
        # A miss is the most expensive lookup there is - every strategy runs and every one of
        # them scans each dictionary to the end - and if "all" found nothing, "first" cannot.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner()

        missing = memo.lookup("そんなわけで", "そんなわけで", "all", scan)
        again = memo.lookup("そんなわけで", "そんなわけで", "first", scan)

        self.assertEqual(scan.picks, ["all"])
        self.assertEqual(missing.value, [])
        self.assertEqual(again.value, [])
        self.assertEqual(again.outcome, "hit")

    def test_the_word_and_the_reading_still_separate_two_lookups(self):
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "definition"))

        memo.lookup("橋", "はし", "all", scan)
        memo.lookup("箸", "はし", "all", scan)  # different word
        memo.lookup("橋", "きょう", "all", scan)  # different reading
        memo.lookup("橋", None, "all", scan)  # no reading at all: a different query entirely

        self.assertEqual(scan.calls, 4)


class MemoisationTests(unittest.TestCase):
    """One scan per distinct key, on the plain `get` path underneath `lookup`."""

    def test_a_repeated_lookup_is_not_scanned_again(self):
        memo = memo_mod.DefinitionMemo()
        lookup = Counter()

        first = memo.get(("見る", "みる"), lookup)
        second = memo.get(("見る", "みる"), lookup)

        self.assertEqual(lookup.calls, 1)
        self.assertEqual((first.value, first.outcome), (lookup.answer, "computed"))
        self.assertEqual((second.value, second.outcome), (lookup.answer, "hit"))

    def test_a_word_with_no_entry_is_not_scanned_again_either(self):
        # The most expensive lookup there is: every strategy runs and every one of them scans
        # each dictionary to the end before concluding nothing is there. Not caching an empty
        # result would leave the worst case uncached.
        memo = memo_mod.DefinitionMemo()
        lookup = Counter([])

        first = memo.get(("そんなわけで", "そんなわけで"), lookup)
        second = memo.get(("そんなわけで", "そんなわけで"), lookup)

        self.assertEqual(lookup.calls, 1)
        self.assertEqual(first.value, [])
        self.assertEqual(second.value, [])
        self.assertEqual(second.outcome, "hit")

    def test_peeking_never_computes(self):
        memo = memo_mod.DefinitionMemo()
        lookup = Counter()

        self.assertIsNone(memo.peek(("見る", "みる")))
        memo.get(("見る", "みる"), lookup)

        self.assertEqual(memo.peek(("見る", "みる")).value, lookup.answer)
        self.assertEqual(lookup.calls, 1)


class SingleFlightTests(unittest.TestCase):
    """What happens when the same word is asked for while the first ask is still scanning.

    Measured, this never fires: every MDX caller sits inside the per-word `asyncio.Lock` in
    `match_words_to_notes`, so same-pair asks are serialised and there is no second ask in
    flight to coalesce. It is kept because it is cheap and because splitting that lock - the
    one change left that could reach the per-word chain - would make it live at once, with
    several hundred tasks in flight and one pair appearing 182 times in a single run.
    """

    def test_latecomers_wait_for_the_running_scan_instead_of_starting_their_own(self):
        memo = memo_mod.DefinitionMemo()
        answer = entries(("大辞泉", "definition"))
        scanning = threading.Event()
        finish = threading.Event()
        calls = []

        def slow_lookup():
            calls.append(1)
            scanning.set()
            finish.wait(5)
            return answer

        results = {}

        def ask(name):
            results[name] = memo.get(("見る", "みる"), slow_lookup)

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
            self.assertEqual(results[f"late{i}"].value, answer)
            self.assertEqual(results[f"late{i}"].outcome, "coalesced")
        self.assertEqual(memo.coalesced, 4)

        # And the answer outlives the scan that produced it
        after = memo.get(("見る", "みる"), slow_lookup)
        self.assertEqual((after.value, after.outcome), (answer, "hit"))
        self.assertEqual(len(calls), 1)

    def test_two_different_words_do_not_wait_on_each_other(self):
        # Coalescing must be per key. A single global lock around the compute would serialise
        # the whole run onto one thread, which is worse than the problem the memo solves.
        memo = memo_mod.DefinitionMemo()
        both_in = threading.Barrier(2, timeout=5)

        def lookup():
            both_in.wait()  # only returns if the other thread got in too
            return entries(("大辞泉", "definition"))

        threads = [
            threading.Thread(target=lambda w=word: memo.get((w, w), lookup), daemon=True)
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
        answer = entries(("大辞泉", "definition"))
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("dictionary file went away")
            return answer

        with self.assertRaises(RuntimeError):
            memo.get(("見る", "みる"), flaky)
        retried = memo.get(("見る", "みる"), flaky)

        self.assertEqual(retried.value, answer)
        self.assertEqual(len(attempts), 2)

    def test_a_waiter_on_a_failing_lookup_computes_rather_than_hanging(self):
        memo = memo_mod.DefinitionMemo()
        answer = entries(("大辞泉", "definition"))
        scanning = threading.Event()
        fail_now = threading.Event()

        def leader_lookup():
            scanning.set()
            fail_now.wait(5)
            raise RuntimeError("dictionary file went away")

        def leader_body():
            try:
                memo.get(("見る", "みる"), leader_lookup)
            except RuntimeError:
                pass

        leader = threading.Thread(target=leader_body, daemon=True)
        leader.start()
        self.assertTrue(scanning.wait(5))

        waiter_result = {}
        waiter = threading.Thread(
            target=lambda: waiter_result.update(r=memo.get(("見る", "みる"), Counter(answer))),
            daemon=True,
        )
        waiter.start()
        fail_now.set()

        waiter.join(5)
        self.assertFalse(waiter.is_alive(), "the waiter was never woken by the failing leader")
        self.assertEqual(waiter_result["r"].value, answer)


class SizeCapTests(unittest.TestCase):
    """The cap is for a long Anki session, not for one run - a run's whole working set was
    measured at 1.58M characters against a 16M budget, with nothing evicted. What matters is
    that an entry is charged for the definitions it holds, and that when the cap does bite it
    drops the coldest entry and keeps the rest."""

    def test_an_entry_costs_the_definition_text_it_holds(self):
        # Entries are result lists now, so a run's several-dictionary words have to be charged
        # for all of them rather than for one, or the cap counts a fraction of what it holds.
        self.assertEqual(memo_mod.entry_chars(entries(("a", "xxx"), ("b", "xx"))), 5)
        self.assertEqual(memo_mod.entry_chars([]), memo_mod.EMPTY_ENTRY_CHARS)
        self.assertEqual(memo_mod.entry_chars(None), memo_mod.EMPTY_ENTRY_CHARS)

    def test_the_least_recently_used_entry_goes_first(self):
        memo = memo_mod.DefinitionMemo(max_chars=25)
        ten_chars = Counter(entries(("大辞泉", "0123456789")))

        memo.get("a", ten_chars)
        memo.get("b", ten_chars)
        memo.get("a", ten_chars)  # a is now the more recently used of the two
        memo.get("c", ten_chars)  # 30 > 25, so one has to go

        self.assertEqual(memo.evictions, 1)
        self.assertEqual(memo.get("c", ten_chars).outcome, "hit")
        self.assertEqual(memo.get("a", ten_chars).outcome, "hit")
        self.assertEqual(memo.get("b", ten_chars).outcome, "computed")

    def test_a_peek_counts_as_a_use_for_eviction(self):
        # `lookup` serves every derived pick through `peek`, so a pair asked only as "first"
        # after its "all" scan would look untouched to the LRU if peeking did not count.
        memo = memo_mod.DefinitionMemo(max_chars=25)
        ten_chars = Counter(entries(("大辞泉", "0123456789")))

        memo.get("a", ten_chars)
        memo.get("b", ten_chars)
        memo.peek("a")
        memo.get("c", ten_chars)

        self.assertEqual(memo.get("a", ten_chars).outcome, "hit")
        self.assertEqual(memo.get("b", ten_chars).outcome, "computed")

    def test_an_entry_larger_than_the_whole_budget_is_still_kept(self):
        # The newest entry is never the one evicted, however far over the budget it puts the
        # memo. Dropping it instead would mean the largest results - which come from the words
        # with the most dictionary entries, i.e. the most-looked-up ones - are the only ones
        # never memoised, and would empty the memo to do it.
        memo = memo_mod.DefinitionMemo(max_chars=5)
        huge = Counter(entries(("大辞泉", "x" * 100)))

        first = memo.get("a", huge)
        second = memo.get("a", huge)

        self.assertEqual(first.value, huge.answer)
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

    def test_a_pick_derived_from_a_cached_scan_counts_as_a_hit(self):
        # This is the number the whole step is judged on: a run whose two callers share entries
        # should report a hit rate near 35%, against the 5.4% it managed keying on the pick.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "definition"))

        memo.lookup("橋", "はし", "all", scan)
        memo.lookup("橋", "はし", "first", scan)

        self.assertEqual((memo.computed, memo.hits), (1, 1))
        self.assertIn("2 lookups asked, 1 scanned", memo.summary())

    def test_a_miss_that_falls_through_to_a_second_key_is_counted_once(self):
        # `lookup` peeks the complete key before asking for the "first" one. That peek must not
        # count as anything, or a run of nothing but "first" asks reports twice the lookups.
        memo = memo_mod.DefinitionMemo()
        scan = Scanner(("大辞泉", "definition"))

        memo.lookup("橋", "はし", "first", scan)

        self.assertEqual((memo.computed, memo.hits, memo.coalesced), (1, 0, 0))
        self.assertIn("1 lookups asked, 1 scanned", memo.summary())

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
