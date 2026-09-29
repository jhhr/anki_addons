"""The replay suite (issue #11, stage 3): real collections, in the root conftest's real_anki
mode, and the dev/ modules by bare name, as the dev scripts import each other. Without the real
anki package there is nothing to replay into, and the suite is not collected.
"""

from __future__ import annotations

import sys
from pathlib import Path

from anki_shared.testing import real_anki

DEV = Path(__file__).resolve().parents[1] / "dev"
if str(DEV) not in sys.path:
    sys.path.insert(0, str(DEV))

collect_ignore_glob = [] if real_anki.is_available() else ["test_*.py"]
