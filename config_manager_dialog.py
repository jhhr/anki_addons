import datetime
import difflib
import filecmp
import json
from dataclasses import dataclass
from pathlib import Path

from aqt import mw
from aqt.qt import (
    Qt,
    QApplication,
    QSizePolicy,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QStyle,
)
from aqt.utils import showInfo, tooltip

from . import sync_actions
from .sync_actions import (
    UPDATED_STATE,
    get_paths,
    overwrite_addon_from_media,
    remove_addon_from_media,
    save_addon_to_media,
)
from .utils import (
    get_existing_addons,
    get_configs_in_media,
    get_main_config,
    is_addon_ignored,
    json_files_deep_equal,
    set_addon_ignored,
    write_main_config,
)

ACTIVE_MANAGER_DIALOGS: list[QDialog] = []


@dataclass
class AddonRow:
    addon_id: str
    addon_name: str
    installed: bool
    addon_meta_path: Path
    media_meta_path: Path
    media_exists: bool
    changed: bool
    updated: bool
    ignored: bool


class AddonConfigManagerDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, prefilter_changes_or_missing: bool = False):
        super().__init__(parent or mw)
        self.setWindowTitle("Manage Addon Configs")
        self.resize(1400, 760)
        self.apply_screen_size_limits()

        self.self_addon_id = Path(__file__).resolve().parent.name
        self.config = get_main_config()
        self.rows: list[AddonRow] = []
        self.filtered_rows: list[AddonRow] = []
        self.selected_addons: set[str] = set()
        self.last_clicked_addon_id: str | None = None
        self.row_checkbox_widgets: dict[str, QCheckBox] = {}
        self.prefilter_changes_or_missing = prefilter_changes_or_missing
        self.sort_column: int | None = None
        self.sort_direction: str = "none"

        layout = QVBoxLayout(self)

        top_controls = QHBoxLayout()
        top_controls.addStretch(1)

        self.filter_count_label = QLabel("Showing 0 addons of 0 total")
        self.filter_count_label.hide()
        self.filter_count_label.setStyleSheet("color: gray;")
        top_controls.addWidget(self.filter_count_label)

        self.selected_count_label = QLabel("Selected (0)")
        self.selected_count_label.setStyleSheet("color: gray;")
        top_controls.addWidget(self.selected_count_label)

        self.bulk_save_btn = QPushButton("Save to local media")
        self.bulk_save_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.bulk_save_btn.setToolTip(
            "Copy selected addon configs from addon folders to local media"
        )
        self.bulk_save_btn.clicked.connect(lambda: self.run_bulk_action("save"))
        top_controls.addWidget(self.bulk_save_btn)

        self.bulk_overwrite_btn = QPushButton("Overwrite from local media")
        self.bulk_overwrite_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.bulk_overwrite_btn.setToolTip(
            "Copy selected addon configs from local media to addon folders"
        )
        self.bulk_overwrite_btn.clicked.connect(lambda: self.run_bulk_action("overwrite"))
        top_controls.addWidget(self.bulk_overwrite_btn)

        self.bulk_remove_btn = QPushButton("Remove from local media")
        self.bulk_remove_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        self.bulk_remove_btn.setToolTip("Delete selected addon config files from local media")
        self.bulk_remove_btn.clicked.connect(lambda: self.run_bulk_action("remove_media"))
        top_controls.addWidget(self.bulk_remove_btn)

        self.bulk_install_btn = QPushButton("Install addon")
        self.bulk_install_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
        self.bulk_install_btn.setToolTip("Install selected missing addons")
        self.bulk_install_btn.clicked.connect(lambda: self.run_bulk_action("install"))
        top_controls.addWidget(self.bulk_install_btn)

        self.bulk_ignore_checkbox = QCheckBox("Ignore selected")
        self.bulk_ignore_checkbox.setToolTip("Toggle ignore for selected addons")
        self.bulk_ignore_checkbox.toggled.connect(self.on_bulk_ignore_toggled)
        top_controls.addWidget(self.bulk_ignore_checkbox)

        layout.addLayout(top_controls)

        filter_layout = QHBoxLayout()

        self.select_all_checkbox = QCheckBox("Select all")
        self.select_all_checkbox.toggled.connect(self.on_select_all_toggled)
        filter_layout.addWidget(self.select_all_checkbox, 0, Qt.AlignmentFlag.AlignBottom)

        name_filter_box = QVBoxLayout()
        name_filter_box.addWidget(QLabel("Filter by addon name"))
        self.name_filter = QLineEdit()
        self.name_filter.setPlaceholderText("Type to filter...")
        self.name_filter.textChanged.connect(self.apply_filters)
        name_filter_box.addWidget(self.name_filter)
        filter_layout.addLayout(name_filter_box, 2)

        status_filter_box = QVBoxLayout()
        status_filter_box.addWidget(QLabel("Status filters"))
        status_row = QHBoxLayout()

        self.changed_filter = QComboBox()
        self.changed_filter.addItems(["No filter", "Changed", "Not changed"])
        self.changed_filter.currentIndexChanged.connect(self.apply_filters)
        status_row.addWidget(self.changed_filter)

        self.installed_filter = QComboBox()
        self.installed_filter.addItems(["No filter", "Not installed", "Installed"])
        self.installed_filter.currentIndexChanged.connect(self.apply_filters)
        status_row.addWidget(self.installed_filter)

        self.updated_filter = QComboBox()
        self.updated_filter.addItems(["No filter", "Updated", "Not updated"])
        self.updated_filter.currentIndexChanged.connect(self.apply_filters)
        status_row.addWidget(self.updated_filter)

        status_filter_box.addLayout(status_row)
        filter_layout.addLayout(status_filter_box, 2)

        ignore_filter_box = QVBoxLayout()
        ignore_filter_box.addWidget(QLabel("Ignore filter"))
        self.ignore_filter = QComboBox()
        self.ignore_filter.addItems(["No filter", "Ignored", "Not ignored"])
        self.ignore_filter.currentIndexChanged.connect(self.apply_filters)
        ignore_filter_box.addWidget(self.ignore_filter)
        filter_layout.addLayout(ignore_filter_box, 1)

        layout.addLayout(filter_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Addon", "Status", "Actions", "Ignore"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self.on_header_clicked)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        layout.addWidget(self.table)
        layout.setStretchFactor(self.table, 1)

        bottom_layout = QHBoxLayout()

        sync_mode_frame = QFrame()
        sync_mode_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        sync_mode_layout = QVBoxLayout(sync_mode_frame)
        sync_mode_layout.setContentsMargins(0, 0, 0, 0)
        sync_mode_layout.addWidget(QLabel("Sync mode"))

        self.mode_auto = QRadioButton("Update configs on Sync")
        self.mode_summary = QRadioButton("Update configs on Sync, show summary")
        self.mode_ask = QRadioButton("Ask about changes to configs")

        self.mode_auto.toggled.connect(lambda checked: self.set_sync_mode("auto", checked))
        self.mode_summary.toggled.connect(lambda checked: self.set_sync_mode("summary", checked))
        self.mode_ask.toggled.connect(lambda checked: self.set_sync_mode("ask", checked))

        sync_mode_layout.addWidget(self.mode_auto)
        sync_mode_layout.addWidget(self.mode_summary)
        sync_mode_layout.addWidget(self.mode_ask)
        bottom_layout.addWidget(sync_mode_frame, 1, Qt.AlignmentFlag.AlignTop)

        sync_now_column = QVBoxLayout()
        sync_now_column.setContentsMargins(8, 0, 0, 0)
        self.sync_now_btn = QPushButton("Sync now")
        self.sync_now_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.sync_now_btn.setMinimumWidth(180)
        self.sync_now_btn.setMinimumHeight(52)
        self.sync_now_btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.sync_now_btn.setToolTip("Run Anki sync now in download-only mode for addon configs")
        self.sync_now_btn.clicked.connect(self.download_from_remote)
        sync_now_column.addWidget(self.sync_now_btn)
        sync_now_column.addSpacing(0)
        bottom_layout.addLayout(sync_now_column)
        bottom_layout.setStretch(0, 1)
        bottom_layout.setStretch(1, 0)
        bottom_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        layout.addLayout(bottom_layout)

        self.refresh_rows()
        self.update_bulk_controls()

    def apply_screen_size_limits(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return

        available = screen.availableGeometry()
        max_height = int(available.height() * 0.92)
        self.setMaximumHeight(max_height)

        if self.height() > max_height:
            self.resize(self.width(), max_height)

    def refresh_rows(self) -> None:
        self.config = get_main_config()
        self.rows = self.build_rows()
        self.update_sync_mode_controls()
        self.apply_filters()

    def update_sync_mode_controls(self) -> None:
        run_on_sync = bool(self.config.get("run_on_sync", True))
        show_summary_on_sync = bool(self.config.get("show_summary_on_sync", False))
        ask_on_sync = bool(self.config.get("ask_on_sync", False))

        self.mode_auto.blockSignals(True)
        self.mode_summary.blockSignals(True)
        self.mode_ask.blockSignals(True)

        self.mode_auto.setChecked(run_on_sync and not show_summary_on_sync and not ask_on_sync)
        self.mode_summary.setChecked(run_on_sync and show_summary_on_sync and not ask_on_sync)
        self.mode_ask.setChecked(ask_on_sync)

        self.mode_auto.blockSignals(False)
        self.mode_summary.blockSignals(False)
        self.mode_ask.blockSignals(False)

    def set_sync_mode(self, mode: str, checked: bool) -> None:
        if not checked:
            return

        if mode == "auto":
            self.config["run_on_sync"] = True
            self.config["show_summary_on_sync"] = False
            self.config["ask_on_sync"] = False
        elif mode == "summary":
            self.config["run_on_sync"] = True
            self.config["show_summary_on_sync"] = True
            self.config["ask_on_sync"] = False
        elif mode == "ask":
            self.config["run_on_sync"] = False
            self.config["show_summary_on_sync"] = False
            self.config["ask_on_sync"] = True

        write_main_config(self.config)
        self.update_sync_mode_controls()

    def build_rows(self) -> list[AddonRow]:
        anki_addons_path, media_path = get_paths()
        existing_addons = get_existing_addons(anki_addons_path)
        media_addons = set(get_configs_in_media(media_path))
        all_ids = sorted(existing_addons | media_addons)

        rows: list[AddonRow] = []
        for addon_id in all_ids:
            addon_meta_path = anki_addons_path / addon_id / "meta.json"
            media_meta_path = media_path / f"_{addon_id}_meta.json"
            installed = addon_id in existing_addons
            media_exists = media_meta_path.is_file()

            if not addon_meta_path.is_file() and not media_exists:
                continue

            changed = False

            if installed and media_exists and addon_meta_path.is_file():
                changed = not filecmp.cmp(
                    addon_meta_path, media_meta_path, shallow=False
                ) and not json_files_deep_equal(addon_meta_path, media_meta_path)

            if changed and addon_id in UPDATED_STATE:
                del UPDATED_STATE[addon_id]

            addon_name = self.get_addon_name(addon_id, addon_meta_path, media_meta_path)
            ignored = is_addon_ignored(self.config, addon_id)
            updated = bool(UPDATED_STATE.get(addon_id, False))

            rows.append(
                AddonRow(
                    addon_id=addon_id,
                    addon_name=addon_name,
                    installed=installed,
                    addon_meta_path=addon_meta_path,
                    media_meta_path=media_meta_path,
                    media_exists=media_exists,
                    changed=changed,
                    updated=updated and not changed,
                    ignored=ignored,
                )
            )

        rows.sort(key=lambda row: row.addon_name.lower())
        return rows

    def get_addon_name(self, addon_id: str, addon_meta_path: Path, media_meta_path: Path) -> str:
        if hasattr(mw, "addonManager") and hasattr(mw.addonManager, "annotatedName"):
            try:
                annotated = mw.addonManager.annotatedName(addon_id)
                if annotated:
                    return str(annotated)
            except Exception:
                pass

        for path in (addon_meta_path, media_meta_path):
            try:
                if path.is_file():
                    with open(path, "r", encoding="utf-8") as file_handle:
                        data = json.load(file_handle)
                    for key in ("name", "title"):
                        value = data.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
            except Exception:
                continue

        return addon_id

    def apply_filters(self) -> None:
        name_filter_value = self.name_filter.text().strip().lower()
        changed_filter = self.changed_filter.currentText()
        installed_filter = self.installed_filter.currentText()
        updated_filter = self.updated_filter.currentText()
        ignore_filter = self.ignore_filter.currentText()

        filtered = []
        for row in self.rows:
            if self.prefilter_changes_or_missing and not (row.changed or not row.installed):
                continue

            if name_filter_value and name_filter_value not in row.addon_name.lower():
                continue

            if changed_filter == "Changed" and not row.changed:
                continue
            if changed_filter == "Not changed" and row.changed:
                continue

            if installed_filter == "Not installed" and row.installed:
                continue
            if installed_filter == "Installed" and not row.installed:
                continue

            if updated_filter == "Updated" and not row.updated:
                continue
            if updated_filter == "Not updated" and row.updated:
                continue

            if ignore_filter == "Ignored" and not row.ignored:
                continue
            if ignore_filter == "Not ignored" and row.ignored:
                continue

            filtered.append(row)

        self.filtered_rows = filtered
        self.update_filter_count_label()
        self.apply_sorting()
        self.populate_table()

    def update_filter_count_label(self) -> None:
        has_filter = (
            bool(self.name_filter.text().strip())
            or self.changed_filter.currentText() != "No filter"
            or self.installed_filter.currentText() != "No filter"
            or self.updated_filter.currentText() != "No filter"
            or self.ignore_filter.currentText() != "No filter"
            or self.prefilter_changes_or_missing
        )
        if has_filter:
            self.filter_count_label.setText(
                f"Showing {len(self.filtered_rows)} addons of {len(self.rows)} total"
            )
            self.filter_count_label.show()
        else:
            self.filter_count_label.hide()

    def apply_sorting(self) -> None:
        if self.sort_column is None or self.sort_direction == "none":
            self.table.horizontalHeader().setSortIndicatorShown(False)
            return

        reverse = self.sort_direction == "desc"

        if self.sort_column == 0:
            self.filtered_rows.sort(key=lambda row: row.addon_name.lower(), reverse=reverse)
        elif self.sort_column == 1:
            self.filtered_rows.sort(key=lambda row: self.status_sort_rank(row), reverse=reverse)
        elif self.sort_column == 2:
            self.filtered_rows.sort(key=lambda row: row.addon_name.lower(), reverse=reverse)
        elif self.sort_column == 3:
            self.filtered_rows.sort(key=lambda row: self.ignore_sort_rank(row), reverse=reverse)

        order = (
            Qt.SortOrder.AscendingOrder
            if self.sort_direction == "asc"
            else Qt.SortOrder.DescendingOrder
        )
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSortIndicator(self.sort_column, order)

    def on_header_clicked(self, column: int) -> None:
        if column == 2:
            return

        if self.sort_column != column:
            self.sort_column = column
            self.sort_direction = "asc"
        elif self.sort_direction == "asc":
            self.sort_direction = "desc"
        elif self.sort_direction == "desc":
            self.sort_column = None
            self.sort_direction = "none"
        else:
            self.sort_direction = "asc"

        self.apply_filters()

    def status_sort_rank(self, row: AddonRow) -> int:
        if row.changed:
            return 0
        if row.updated:
            return 1
        if not row.installed:
            return 2
        return 3

    def ignore_sort_rank(self, row: AddonRow) -> int:
        return 0 if row.ignored else 1

    def populate_table(self) -> None:
        self.table.setRowCount(len(self.filtered_rows))
        self.row_checkbox_widgets.clear()

        for table_row, row_data in enumerate(self.filtered_rows):
            first_widget = QWidget()
            first_layout = QHBoxLayout(first_widget)
            first_layout.setContentsMargins(4, 2, 4, 2)

            checkbox = QCheckBox()
            checkbox.setChecked(row_data.addon_id in self.selected_addons)
            checkbox.clicked.connect(
                lambda checked, addon_id=row_data.addon_id: self.on_row_checkbox_clicked(
                    addon_id, checked
                )
            )
            first_layout.addWidget(checkbox)

            name_label = QLabel(f"{row_data.addon_name} ({row_data.addon_id})")
            first_layout.addWidget(name_label)
            first_layout.addStretch(1)

            self.table.setCellWidget(table_row, 0, first_widget)
            self.row_checkbox_widgets[row_data.addon_id] = checkbox

            status_text = "Up to date"
            if not row_data.installed:
                status_text = "Not installed"
            elif row_data.changed:
                status_text = "Changed"
            elif row_data.updated:
                status_text = "Updated"

            status_btn = QPushButton(status_text)
            status_btn.setToolTip("Status of addon config sync state")
            status_btn.setEnabled(row_data.changed)
            if row_data.changed:
                status_btn.clicked.connect(
                    lambda checked=False, row=row_data: self.show_diff_dialog(row)
                )
                status_btn.setToolTip("Show config diff and modified times")
            self.table.setCellWidget(table_row, 1, status_btn)

            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(4, 2, 4, 2)

            save_btn = QPushButton("Save to local media")
            save_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
            save_btn.setText("")
            save_btn.setToolTip("Save to local media")
            save_btn.setEnabled(row_data.installed and not row_data.ignored)
            save_btn.clicked.connect(
                lambda checked=False, addon_id=row_data.addon_id: self.run_action_for_addons(
                    [addon_id], "save"
                )
            )
            actions_layout.addWidget(save_btn)

            overwrite_btn = QPushButton("Overwrite from local media")
            overwrite_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            overwrite_btn.setText("")
            overwrite_btn.setToolTip("Overwrite from local media")
            overwrite_btn.setEnabled(
                row_data.installed and row_data.media_exists and not row_data.ignored
            )
            overwrite_btn.clicked.connect(
                lambda checked=False, addon_id=row_data.addon_id: self.run_action_for_addons(
                    [addon_id], "overwrite"
                )
            )
            actions_layout.addWidget(overwrite_btn)

            remove_btn = QPushButton("Remove from local media")
            remove_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
            remove_btn.setText("")
            remove_btn.setToolTip("Remove from local media")
            remove_btn.setEnabled(row_data.media_exists)
            remove_btn.clicked.connect(
                lambda checked=False, addon_id=row_data.addon_id: self.run_action_for_addons(
                    [addon_id], "remove_media"
                )
            )
            actions_layout.addWidget(remove_btn)

            install_btn = QPushButton("Install addon")
            install_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowDown))
            install_btn.setText("")
            install_btn.setToolTip("Install addon")
            install_btn.setEnabled(not row_data.installed)
            install_btn.clicked.connect(
                lambda checked=False, addon_id=row_data.addon_id: self.run_action_for_addons(
                    [addon_id], "install"
                )
            )
            actions_layout.addWidget(install_btn)

            actions_layout.addStretch(1)

            self.table.setCellWidget(table_row, 2, actions_widget)

            ignore_widget = QWidget()
            ignore_layout = QHBoxLayout(ignore_widget)
            ignore_layout.setContentsMargins(4, 2, 4, 2)
            ignore_checkbox = QCheckBox("Ignore")
            ignore_checkbox.setToolTip("Ignore this addon during sync operations")
            ignore_checkbox.setChecked(row_data.ignored)
            ignore_checkbox.toggled.connect(
                lambda checked, addon_id=row_data.addon_id: self.on_single_ignore_toggled(
                    addon_id, checked
                )
            )
            ignore_layout.addWidget(ignore_checkbox)
            ignore_layout.addStretch(1)
            self.table.setCellWidget(table_row, 3, ignore_widget)

            for column in range(4):
                item = QTableWidgetItem()
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                if row_data.addon_id == self.self_addon_id:
                    item.setBackground(self.palette().brush(self.palette().ColorRole.AlternateBase))
                self.table.setItem(table_row, column, item)

        self.update_select_all_checkbox()
        self.update_bulk_controls()

    def update_select_all_checkbox(self) -> None:
        visible_ids = {row.addon_id for row in self.filtered_rows}
        all_selected = bool(visible_ids) and visible_ids.issubset(self.selected_addons)
        self.select_all_checkbox.blockSignals(True)
        self.select_all_checkbox.setChecked(all_selected)
        self.select_all_checkbox.blockSignals(False)
        self.update_bulk_controls()

    def update_bulk_controls(self) -> None:
        selected_count = len(self.selected_addons)
        self.selected_count_label.setText(f"Selected ({selected_count})")
        self.selected_count_label.setStyleSheet("color: gray;" if selected_count == 0 else "")
        enabled = selected_count > 0
        self.bulk_save_btn.setEnabled(enabled)
        self.bulk_overwrite_btn.setEnabled(enabled)
        self.bulk_remove_btn.setEnabled(enabled)
        self.bulk_install_btn.setEnabled(enabled)
        self.bulk_ignore_checkbox.setEnabled(enabled)

    def on_row_checkbox_clicked(self, addon_id: str, checked: bool) -> None:
        shift_pressed = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)

        if shift_pressed and self.last_clicked_addon_id:
            visible_ids = [row.addon_id for row in self.filtered_rows]
            if addon_id in visible_ids and self.last_clicked_addon_id in visible_ids:
                start_index = visible_ids.index(self.last_clicked_addon_id)
                end_index = visible_ids.index(addon_id)
                low = min(start_index, end_index)
                high = max(start_index, end_index)
                for row_addon_id in visible_ids[low : high + 1]:
                    if checked:
                        self.selected_addons.add(row_addon_id)
                    else:
                        self.selected_addons.discard(row_addon_id)
                    if row_addon_id in self.row_checkbox_widgets:
                        checkbox = self.row_checkbox_widgets[row_addon_id]
                        checkbox.blockSignals(True)
                        checkbox.setChecked(checked)
                        checkbox.blockSignals(False)
            else:
                self.toggle_single_selection(addon_id, checked)
        else:
            self.toggle_single_selection(addon_id, checked)

        self.last_clicked_addon_id = addon_id
        self.update_select_all_checkbox()
        self.update_bulk_controls()

    def toggle_single_selection(self, addon_id: str, checked: bool) -> None:
        if checked:
            self.selected_addons.add(addon_id)
        else:
            self.selected_addons.discard(addon_id)

    def on_select_all_toggled(self, checked: bool) -> None:
        for row in self.filtered_rows:
            if checked:
                self.selected_addons.add(row.addon_id)
            else:
                self.selected_addons.discard(row.addon_id)
        for addon_id, checkbox in self.row_checkbox_widgets.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(addon_id in self.selected_addons)
            checkbox.blockSignals(False)
        self.update_bulk_controls()

    def run_bulk_action(self, action: str) -> None:
        addon_ids = sorted(self.selected_addons)
        if not addon_ids:
            tooltip("No rows selected", period=1500)
            return
        self.run_action_for_addons(addon_ids, action)

    def run_action_for_addons(self, addon_ids: list[str], action: str) -> None:
        success = 0
        for addon_id in addon_ids:
            row = next((item for item in self.rows if item.addon_id == addon_id), None)
            if not row:
                continue

            if action in {"save", "overwrite"} and row.ignored:
                continue

            if action == "save":
                if row.installed and save_addon_to_media(addon_id):
                    success += 1
            elif action == "overwrite":
                if row.installed and overwrite_addon_from_media(addon_id):
                    success += 1
            elif action == "remove_media":
                if remove_addon_from_media(addon_id):
                    success += 1
            elif action == "install":
                if (not row.installed) and self.install_addon(addon_id):
                    success += 1

        self.refresh_rows()
        tooltip(f"{success} addon(s) updated", period=1500)

    def on_single_ignore_toggled(self, addon_id: str, checked: bool) -> None:
        self.config = set_addon_ignored(self.config, addon_id, checked)
        write_main_config(self.config)
        self.refresh_rows()

    def on_bulk_ignore_toggled(self, checked: bool) -> None:
        addon_ids = sorted(self.selected_addons)
        if not addon_ids:
            return
        for addon_id in addon_ids:
            self.config = set_addon_ignored(self.config, addon_id, checked)
        write_main_config(self.config)
        self.refresh_rows()

    def install_addon(self, addon_id: str) -> bool:
        for method_name in ("installAddon", "install_addon", "downloadAndInstall"):
            method = getattr(mw.addonManager, method_name, None)
            if callable(method):
                try:
                    method(addon_id)
                    return True
                except Exception:
                    continue

        showInfo(
            f"Could not auto-install addon {addon_id}.\n\nUse Tools -> Add-ons -> Get Add-ons...",
            title="Install Addon",
        )
        return False

    def show_diff_dialog(self, row: AddonRow) -> None:
        if not row.addon_meta_path.is_file() or not row.media_meta_path.is_file():
            showInfo("One of the config files is missing.", title="Diff")
            return

        addon_lines = self.read_json_lines(row.addon_meta_path)
        media_lines = self.read_json_lines(row.media_meta_path)
        diff_lines = list(
            difflib.unified_diff(
                media_lines,
                addon_lines,
                fromfile=f"media/_{row.addon_id}_meta.json",
                tofile=f"addon/{row.addon_id}/meta.json",
                lineterm="",
            )
        )

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Config diff: {row.addon_name}")
        dialog.resize(1000, 700)
        layout = QVBoxLayout(dialog)

        addon_time = self.format_mtime(row.addon_meta_path)
        media_time = self.format_mtime(row.media_meta_path)
        info_label = QLabel(
            f"Addon file modified: {addon_time}<br>Media file modified: {media_time}"
        )
        info_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info_label)

        diff_box = QTextEdit()
        diff_box.setReadOnly(True)
        diff_box.setAcceptRichText(True)
        diff_box.setHtml(self.render_diff_html(diff_lines))
        layout.addWidget(diff_box)

        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)

        dialog.exec()

    def read_json_lines(self, path: Path) -> list[str]:
        try:
            with open(path, "r", encoding="utf-8") as file_handle:
                data = json.load(file_handle)
            text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
            return text.splitlines()
        except Exception:
            with open(path, "r", encoding="utf-8", errors="replace") as file_handle:
                return file_handle.read().splitlines()

    def render_diff_html(self, diff_lines: list[str]) -> str:
        if not diff_lines:
            return "<pre>No textual diff</pre>"

        html_lines: list[str] = []
        for line in diff_lines:
            safe_line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if line.startswith("+") and not line.startswith("+++"):
                html_lines.append(f'<span style="color:#1e7f34;">{safe_line}</span>')
            elif line.startswith("-") and not line.startswith("---"):
                html_lines.append(f'<span style="color:#b42318;">{safe_line}</span>')
            elif line.startswith("@@"):
                html_lines.append(f'<span style="color:#9a6700;">{safe_line}</span>')
            else:
                html_lines.append(safe_line)

        return "<pre>" + "\n".join(html_lines) + "</pre>"

    def format_mtime(self, path: Path) -> str:
        if not path.exists():
            return "N/A"
        timestamp = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")

    def download_from_remote(self) -> None:
        sync_actions.SUPPRESS_AUTO_SYNC_ACTIONS = True

        def on_sync_finish() -> None:
            sync_actions.SUPPRESS_AUTO_SYNC_ACTIONS = False
            self.refresh_rows()
            tooltip("Remote download sync complete", period=2000)

        sync_actions.SUPPRESS_SYNC_FINISH_CALLBACKS.append(on_sync_finish)

        if hasattr(mw, "on_sync_button_clicked"):
            mw.on_sync_button_clicked()
        elif hasattr(mw, "on_sync_button"):
            mw.on_sync_button()
        else:
            sync_actions.SUPPRESS_AUTO_SYNC_ACTIONS = False
            showInfo("Could not trigger sync from this Anki build.", title="Download from remote")


def open_addon_config_manager(
    blocking: bool = True, prefilter_changes_or_missing: bool = False
) -> AddonConfigManagerDialog:
    dialog = AddonConfigManagerDialog(
        parent=mw,
        prefilter_changes_or_missing=prefilter_changes_or_missing,
    )
    if blocking:
        dialog.exec()
    else:
        ACTIVE_MANAGER_DIALOGS.append(dialog)

        def _cleanup() -> None:
            if dialog in ACTIVE_MANAGER_DIALOGS:
                ACTIVE_MANAGER_DIALOGS.remove(dialog)

        dialog.finished.connect(lambda _: _cleanup())
        dialog.show()

    return dialog
