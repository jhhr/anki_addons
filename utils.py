import json
from pathlib import Path
from typing import Literal

from aqt import mw
from aqt.qt import (
    Qt,
    QWidget,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QDialogButtonBox,
    QMessageBox,
    QStyle,
)
from aqt.utils import qconnect


def is_addon_disabled(meta_json_path: Path) -> bool:
    """Check if an addon is marked as disabled in its meta.json file."""
    try:
        with open(meta_json_path, "r", encoding="utf-8") as f:
            meta_data = json.load(f)
            return meta_data.get("disabled", False)
    except Exception:
        return False  # If we can't read the file, assume it's not disabled


def get_existing_addons(anki_addons_path: Path) -> set[str]:
    """Get a set of existing addon IDs in the Anki addons folder."""
    return {addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()}


def get_configs_in_media(media_path: Path) -> list[str]:
    """Get a list of addon IDs that have synced config files in the media folder."""

    synced_config_files = [f for f in media_path.glob("_*_meta.json")]
    # Remove leading _ and trailing _meta.json
    synced_addon_ids = [f.name[1:-10] for f in synced_config_files]
    return synced_addon_ids


TextFormat = Literal["plain", "rich", "markdown"]


def show_non_blocking_info(
    text: str,
    parent: QWidget | None = None,
    type: str = "info",
    title: str = "Anki",
    textFormat: TextFormat | None = None,
    customBtns: list[QMessageBox.StandardButton] | None = None,
) -> QDialog:
    """
    Show a small non-blocking info window with an OK button.
    Similar to on aqt.utils.showInfo but non-blocking.
    """
    parent_widget: QWidget
    if parent is None:
        parent_widget = mw.app.activeWindow() or mw
    else:
        parent_widget = parent
    # Determine icon
    if type == "warning":
        icon = QStyle.StandardPixmap.SP_MessageBoxWarning
    elif type == "critical":
        icon = QStyle.StandardPixmap.SP_MessageBoxCritical
    else:
        icon = QStyle.StandardPixmap.SP_MessageBoxInformation

    # Create non-modal dialog
    dialog = QDialog(parent_widget)
    dialog.setWindowTitle(title)
    dialog.setModal(False)  # This makes it non-blocking

    layout = QVBoxLayout(dialog)

    # Add icon and text
    h_layout = QHBoxLayout()
    icon_label = QLabel()
    icon_label.setPixmap(dialog.style().standardIcon(icon).pixmap(32, 32))
    h_layout.addWidget(icon_label)

    text_label = QLabel(text)
    if textFormat == "plain":
        text_label.setTextFormat(Qt.TextFormat.PlainText)
    elif textFormat == "rich":
        text_label.setTextFormat(Qt.TextFormat.RichText)
    elif textFormat == "markdown":
        text_label.setTextFormat(Qt.TextFormat.MarkdownText)
    elif textFormat is not None:
        raise Exception("unexpected textFormat type")
    text_label.setWordWrap(True)
    h_layout.addWidget(text_label, 1)

    layout.addLayout(h_layout)

    # Add buttons
    button_box = QDialogButtonBox()
    if customBtns:
        for btn in customBtns:
            button_box.addButton(btn)
    else:
        button_box.addButton(QDialogButtonBox.StandardButton.Ok)

    qconnect(button_box.accepted, dialog.accept)
    qconnect(button_box.rejected, dialog.reject)

    layout.addWidget(button_box)

    dialog.show()  # Use show() instead of exec() for non-blocking
    return dialog


def ordered(obj: object) -> object:
    """
    Return a JSON-serializable object with its keys deeply sorted.
    Useful for comparing JSON objects.
    """
    if isinstance(obj, dict):
        return sorted((k, ordered(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return sorted(ordered(x) for x in obj)
    else:
        return obj


def json_files_deep_equal(file1: Path, file2: Path) -> bool:
    """Compare two JSON files for deep equality, ignoring key order."""
    try:
        with open(file1, "r", encoding="utf-8") as f1, open(file2, "r", encoding="utf-8") as f2:
            obj1 = json.load(f1)
            obj2 = json.load(f2)
            return ordered(obj1) == ordered(obj2)
    except Exception:
        return False  # If we can't read the files, assume they are not equal
