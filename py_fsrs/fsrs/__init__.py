"""
py_fsrs
-------

Py-FSRS is the official Python implementation of the FSRS scheduler algorithm, which can be used to
develop spaced repetition systems.

Vendored fork note: imports are relative so this package works when nested inside the Anki addon
(as ``py_fsrs.fsrs``), and the Optimizer is omitted because it depends on torch.
"""

from .card import Card
from .rating import Rating
from .review_log import ReviewLog
from .scheduler import Scheduler, DEFAULT_PARAMETERS
from .state import State

__all__ = [
    "Card",
    "DEFAULT_PARAMETERS",
    "Rating",
    "ReviewLog",
    "Scheduler",
    "State",
]
