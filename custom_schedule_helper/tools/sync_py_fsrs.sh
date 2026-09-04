#!/usr/bin/env bash
#
# Sync the vendored py_fsrs/ tree to a newer upstream py-fsrs release.
#
# py_fsrs/ is a trimmed, locally modified copy of upstream, not a submodule. See
# py_fsrs/README.md for what is vendored and the local modifications you must reconcile.
#
# Usage:
#   tools/sync_py_fsrs.sh              # sync to the latest upstream tag
#   tools/sync_py_fsrs.sh v6.4.0       # sync to a specific tag
#   tools/sync_py_fsrs.sh --list       # just show available upstream tags
#
set -euo pipefail

UPSTREAM="https://github.com/open-spaced-repetition/py-fsrs.git"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="$REPO_ROOT/py_fsrs"
VENDOR_DOC="$VENDOR_DIR/README.md"

# Paths that make up the vendored set. The diff is restricted to these so that files outside
# it (.github/, tests/, pyproject.toml, fsrs/optimizer.py, fsrs/py.typed, ...) are never
# created here.
VENDORED_PATHS=(fsrs LICENSE)
EXCLUDED_PATHS=(':(exclude)fsrs/optimizer.py' ':(exclude)fsrs/py.typed')

die() { echo "error: $*" >&2; exit 1; }

[ -d "$VENDOR_DIR" ] || die "no py_fsrs/ directory at $VENDOR_DIR"
[ -f "$VENDOR_DOC" ] || die "no py_fsrs/README.md -- is this the right repo?"

# The currently vendored version is recorded in py_fsrs/README.md; it is the diff base.
CURRENT="$(grep -oE '\*\*v[0-9]+\.[0-9]+\.[0-9]+\*\*' "$VENDOR_DOC" | head -1 | tr -d '*')"
[ -n "$CURRENT" ] || die "could not read the vendored version from $VENDOR_DOC"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Fetching upstream py-fsrs..."
git clone --quiet --bare "$UPSTREAM" "$WORK/upstream.git"
UG=(git --git-dir="$WORK/upstream.git")

if [ "${1:-}" = "--list" ]; then
    echo "Currently vendored: $CURRENT"
    echo "Available upstream tags (newest first):"
    "${UG[@]}" tag --sort=-v:refname | head -20
    exit 0
fi

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    TARGET="$("${UG[@]}" tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
    echo "No target given; using latest upstream tag: $TARGET"
fi

"${UG[@]}" rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null \
    || die "unknown upstream ref: $TARGET"

if [ "$CURRENT" = "$TARGET" ]; then
    echo "Already vendoring $TARGET -- nothing to do."
    exit 0
fi

echo
echo "Syncing vendored py_fsrs: $CURRENT -> $TARGET"
echo
echo "Upstream changes in this range:"
"${UG[@]}" log --oneline --no-decorate "$CURRENT..$TARGET" | sed 's/^/  /'
echo
echo "Changes to vendored paths only:"
"${UG[@]}" diff --stat "$CURRENT" "$TARGET" -- "${VENDORED_PATHS[@]}" "${EXCLUDED_PATHS[@]}" \
    | sed 's/^/  /'
echo
echo "(Changes outside ${VENDORED_PATHS[*]} are ignored by design -- see py_fsrs/README.md.)"
echo

# Upstream-to-upstream diff, applied on top of the vendored tree. --3way means anything that
# collides with a local modification is left in the working tree with conflict markers rather
# than silently dropped.
"${UG[@]}" diff "$CURRENT" "$TARGET" -- "${VENDORED_PATHS[@]}" "${EXCLUDED_PATHS[@]}" \
    > "$WORK/upstream.patch"

if [ ! -s "$WORK/upstream.patch" ]; then
    echo "No changes to vendored paths between $CURRENT and $TARGET."
    echo "Just update the version rows in py_fsrs/README.md to $TARGET."
    exit 0
fi

cd "$REPO_ROOT"
echo "Applying to py_fsrs/ (3-way)..."
set +e
git apply --3way --directory=py_fsrs "$WORK/upstream.patch"
APPLY_STATUS=$?
set -e

if [ $APPLY_STATUS -eq 0 ]; then
    echo "Applied cleanly."
else
    echo "Applied with conflicts -- resolve the conflict markers in py_fsrs/ before committing."
fi

# ---------------------------------------------------------------------------
# Regression check. tests/ is not vendored (it would ship in the addon zip), so pull
# upstream's suite at the target version into a temp dir and run it against the vendored
# package.
# ---------------------------------------------------------------------------
echo
if ! python -c "import pytest" >/dev/null 2>&1; then
    echo "Skipping tests: pytest is not installed in this python."
else
    echo "Running upstream's test suite at $TARGET against the vendored fsrs/ ..."
    ISO="$WORK/iso"
    mkdir -p "$ISO"
    "${UG[@]}" archive "$TARGET" | tar -x -C "$ISO"

    # Swap upstream's package for ours, and drop the optimizer test (we do not vendor it).
    rm -rf "$ISO/fsrs"
    cp -r "$VENDOR_DIR/fsrs" "$ISO/fsrs"
    rm -rf "$ISO/fsrs/__pycache__" "$ISO/tests/test_optimizer.py"

    set +e
    ( cd "$ISO" && python -m pytest tests/test_basic.py -q -p no:cacheprovider 2>&1 | tail -12 )
    set -e
    cat <<'EOF'

Four failures are expected and documented in py_fsrs/README.md:
  test_retrievability, test_memo_state, test_Optimizer_lazy_loading  (by design)
  test_class___eq___methods                                          (upstream flake)
Anything beyond those four is a real regression.
EOF
fi

cat <<EOF

Next steps:
  1. Reconcile against the local modifications in py_fsrs/README.md. In particular re-check,
     in py_fsrs/fsrs/scheduler.py:
       - imports are still relative (upstream writes them absolute)
       - get_card_retrievability still returns 1 for an unreviewed card, and fractional days
       - _next_difficulty still takes retrievability, at every call site
       - review_card still hoists initialisation out of the state match, and falls through
       - the card.step None guard survived
  2. Update the "Vendored version" and "Last synced" rows in py_fsrs/README.md to $TARGET.
EOF
