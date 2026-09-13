# Chain log
review-fixes task-1 finished 2026-09-13T05:17:10Z — finding 1 fixed (5a69655): meaning_group_note_ids now falls back to the search when either word value is empty
review-fixes task-2 finished 2026-09-13T06:15:00Z — finding 2 fixed (ff47058): an MDX outage no longer becomes a permanent NO_DICTIONARY_ENTRY_TAG
review-fixes task-3 finished 2026-09-13T05:32:54Z — findings 3+5 closed in 557e0cc: stale user_files/lib no longer shadows a healthy shipped lib, per-platform copy guarded, stale manifest dropped with the tree
review-fixes task-4 finished 2026-09-13T07:05:00Z — finding 4 fixed (9fb93b8): a run blends its measurement into the stored estimate once, however many times it persists
review-fixes task-5 finished 2026-09-13T05:49:10Z — findings 6+7 fixed in 0f2a376 (one-pass dedup helper, word_list_dict bound first) with 14 new tests; queue now holds one [USER] question raised by this task
CHAIN PAUSED — waiting on user: whether bulk_op's word list deduplication should write the deduplicated lists back to the note (a behaviour change) or be deleted as dead code. All seven review findings are fixed; this is the only queue entry left.
CHAIN RESUMED — user chose: write the deduplicated word lists back to the note. Queued for task-6.
