from __future__ import annotations


try:
    from .addon import init_addon

    init_addon()
except ModuleNotFoundError as exc:
    if exc.name not in {"aqt", "anki"}:
        raise
