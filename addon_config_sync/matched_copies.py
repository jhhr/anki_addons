"""Which media copies are still what this device's configs last matched.

`read_configs_on_sync` runs whenever a media sync stops. A config that differs from its copy
in the media folder then looks the same whether another device sent a new copy or this device
changed its config and has not sent it yet: only a collection sync starting sends
(`save_configs_on_sync`), and Anki also syncs media by itself, every 15 minutes by default,
and after a media sync that failed the hook fires as well. Copying the media copy back in
both cases undid every config change that a media sync ended on before a collection sync had
started.

What tells the two apart is whether the media copy has changed since the two files last
matched. So each time a config and its media copy are seen to match, the copy's sha256 is
kept here. A copy that still has it has had nothing downloaded over it, and a difference
from it is this device's own. That is all this file knows; which of this device's own
differences are then left alone is `read_configs_on_sync`'s to say.

The bytes are hashed, not the JSON in them: a copy rewritten to the same content reads as
changed, which is the careful side to err on.

Kept per profile folder, because a media folder is one profile's while an add-on's config is
the same file for all of them, and in `user_files/`, which Anki carries over when it updates
the add-on. Not in this add-on's own config: that is one of the files being synced, and what
a media copy last matched is true of one device only.

A held digest can only ever equal a copy that is what it was at some match, so nothing
kept here can make a copy with new content pass for an old one. What can go wrong is a
match that is not held, which means copying as before, or one that outlives the config it
was made for, as when an add-on is deleted and installed again between two syncs. That is
why `sync_actions.is_change_made_here` looks at what the two files hold and a held match is
never the whole answer.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

VERSION = 1


def state_file() -> Path:
    return Path(__file__).parent / "user_files" / "matched_copies.json"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_state() -> dict[str, dict[str, str]]:
    """Each profile's digests by add-on id; nothing for a file that is not what `save` writes."""
    try:
        with open(state_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {
        profile: {addon_id: held for addon_id, held in copies.items() if isinstance(held, str)}
        for profile, copies in profiles.items()
        if isinstance(copies, dict)
    }


class MatchedCopies:
    """The state file as it is now, seen from one profile; `save` writes what changed.

    `profile` is the profile's folder, not its name: two Anki data folders can share an
    add-on's folder, and so this file, with a profile of the same name in each."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self._profiles = _read_state()
        self._changed = False

    def unchanged(self, addon_id: str, media_meta_json: Path) -> bool:
        """Whether this copy is, byte for byte, the one the add-on's config last matched."""
        held = self._profiles.get(self.profile, {}).get(addon_id)
        if held is None:
            return False
        try:
            return held == digest(media_meta_json.read_bytes())
        except OSError:
            return False

    def record(self, addon_id: str, media_copy: bytes) -> None:
        """The add-on's config matches a media copy of these bytes. The bytes, not the file:
        the caller has compared what it read, and the file may be another by now."""
        current = digest(media_copy)
        copies = self._profiles.setdefault(self.profile, {})
        if copies.get(addon_id) != current:
            copies[addon_id] = current
            self._changed = True

    def forget(self, addon_id: str) -> None:
        """Nothing is known of this profile's copy of the add-on's config any more."""
        if self._profiles.get(self.profile, {}).pop(addon_id, None) is not None:
            self._changed = True

    def keep_only(self, with_config: set[str]) -> None:
        """Drop, in every profile, the add-ons that have no meta.json any more."""
        for copies in self._profiles.values():
            for addon_id in list(copies):
                if addon_id not in with_config:
                    del copies[addon_id]
                    self._changed = True

    def save(self) -> None:
        if not self._changed:
            return
        path = state_file()
        data = {
            "version": VERSION,
            "profiles": {profile: copies for profile, copies in self._profiles.items() if copies},
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written beside the file and moved over it, so a write cut short leaves the old
            # state and not half of a new one
            unfinished = path.with_name(path.name + ".tmp")
            with open(unfinished, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(unfinished, path)
        except OSError:
            # Callers are Anki's sync hooks, and an exception out of one stops the sync. The
            # file then stays as it was, which the module docstring says is safe to read.
            return
        self._changed = False
