from typing import Any

mw: Any = None

__all__ = ["mw", "browser", "gui_hooks", "operations", "qt", "utils"]


def __getattr__(name: str) -> Any:
    return Any
