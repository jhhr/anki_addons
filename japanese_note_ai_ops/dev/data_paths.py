"""Where the private test data checkout is: a clone of the test data repo, for what is made from
a real collection and so cannot go into this public repo (replay fixtures, benchmark corpora,
eval sets, hand labels, answer caches).

It is `<repo>/test_data`, gitignored, unless ANKI_ADDONS_TEST_DATA names another directory, which
must then exist (a relative one is taken from the repo root). A linked git worktree without one
of its own uses its main checkout's (`main_checkout`). This addon's files are under
`japanese_note_ai_ops/` in it: `fixtures/` and `corpora/` (replay.py), `evals/` (the research
scripts, `word_array/research/_bootstrap.eval_file`).

Stdlib only, and imports nothing of the addon: the research scripts load it by file path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ADDON_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ADDON_DIR.parent
DATA_ROOT_ENV = "ANKI_ADDONS_TEST_DATA"
# This addon's directory in it: the repo's name for the addon, whatever Anki's folder is called
DATA_ADDON = "japanese_note_ai_ops"
DEFAULT_ROOT = REPO_ROOT / "test_data"


def data_root() -> Optional[Path]:
    """The test data checkout, or None on a machine without one.

    Raises FileNotFoundError when ANKI_ADDONS_TEST_DATA is set to something that is not a
    directory: read as "no checkout", a mistyped one skipped every private fixture and sent
    the research scripts' eval data to output/, without a word."""
    configured = os.environ.get(DATA_ROOT_ENV)
    if configured:
        # An absolute value replaces the root; the scripts and pytest start in different cwds
        root = REPO_ROOT / configured
        if not root.is_dir():
            raise FileNotFoundError(
                f"{DATA_ROOT_ENV} is {configured!r}, and {root} is not a directory: clone the"
                " test data repo there, or unset it"
            )
        return root
    if DEFAULT_ROOT.is_dir():
        return DEFAULT_ROOT
    # A linked worktree (a Claude Code session's, under .claude/worktrees/) has no test_data of
    # its own, being a gitignored clone: without this its sessions skipped every private fixture
    # and wrote the research scripts' paid answers into an output/ deleted with the worktree
    main = main_checkout(REPO_ROOT)
    if main is not None and (main / "test_data").is_dir():
        return main / "test_data"
    return None


def main_checkout(repo: Path) -> Optional[Path]:
    """The main working tree of `repo` when `repo` is a linked worktree of it, else None.

    A linked worktree's `.git` is a file, `gitdir: <main>/.git/worktrees/<name>`, and that
    directory's `commondir` leads to the main one's `.git`. A submodule's `.git` is a file too,
    but its gitdir has no `commondir`."""
    dot_git = repo / ".git"
    try:
        if not dot_git.is_file():
            return None
        line = dot_git.read_text(encoding="utf-8").strip()
        if not line.startswith("gitdir:"):
            return None
        # Absolute as git writes it; a relative one is from the worktree
        gitdir = repo / line[len("gitdir:"):].strip()
        commondir = gitdir / "commondir"
        if not commondir.is_file():
            return None
        return (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve().parent
    except OSError:
        return None


def data_dir(kind: str) -> Optional[Path]:
    """`fixtures`, `corpora` or `evals` of this addon in the test data checkout, if there is
    one."""
    root = data_root()
    return root / DATA_ADDON / kind if root is not None else None
