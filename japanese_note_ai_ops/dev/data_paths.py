"""Where the private test data checkout is: a clone of the test data repo, for what is made from
a real collection and so cannot go into this public repo (replay fixtures, benchmark corpora,
eval sets, hand labels, answer caches).

It is `<repo>/test_data`, gitignored, unless ANKI_ADDONS_TEST_DATA names another directory. This
addon's files are under `japanese_note_ai_ops/` in it: `fixtures/` and `corpora/` (replay.py),
`evals/` (the research scripts, `word_array/research/_bootstrap.eval_file`).

Stdlib only, and imports nothing of the addon: the research scripts load it by file path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ADDON_DIR = Path(__file__).resolve().parents[1]
DATA_ROOT_ENV = "ANKI_ADDONS_TEST_DATA"
# This addon's directory in it: the repo's name for the addon, whatever Anki's folder is called
DATA_ADDON = "japanese_note_ai_ops"
DEFAULT_ROOT = ADDON_DIR.parent / "test_data"


def data_root() -> Optional[Path]:
    """The test data checkout, or None on a machine without one."""
    configured = os.environ.get(DATA_ROOT_ENV)
    root = Path(configured) if configured else DEFAULT_ROOT
    return root if root.is_dir() else None


def data_dir(kind: str) -> Optional[Path]:
    """`fixtures`, `corpora` or `evals` of this addon in the test data checkout, if there is
    one."""
    root = data_root()
    return root / DATA_ADDON / kind if root is not None else None
