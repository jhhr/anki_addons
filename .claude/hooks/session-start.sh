#!/bin/bash
# Claude Code SessionStart hook, cloud sessions only. A cloud session starts from a fresh
# clone on a stock image, where `python -m pytest` finds no `anki` and quietly runs every suite
# against the stand-in, the jp_text_processing submodule is not checked out and no addon has
# its shared/ links. This puts all of that in place, then puts the venv on the session's PATH.
# A local checkout is left alone: there the interpreter, the submodule and the junctions are
# the developer's own. What the environment itself must provide is in
# anki_shared/testing/README.md, "Cloud sessions".
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi
cd "$CLAUDE_PROJECT_DIR"

# PyQt6 loads libEGL even on the offscreen platform, and the image does not have it: without it
# pytest-anki's plugin fails to import and takes the whole run down. The environment's setup
# script is the better place for this, since its result is cached; this covers one without it.
if ! ldconfig -p | grep -q 'libEGL\.so\.1'; then
  # The image lists PPAs the network policy may refuse; the update warns about each and the
  # Ubuntu archive still serves libegl1, so only a failed install is worth hearing about.
  apt-get update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive DEBCONF_NOWARNINGS=yes \
    apt-get install -y -qq --no-install-recommends libegl1 >/dev/null
fi

# A venv, not the image's default python: that one also sees Debian's dist-packages, and pip
# cannot upgrade what Debian installed ("Cannot uninstall blinker 1.7.0, RECORD file not
# found"). 3.10 where the image has it, the version mypy.ini checks against. The path is
# hardcoded so that a setup script can build the same venv ahead of time and have the
# environment cache keep it; then the installs below find everything already there.
VENV=/opt/anki-venv
if [ ! -x "$VENV/bin/python" ]; then
  "$(command -v python3.10 || command -v python3)" -m venv "$VENV"
fi
PY="$VENV/bin/python"

# Only a submodule that was never checked out. On one that was, `git submodule update` moves it
# back to the recorded commit, and a resumed session may have been working in it.
if [ ! -e anki_shared/jp_text_processing/.git ]; then
  git submodule --quiet update --init --recursive
fi

# japanese_note_ai_ops/test loads the addon's vendored packages from lib/, which a clone does
# not have; installed here they resolve from the venv instead.
PIP=("$PY" -m pip install -q --disable-pip-version-check)
"${PIP[@]}" -r requirements-dev.txt -r japanese_note_ai_ops/requirements.txt
"${PIP[@]}" --no-deps -r requirements-dev-nodeps.txt

# The layout every developer checkout has. The tests would manage without it, but mypy would
# not: `from ..shared.x` has nothing to resolve to and the count nearly doubles.
"$PY" build.py link >/dev/null

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$VENV/bin:\$PATH\"" >>"$CLAUDE_ENV_FILE"
fi

# Hook output reaches the session as context: say which interpreter the suites run on.
"$PY" - <<'EOF'
import sys
from importlib.metadata import version

print(
    f"anki_addons cloud setup: python is {sys.executable} (Python {sys.version.split()[0]},"
    f" anki {version('anki')}, pytest-anki2 {version('pytest-anki2')}); jp_text_processing"
    " is checked out and every addon has its shared/ links"
)
EOF
