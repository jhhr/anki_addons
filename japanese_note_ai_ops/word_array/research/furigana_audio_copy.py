"""Copy the picked cards' audio clips into the test data checkout, from the folder subs2srs wrote
them to, so that every machine and cloud session that runs the furigana audio eval has them.

Run it where the subs2srs output is, then commit and push `evals/furigana_audio/audio/` in the
test data repo. A clip already there is left alone, so it can run again after a new pick; one
no longer picked is listed, never deleted. The clips are found by name anywhere under
`--media`, since subs2srs's output folder layout depends on its settings.

    python word_array/research/furigana_audio_copy.py --media "<subs2srs output folder>"
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

import furigana_audio as fa


def find_clips(media: Path, names: set[str]) -> dict[str, Path]:
    """Each wanted file name's path under `media`, the first found where two folders have it."""
    found: dict[str, Path] = {}
    for path in sorted(media.rglob("*")):
        if path.name in names and path.name not in found and path.is_file():
            found[path.name] = path
    return found


def copy_clips(selection: Sequence[dict], media: Path, dest: Path) -> tuple[int, int, list[str]]:
    """(copied, already there, missing file names)."""
    wanted = {row["audio"] for row in selection}
    have = {p.name for p in dest.glob("*")} if dest.is_dir() else set()
    found = find_clips(media, wanted - have)
    dest.mkdir(parents=True, exist_ok=True)
    for name, path in sorted(found.items()):
        shutil.copy2(path, dest / name)
    missing = sorted(wanted - have - set(found))
    return len(found), len(wanted & have), missing


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--media", type=Path, required=True, help="the subs2srs output folder")
    args = ap.parse_args(argv)
    if not args.media.is_dir():
        print(f"{args.media} is no folder", file=sys.stderr)
        return 2
    selection = fa.read_jsonl(fa.SELECTION)
    if not selection:
        print(f"{fa.SELECTION} is missing or empty: run furigana_audio_select.py", file=sys.stderr)
        return 2
    copied, kept, missing = copy_clips(selection, args.media, fa.AUDIO)
    print(f"{copied} clips copied, {kept} already in {fa.AUDIO}")
    for name in missing:
        print(f"missing: {name}", file=sys.stderr)
    stale = sorted({p.name for p in fa.AUDIO.glob("*")} - {row["audio"] for row in selection})
    if stale:
        print(f"{len(stale)} clips there are no longer picked (left in place): {stale[:5]}...")
    size = sum(p.stat().st_size for p in fa.AUDIO.glob("*"))
    print(f"{fa.AUDIO} holds {size / 1e6:.1f} MB")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
