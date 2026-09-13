"""The data the generator needs that the add-on can't ship, downloaded on first use.

- Sudachi's system dictionary. sudachidict_core is a 72 MB wheel unpacking to ~200 MB, far
  past what an .ankiaddon can carry, so it isn't in requirements.txt: the wheel is fetched
  from PyPI at a pinned version and hash and only its system.dic is kept. (SudachiPy itself,
  ~1.5 MB per platform, is vendored like the other packages.)
- JMdict_e, 11 MB, from EDRDG. Not pinned: it is updated daily and any recent copy will do.
  The lookup index is built from it once (~15 s) and pickled.

Both go under user_files/, the only directory Anki keeps across an add-on update.

Nothing here downloads by itself. Whatever runs the generator asks the user first - missing()
says what is needed and how big it is - and then calls ensure() off the main thread.
"""

import hashlib
import importlib.util
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import jmdict_index

USER_FILES = Path(__file__).resolve().parent.parent / "user_files"
SUDACHI_DIR = USER_FILES / "sudachi"
SUDACHI_DIC = SUDACHI_DIR / "system_core.dic"
# Built from the collection's own sentences (names.build_lexicon), not downloaded
NAME_LEXICON = USER_FILES / "name_lexicon.json"

# The core and full dictionaries score the same on the gold examples; core is ~60% the size
SUDACHI_DICT_VERSION = "20260723"
SUDACHI_DICT_URL = (
    "https://files.pythonhosted.org/packages/46/fe/"
    "68a146fced55319af40d25a4fe19b94c3a988406ce4674a8b2f0237fbc9f/"
    f"sudachidict_core-{SUDACHI_DICT_VERSION}-py3-none-any.whl"
)
SUDACHI_DICT_SHA256 = "b3869ce6b12b4bfa09575dc19030703bb669ab41bac12a74cafcbb28c6be2498"
SUDACHI_DICT_MEMBER = "sudachidict_core/resources/system.dic"

# Dictionaries installed as packages, for development; the add-on uses SUDACHI_DIC
INSTALLED_SUDACHI_DICTS = ("core", "full", "small")

_CHUNK = 1 << 20

Progress = Callable[[str], None]


class ResourcesMissing(RuntimeError):
    """The generator was called before ensure() had fetched what it needs."""


@dataclass(frozen=True)
class Download:
    name: str
    size_mb: float


SUDACHI_DOWNLOAD = Download(f"Sudachi dictionary (sudachidict_core {SUDACHI_DICT_VERSION})", 72)
JMDICT_DOWNLOAD = Download("JMdict (EDRDG Japanese-English dictionary)", 11)


def has_sudachipy() -> bool:
    return importlib.util.find_spec("sudachipy") is not None


def sudachi_dictionary() -> Optional[str]:
    """What to pass as Dictionary(dict=...): the SUDACHI_DICT environment variable (a
    dictionary name or an absolute path), the downloaded system.dic, or an installed
    sudachidict_* package. None when there is none of these."""
    if os.environ.get("SUDACHI_DICT"):
        return os.environ["SUDACHI_DICT"]
    if SUDACHI_DIC.exists():
        return str(SUDACHI_DIC)
    for name in INSTALLED_SUDACHI_DICTS:
        if importlib.util.find_spec(f"sudachidict_{name}") is not None:
            return name
    return None


def missing() -> list[Download]:
    """The downloads ensure() would make."""
    out = []
    if sudachi_dictionary() is None:
        out.append(SUDACHI_DOWNLOAD)
    if not jmdict_index.is_available():
        out.append(JMDICT_DOWNLOAD)
    return out


def is_ready() -> bool:
    return has_sudachipy() and not missing()


def ensure(on_progress: Progress = lambda _: None) -> None:
    """Download whatever is missing and build the JMdict index. Blocking; raises on failure,
    leaving nothing half-written behind."""
    if sudachi_dictionary() is None:
        _fetch_sudachi_dictionary(on_progress)
    if not jmdict_index.is_available():
        _download(jmdict_index.JMDICT_URL, jmdict_index.JMDICT_GZ, JMDICT_DOWNLOAD, on_progress)
    on_progress("Indexing JMdict...")
    jmdict_index.index()


def _fetch_sudachi_dictionary(on_progress: Progress) -> None:
    SUDACHI_DIR.mkdir(parents=True, exist_ok=True)
    wheel = SUDACHI_DIR / f"sudachidict_core-{SUDACHI_DICT_VERSION}.whl"
    try:
        _download(SUDACHI_DICT_URL, wheel, SUDACHI_DOWNLOAD, on_progress, SUDACHI_DICT_SHA256)
        on_progress("Unpacking the Sudachi dictionary...")
        with zipfile.ZipFile(wheel) as z, z.open(SUDACHI_DICT_MEMBER) as src:
            _write_atomically(SUDACHI_DIC, lambda dst: shutil.copyfileobj(src, dst, _CHUNK))
    finally:
        wheel.unlink(missing_ok=True)


def _download(
    url: str,
    dest: Path,
    what: Download,
    on_progress: Progress,
    sha256: Optional[str] = None,
) -> None:
    digest = hashlib.sha256()

    def fetch(dst) -> None:
        with urllib.request.urlopen(url, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(_CHUNK):
                dst.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                size = f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total else f"{done / 1e6:.0f} MB"
                on_progress(f"Downloading {what.name}: {size}")
        if sha256 and digest.hexdigest() != sha256:
            raise OSError(f"{url} did not match its expected sha256; not using it")

    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(dest, fetch)


def _write_atomically(dest: Path, write: Callable) -> None:
    """Write through a temporary file beside dest, renamed into place only when complete."""
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as dst:
            write(dst)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
