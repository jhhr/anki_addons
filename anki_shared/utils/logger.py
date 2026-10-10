"""The log level names an addon's config may hold, and what they mean to `logging`.

The hand-rolled `Logger` that used to live here is gone. It predated the addons logging
through the standard library, and it carried two problems with it: a default argument
`Logger("error")` on every signature that wanted one, so every caller shared one instance and
setting a level in one place changed it everywhere; and a `log` callable per instance, which
made "where does this line go" a property of the call chain rather than of the application.

`logging` answers both -- the level and the handlers belong to the logger, not to the
argument threaded through the call -- so all that is left to share is the mapping from the
names a config file is allowed to use to the levels `logging` understands.
"""

import logging
from typing import Literal

LogLevel = Literal["error", "warning", "info", "debug"]

LOG_LEVELS: dict[str, int] = {
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def log_level_to_int(level: str) -> int:
    """The `logging` level a config's level name asks for; unknown names log errors only."""
    return LOG_LEVELS.get(level, logging.ERROR)
