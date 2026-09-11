"""Makes this directory a package so its `conftest.py` is not a second top-level `conftest`.

Without it, pytest imports every non-package test directory's `conftest.py` under the bare
module name `conftest`, and the last one imported wins: collecting this suite alongside
`copy_anywhere/test` would leave that suite's `from conftest import VOCAB, ...` resolving to
*this* file. `copy_anywhere` is already a package -- the root conftest registers it as a
stub whose `__path__` is the real directory, so importing this subpackage never runs the
addon's own `__init__.py` -- so the name becomes `copy_anywhere.test_anki.conftest` instead.
"""
