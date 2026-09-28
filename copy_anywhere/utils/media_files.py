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
    """A filename that does not name a file inside the media folder, or one that cannot be
    read."""


def file_key(name: str) -> str:
    """What makes two names one file: Windows and macOS do not tell `_Foo.txt` from
    `_foo.txt`, so a run that checked names as typed let a write that must not overwrite
    replace a file it had written itself under the other case."""
    return name.casefold()


def media_folder() -> Path:
    return Path(mw.pm.profileFolder(), MEDIA_FOLDER_NAME)


#: What Windows refuses in a file name, or reads as something else: `:` names an alternate
#: data stream of the file before it, so `a:b.txt` wrote a hidden stream of an empty `a`.
#: Refused on every system, so a definition that runs on one runs on all, and the preview
#: -- which writes nothing -- refuses what the real run would fail on.
_REFUSED_CHARACTERS = frozenset('<>:"|?*')


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
    refused = sorted(
        {char for char in name if char in _REFUSED_CHARACTERS or ord(char) < 32 or char == "\x7f"}
    )
    if refused:
        shown = " ".join(char if char.isprintable() else repr(char) for char in refused)
        raise MediaFileError(f"Filename '{filename}' must not contain {shown}")
    if name.endswith("."):
        # Windows drops a trailing dot, so `a.` and `a` would be one file there.
        raise MediaFileError(f"Filename '{filename}' must not end in '.'")
    if not name.startswith("_"):
        name = f"_{name}"
    return name


def _unusable(filename: str, error: Exception) -> MediaFileError:
    """What the file system said about `filename`, as the error a file stage reports.

    A name made from note fields can be anything: longer than the file system allows, or
    holding a character it refuses. Its OSError or ValueError went past the stages' error
    handling, which only knows this class, and aborted the whole run with a traceback.
    """
    reason = getattr(error, "strerror", None) or str(error)
    return MediaFileError(f"File '{filename}' cannot be used: {reason}")


def media_file_path(filename: str) -> Path:
    name = normalize_media_filename(filename)
    try:
        path = (media_folder() / name).resolve()
        folder = media_folder().resolve()
    except (OSError, ValueError) as error:
        raise _unusable(name, error) from error
    if folder != path.parent:
        # Belt and braces: normalize_media_filename already refuses separators, so reaching
        # here means the resolved path escaped some other way (a symlinked name, say).
        raise MediaFileError(f"Filename '{filename}' resolves outside the media folder")
    return path


def media_file_exists(filename: str) -> bool:
    path = media_file_path(filename)
    try:
        return path.exists()
    except OSError as error:
        raise _unusable(path.name, error) from error


def read_media_file(filename: str) -> Optional[str]:
    """The file's text, or None when it does not exist. Invalid UTF-8 raises."""
    path = media_file_path(filename)
    if not media_file_exists(path.name):
        return None
    if path.is_dir():
        raise MediaFileError(f"'{path.name}' is a folder in the media folder, not a file")
    try:
        with open(path, "r", encoding="utf-8", newline="") as file:
            return file.read()
    except OSError as error:
        raise MediaFileError(
            f"File '{path.name}' could not be read: {error.strerror or error}"
        ) from error


def write_media_file(filename: str, text: str) -> None:
    path = media_file_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(text)
