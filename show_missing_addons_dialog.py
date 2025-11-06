from pathlib import Path
from aqt import mw
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QWidget,
    QPushButton,
    QTextEdit,
    QHBoxLayout,
    QMessageBox,
    QApplication,
)
from aqt.utils import showInfo, tooltip


def show_missing_addons_dialog(missing_addons):
    """
    Show a custom dialog for missing addons with ability to delete configs or copy addon
    codes to clipboard.
    """
    dialog = QDialog(mw)
    dialog.setWindowTitle("Missing Addons")
    dialog.setMinimumWidth(600)

    # Limit dialog height to 80% of screen height
    screen = dialog.screen()
    if screen:
        screen_height = screen.availableGeometry().height()
        max_height = int(screen_height * 0.8)
        dialog.setMaximumHeight(max_height)

    # Internal state - list of addons currently displayed
    addon_list = missing_addons.copy()

    # Main layout for the dialog
    main_layout = QVBoxLayout()

    # Header message (not scrollable)
    header = QLabel()
    header.setWordWrap(True)
    main_layout.addWidget(header)

    # Create a frame to contain the addon list
    list_frame = QFrame()
    list_frame.setFrameShape(QFrame.Shape.StyledPanel)
    list_frame.setFrameShadow(QFrame.Shadow.Sunken)
    frame_layout = QVBoxLayout(list_frame)
    frame_layout.setContentsMargins(0, 0, 0, 0)

    # Create scroll area for the addon list
    scroll_area = QScrollArea()
    scroll_area.setWidgetResizable(True)
    scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

    # Widget to contain the scrollable content
    scroll_content = QWidget()
    scroll_layout = QVBoxLayout(scroll_content)

    # List of addon codes with delete buttons
    codes_label = QLabel("<br><b>Addon codes:</b>")
    scroll_layout.addWidget(codes_label)

    # Create a container for the list of addons with delete buttons
    addons_container_widget = QVBoxLayout()
    scroll_layout.addLayout(addons_container_widget)

    scroll_layout.addStretch()
    scroll_area.setWidget(scroll_content)
    frame_layout.addWidget(scroll_area)

    main_layout.addWidget(list_frame)

    # Delete all button right below the addon list
    delete_all_button = QPushButton(f"🗑️ Delete All {len(addon_list)} Configs")
    main_layout.addWidget(delete_all_button)

    # Bottom section (not scrollable)
    # Space-separated list for easy copying
    copy_label = QLabel("<br><b>Copy all codes (space-separated):</b>")
    main_layout.addWidget(copy_label)

    # Text box with space-separated codes
    text_box = QTextEdit()
    text_box.setReadOnly(True)
    text_box.setMaximumHeight(60)
    main_layout.addWidget(text_box)

    # Instructions
    instructions = QLabel(
        "<br><i>Tip: You can paste all codes at once in the Anki add-on installer!</i><br>"
        "<i>Go to: Tools → Add-ons → Get Add-ons... and paste the codes above.</i>"
    )
    instructions.setWordWrap(True)
    main_layout.addWidget(instructions)

    # Buttons
    button_layout = QHBoxLayout()

    copy_button = QPushButton("📋 Copy to Clipboard")
    button_layout.addWidget(copy_button)

    close_button = QPushButton("Close")
    close_button.clicked.connect(dialog.accept)
    button_layout.addWidget(close_button)

    main_layout.addLayout(button_layout)

    dialog.setLayout(main_layout)

    def update_ui():
        """Update the dialog UI to reflect the current addon_list"""
        # Update header
        header.setText(
            f"<b>Found {len(addon_list)} configs with no addon installed for them:</b><br>"
            "<i>You can install missing addons or delete unnecessary files.</i>"
        )

        # Clear existing addon rows
        while addons_container_widget.count():
            child = addons_container_widget.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                while child.layout().count():
                    subchild = child.layout().takeAt(0)
                    if subchild.widget():
                        subchild.widget().deleteLater()

        # Recreate addon rows
        for addon_id in addon_list:
            addon_row = QHBoxLayout()

            addon_label = QLabel(f"&nbsp;&nbsp;• <b>{addon_id}</b>")
            addon_row.addWidget(addon_label)
            addon_row.addStretch()

            delete_btn = QPushButton("🗑️ Delete file")
            delete_btn.setMaximumWidth(140)
            delete_btn.clicked.connect(lambda checked, aid=addon_id: on_delete_single(aid))
            addon_row.addWidget(delete_btn)

            addons_container_widget.addLayout(addon_row)

        # Update text box
        space_separated = " ".join(addon_list)
        text_box.setPlainText(space_separated)

        # Update copy button
        try:
            copy_button.clicked.disconnect()
        except Exception:
            pass
        copy_button.clicked.connect(lambda: copy_to_clipboard(space_separated))

        # Update delete all button
        try:
            delete_all_button.clicked.disconnect()
        except Exception:
            pass
        delete_all_button.clicked.connect(on_delete_all)
        delete_all_button.setEnabled(len(addon_list) > 0)
        delete_all_button.setText(f"🗑️ Delete All {len(addon_list)} Configs")

        # Close dialog if no addons left
        if len(addon_list) == 0:
            tooltip("All configs for missing addons deleted! ✓", period=2000)
            dialog.accept()

    def on_delete_single(addon_id):
        """Handle deletion of a single addon config"""
        if delete_synced_config(addon_id):
            addon_list.remove(addon_id)
            update_ui()

    def on_delete_all():
        """Handle deletion of all configs for missing addons"""
        reply = QMessageBox.question(
            dialog,
            "Confirm Delete All",
            "Are you sure you want to delete synced configs for all"
            f" {len(addon_list)} missing addon(s)?<br><br><i>This action cannot be undone.</i>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            deleted_count = 0
            failed_addons = []

            for addon_id in addon_list[:]:  # Copy list to avoid modification during iteration
                if delete_synced_config(addon_id):
                    deleted_count += 1
                else:
                    failed_addons.append(addon_id)

            # Update addon_list to only contain failed ones
            addon_list.clear()
            addon_list.extend(failed_addons)

            message = f"<b>Deleted {deleted_count} synced config(s)</b>"
            if failed_addons:
                message += f"<br><i>Failed to delete {len(failed_addons)} config(s)</i>"

            showInfo(message, title="Delete Complete", textFormat="rich")
            update_ui()

    # Initial UI setup
    update_ui()

    dialog.exec()


def copy_to_clipboard(text):
    """Copy text to clipboard and show confirmation"""
    clipboard = QApplication.clipboard()
    clipboard.setText(text)
    tooltip("Copied to clipboard! ✓", period=2000)


def delete_synced_config(addon_id):
    """Delete the synced config file for a specific addon

    Returns:
        bool: True if deletion was successful, False otherwise
    """
    media_path = Path(mw.pm.profileFolder(), "collection.media")
    config_file = media_path / f"_{addon_id}_meta.json"

    if config_file.exists():
        try:
            config_file.unlink()
            tooltip(f"Deleted config for {addon_id} ✓", period=2000)
            return True
        except Exception as e:
            showInfo(
                f"Error deleting config for {addon_id}: {str(e)}", title="Error", textFormat="rich"
            )
            return False
    else:
        tooltip(f"Config file for {addon_id} not found", period=2000)
        return False
