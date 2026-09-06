from aqt.gui_hooks import sync_will_start, sync_did_finish

from ..configuration import Config
from ..logic.copy_fields import copy_fields
from ..shared.notify import post

SOURCE = "Copy Anywhere"

# One entry per sync, replaced in place: the local pass reports as soon as it
# lands and the remote pass overwrites that same entry rather than leaving two
# behind. `group` puts it alongside whatever the other addons post this sync.
KEY = "sync"
GROUP = "sync"


class SyncResult:
    def __init__(self):
        self.local_changes_text = ""
        self.remote_changes_text = ""
        self.definitions_count = 0

    def has_changes(self):
        return bool(self.local_changes_text or self.remote_changes_text)

    def incr_count(self, count: int):
        self.definitions_count += count

    def clear(self):
        self.local_changes_text = ""
        self.remote_changes_text = ""
        self.definitions_count = 0

    def title(self) -> str:
        if self.local_changes_text and self.remote_changes_text:
            which = "local and remote changes"
        elif self.local_changes_text:
            which = "local changes"
        else:
            which = "remote changes"
        return f"Copied fields for {which}"

    def body(self) -> str:
        parts = []
        if self.local_changes_text:
            parts.append(f"<b>Local changes:</b><br>{self.local_changes_text}")
        if self.remote_changes_text:
            parts.append(f"<b>Remote changes:</b><br>{self.remote_changes_text}")
        return "<br><br>".join(parts)


def show_result(sync_result: SyncResult, clear: bool = True) -> None:
    if sync_result.has_changes():
        post(
            source=SOURCE,
            title=sync_result.title(),
            body=sync_result.body(),
            level="success",
            # Longer when more was copied, since there is more to read.
            timeout_ms=5000 + sync_result.definitions_count * 1000,
            group=GROUP,
            key=KEY,
        )
    if clear:
        # Start the next sync from a clean slate.
        sync_result.clear()


def _copy_on_sync_definitions():
    config = Config()
    config.load()
    return [
        definition
        for definition in config.copy_definitions
        if definition.get("copy_on_sync", False)
    ]


def local_changes_copy_definitions(sync_result: SyncResult) -> None:
    copy_on_sync_definitions = _copy_on_sync_definitions()
    if not copy_on_sync_definitions:
        return

    def update_local_sync_result(text: str, count: int):
        if text:
            sync_result.local_changes_text = text
        sync_result.incr_count(count)
        # Report now; the remote pass replaces this entry under the same key.
        show_result(sync_result, clear=False)

    copy_fields(
        copy_definitions=copy_on_sync_definitions,
        update_sync_result=update_local_sync_result,
        progress_title="Copying fields for local changes",
    )


def remote_changes_copy_definitions(sync_result: SyncResult) -> None:
    copy_on_sync_definitions = _copy_on_sync_definitions()
    if not copy_on_sync_definitions:
        show_result(sync_result)
        return

    def update_remote_sync_result(text: str, count: int):
        if text:
            sync_result.remote_changes_text = text
        sync_result.incr_count(count)

    copy_fields(
        copy_definitions=copy_on_sync_definitions,
        update_sync_result=update_remote_sync_result,
        on_done=lambda: show_result(sync_result),
        progress_title="Copying fields for remote changes",
    )


def init_sync_hook():
    sync_result = SyncResult()

    # Run copy fields for local changes that will be synced
    sync_will_start.append(lambda: local_changes_copy_definitions(sync_result))
    # Then again after getting changes from remote, another sync will be needed after this
    sync_did_finish.append(lambda: remote_changes_copy_definitions(sync_result))
