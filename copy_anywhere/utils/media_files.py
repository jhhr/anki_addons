"""Reading and writing files in the media folder for format-2 file stages.

`write_to_media_folder()` opens in text mode, so on Windows every "\\n" it writes becomes
"\\r\\n". That is harmless when a file is only ever written, but format 2 lets a definition
read a file, change part of it and write it back (§5.4), and a round trip that rewrites every
line ending is not a round trip. These helpers open with `newline=""` in both directions and
use UTF-8 without a BOM, so the bytes that come back are the bytes that went in apart from
what the definition changed.

The filename rules are shared by reads and writes: a name resolves inside the media folder,
and a path separator or a `..` segment is refused rather than normalised, so a definition
cannot reach out of the folder.
"""

from pathlib import Path
from typing import Optional

from aqt import mw

MEDIA_FOLDER_NAME = "collection.media"


class MediaFileError(ValueError):
    """A filename that does not name a file inside the media folder."""


def media_folder() -> Path:
    return Path(mw.pm.profileFolder(), MEDIA_FOLDER_NAME)


def normalize_media_filename(filename: str) -> str:
    """The stored name for `filename`, or raise if it does not name one file in the folder.

    The leading underscore is what `write_to_media_folder()` has always added: it marks the
    file as one the addon owns, which keeps Anki's unused-media check from offering to
    delete it.
    """
    if not filename or not filename.strip():
        raise MediaFileError("Filename must not be empty")
    name = filename.strip()
    if "/" in name or "\\" in name:
        raise MediaFileError(f"Filename '{filename}' must not contain a path separator")
    if name in (".", "..") or ".." in Path(name).parts:
        raise MediaFileError(f"Filename '{filename}' must not contain a '..' segment")
    if not name.startswith("_"):
        name = f"_{name}"
    return name


def media_file_path(filename: str) -> Path:
    path = (media_folder() / normalize_media_filename(filename)).resolve()
    folder = media_folder().resolve()
    if folder != path.parent:
        # Belt and braces: normalize_media_filename already refuses separators, so reaching
        # here means the resolved path escaped some other way (a symlinked name, say).
        raise MediaFileError(f"Filename '{filename}' resolves outside the media folder")
    return path


def media_file_exists(filename: str) -> bool:
    return media_file_path(filename).exists()


def read_media_file(filename: str) -> Optional[str]:
    """The file's text, or None when it does not exist. Invalid UTF-8 raises."""
    path = media_file_path(filename)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8", newline="") as file:
        return file.read()


def write_media_file(filename: str, text: str) -> None:
    path = media_file_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(text)
