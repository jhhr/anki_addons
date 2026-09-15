"""The sentences a note's word appears in, looked up once per note rather than once per ask.

`get_sentences_for_note` asks the collection which *other* notes list this note's id in their
`sentence-vocab-list` field. That is a leading-wildcard match on a note field, so it is a
whole-collection scan by the same argument as every other one here - 0.389s measured, and no
index exists or can exist for it.

It is not asked often, but it is asked about the same handful of notes over and over. Three
runs, counted:

    round 6      26 scans over  17 distinct note ids   1.53x
    round 7     202 scans over  66 distinct note ids   3.06x
    round 8     257 scans over  60 distinct note ids   4.28x
    round 9     148 scans over  44 distinct note ids   3.36x   (a different machine)

The distinct set stays small while the scans scale with the workload, so 77% of the work is
re-asking a question the run has already answered: 197 of round 8's 257 scans, 76.6s, and 77%
of the only whole-collection scan left anywhere on the hot path once the meaning-group query
moved to the word index.

The earlier reading of this closed it as "not worth an index", and that was true - it was also
answering the wrong question. What it costed was an inverted index (note id -> the notes whose
list mentions it), which is real work for 4% of a run. Counting the *distinct arguments* first
says the fix is a dict. The lesson generalises, and is the same one rounds 1-5 taught: count
distinct arguments before costing an index.

Sound for the same reason as `word_index.py` and `note_cache.py`: a run does not write to the
collection. Every update happens in base_ops' cleanup phase after the ops have finished, so
which notes mention a note id cannot change while the run is going.

Two things beyond a plain dict:

- **What is memoised is the other notes' sentences, not the answer.** The two call shapes
  differ by `exclude_self`, which decides only whether the asking note's own sentence goes on
  the front. Memoising the returned value would have the two callers poison each other.
- **Single flight**, because the callers arrive from `asyncio.to_thread` and several hundred
  tasks are in flight: two tasks that want the same note id would otherwise both scan. The
  waiter blocks on the leader's lock rather than starting a second 0.389s pass.
"""

import logging
import threading
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from anki.notes import NoteId

    from ..configuration import EnAndJPSentence

logger = logging.getLogger(__name__)

# How often to log the running hit rate, in asks. INFO so the rate shows up in a run logged at
# any level, the way the MDX memo's line and the note cache's do.
REPORT_EVERY = 50


class SentenceCache:
    """The other-note sentences for each note id this run has asked about.

    Locked, unlike `NoteCache`: its callers are pool threads rather than coroutines, and the
    thing being guarded is not the dict but the scan - two threads finding the key absent at
    the same moment is exactly the duplicate whole-collection pass this exists to remove.
    """

    __slots__ = ("_sentences", "_locks", "_lock", "asked", "scanned", "_next_report")

    def __init__(self) -> None:
        self._sentences: "dict[NoteId, list[EnAndJPSentence]]" = {}
        # One lock per note id, created under `_lock`. Held across the scan, so a second asker
        # for the same id waits for the answer instead of going to the collection for its own.
        self._locks: "dict[NoteId, threading.Lock]" = {}
        self._lock = threading.Lock()
        self.asked = 0
        self.scanned = 0
        self._next_report = REPORT_EVERY

    def __len__(self) -> int:
        return len(self._sentences)

    def get(
        self,
        note_id: "NoteId",
        scan: "Callable[[], list[EnAndJPSentence]]",
    ) -> "list[EnAndJPSentence]":
        """The other notes' sentences for `note_id`, scanning at most once per id.

        `scan` runs on the calling thread and outside `_lock`, so it is free to take a turn
        with the collection and as long as that turn takes.
        """
        with self._lock:
            self.asked += 1
            cached = self._sentences.get(note_id)
            report = self._should_report()
            key_lock = self._locks.get(note_id)
            if key_lock is None:
                key_lock = self._locks.setdefault(note_id, threading.Lock())
        if report:
            self._report()
        if cached is not None:
            return cached

        with key_lock:
            # Re-checked under the per-id lock: the thread that held it before this one was
            # very likely computing exactly this answer.
            cached = self._sentences.get(note_id)
            if cached is not None:
                return cached
            sentences = scan()
            with self._lock:
                self.scanned += 1
                self._sentences[note_id] = sentences
            return sentences

    def _should_report(self) -> bool:
        """Whether this ask is the one that logs. Caller holds the lock."""
        if self.asked < self._next_report:
            return False
        self._next_report = self.asked + REPORT_EVERY
        return True

    def _report(self) -> None:
        avoided = self.asked - self.scanned
        logger.info(
            "Sentence cache: %d asked, %d scanned, %d from cache (%.1f%% avoided),"
            " %d notes held",
            self.asked,
            self.scanned,
            avoided,
            100 * avoided / self.asked if self.asked else 0.0,
            len(self._sentences),
        )
