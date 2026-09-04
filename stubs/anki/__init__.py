from typing import Any

__all__ = [
    "cards",
    "collection",
    "consts",
    "decks",
    "errors",
    "hooks",
    "models",
    "notes",
    "stats",
    "utils",
]


def __getattr__(name: str) -> Any:
    return Any
