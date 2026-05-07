from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QCoreApplication, Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QToolButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import mobase

from .archive_index import (
    normalize_asset_path,
    parse_archive_index,
    unique_preserving_order,
)


@dataclass(frozen=True)
class Provider:
    kind: str
    path: str
    mod_name: str
    display_mod_name: str
    archive_name: str
    real_archive_path: str
    order: int
    archive_order: int = 0
    size: int = 0

    def label(self) -> str:
        if self.kind == "archive":
            return f"{self.display_mod_name} :: {self.archive_name}"
        return f"{self.display_mod_name} (loose)"

    def kind_label(self) -> str:
        return "Archive" if self.kind == "archive" else "Loose"


@dataclass(frozen=True)
class Conflict:
    path: str
    chain: List[Provider]

    @property
    def winner(self) -> Provider:
        return self.chain[-1]

    def chain_label(self) -> str:
        return " -> ".join(provider.label() for provider in self.chain)

    def overwritten_label(self) -> str:
        return " -> ".join(provider.label() for provider in self.chain[:-1])

    def reason(self) -> str:
        winner = self.winner
        has_archive = any(provider.kind == "archive" for provider in self.chain)
        has_loose = any(provider.kind == "loose" for provider in self.chain)
        if winner.kind == "loose" and has_archive:
            return "Loose file overrides archive contents"
        if winner.kind == "loose" and has_loose:
            return "Higher MO2 priority loose file wins"
        if winner.kind == "archive" and has_archive:
            return "Later active archive wins"
        return "Last provider in the chain wins"


@dataclass(frozen=True)
class ArchiveListing:
    mod_name: str
    archive_name: str
    display_mod_name: str
    real_archive_path: str
    archive_order: int
    files: List[str]


@dataclass(frozen=True)
class ScanResult:
    conflicts: List[Conflict]
    archives: List[ArchiveListing]
    warnings: List[str]


@dataclass(frozen=True)
class ModArchiveSummary:
    mod_name: str
    display_mod_name: str
    load_order: int
    archives: List[ArchiveListing]
    files: List[str]


ProgressCallback = Callable[[str, Optional[int], Optional[int]], bool]
ROLE_MOD_NAME = int(Qt.ItemDataRole.UserRole)
ROLE_ARCHIVE_NAME = ROLE_MOD_NAME + 1


CATEGORIES = [
    ("scripts", "Scripts"),
    ("textures", "Textures"),
    ("meshes", "Meshes"),
    ("interface", "Interface"),
    ("sound", "Sound"),
    ("music", "Music"),
    ("strings", "Strings"),
    ("seq", "Seq"),
    ("skse", "SKSE"),
    ("other", "Other"),
]


@lru_cache(maxsize=262144)
def category_for_path(path: str) -> str:
    parts = normalize_asset_path(path).split("\\")
    if not parts or not parts[0]:
        return "other"
    if parts[0] == "source" and len(parts) > 1 and parts[1] == "scripts":
        return "scripts"
    known = {key for key, _label in CATEGORIES if key != "other"}
    return parts[0] if parts[0] in known else "other"


class BsaConflictDialog(QDialog):
    def __init__(self, plugin: "BsaConflictViewerPlugin", parent=None):
        super().__init__(parent)
        self._plugin = plugin
        self._conflicts: List[Conflict] = []
        self._conflict_by_path: Dict[str, Conflict] = {}
        self._archives: List[ArchiveListing] = []
        self._mod_summaries: List[ModArchiveSummary] = []
        self._warnings: List[str] = []
        self._category_checks: Dict[str, QCheckBox] = {}
        self._last_detail_table: Optional[QTableWidget] = None
        self._populate_timer = QTimer(self)
        self._populate_timer.setSingleShot(True)
        self._populate_timer.setInterval(300)
        self._populate_timer.timeout.connect(self.populate)

        self.setWindowTitle("BSA Conflict Viewer")
        self.resize(1250, 760)

        layout = QVBoxLayout(self)

        self._summary = QLabel(self)
        self._summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._summary)

        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Mods:", self))
        self._mod_filter = QLineEdit(self)
        self._mod_filter.setPlaceholderText("Partial mod search")
        self._mod_filter.textChanged.connect(self._schedule_populate)
        search_layout.addWidget(self._mod_filter, 1)

        search_layout.addWidget(QLabel("Sort:", self))
        self._mod_sort = QComboBox(self)
        self._mod_sort.addItem("Load order", "load_order")
        self._mod_sort.addItem("Alphabetic", "alphabetic")
        self._mod_sort.addItem("File quantity", "files")
        self._mod_sort.currentIndexChanged.connect(self.populate)
        search_layout.addWidget(self._mod_sort)

        search_layout.addWidget(QLabel("Files:", self))
        self._file_filter = QLineEdit(self)
        self._file_filter.setPlaceholderText("Partial file search")
        self._file_filter.textChanged.connect(self._schedule_populate)
        search_layout.addWidget(self._file_filter, 2)
        layout.addLayout(search_layout)

        category_layout = QHBoxLayout()
        for key, label in CATEGORIES:
            checkbox = QCheckBox(label, self)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self.populate)
            self._category_checks[key] = checkbox
            category_layout.addWidget(checkbox)
        category_layout.addStretch(1)
        layout.addLayout(category_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._mod_list = QTreeWidget(self)
        self._mod_list.setMinimumWidth(320)
        self._mod_list.setHeaderHidden(True)
        self._mod_list.setUniformRowHeights(True)
        self._mod_list.itemClicked.connect(self._on_mod_item_clicked)
        self._mod_list.currentItemChanged.connect(self._on_selected_mod_changed)
        splitter.addWidget(self._mod_list)

        details = QVBoxLayout()
        details_widget = QWidget(self)
        details_widget.setLayout(details)

        self._winning_header, self._winning_section = self._make_dropdown_section(
            "Winning file conflicts: 0"
        )
        self._winning_table = self._make_table(["File", "Overwritten mods"])
        self._winning_section.addWidget(self._winning_table)
        details.addWidget(self._winning_header)
        details.addLayout(self._winning_section)

        self._losing_header, self._losing_section = self._make_dropdown_section(
            "Losing file conflicts: 0"
        )
        self._losing_table = self._make_table(["File", "Providing mod"])
        self._losing_section.addWidget(self._losing_table)
        details.addWidget(self._losing_header)
        details.addLayout(self._losing_section)

        self._no_conflict_header, self._no_conflict_section = self._make_dropdown_section(
            "Files without conflicts: 0"
        )
        self._no_conflict_table = self._make_table(["File"])
        self._no_conflict_section.addWidget(self._no_conflict_table)
        details.addWidget(self._no_conflict_header)
        details.addLayout(self._no_conflict_section)
        self._detail_tables = (
            self._winning_table,
            self._losing_table,
            self._no_conflict_table,
        )
        for table in self._detail_tables:
            table.itemSelectionChanged.connect(
                lambda table=table: self._remember_detail_table(table)
            )

        splitter.addWidget(details_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(self)
        scan_button = buttons.addButton("Scan", QDialogButtonBox.ButtonRole.ActionRole)
        export_button = buttons.addButton(
            "Export CSV", QDialogButtonBox.ButtonRole.ActionRole
        )
        copy_button = buttons.addButton(
            "Copy Selected Chain", QDialogButtonBox.ButtonRole.ActionRole
        )
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        scan_button.clicked.connect(self.refresh)
        export_button.clicked.connect(self.export_csv)
        copy_button.clicked.connect(self.copy_selected_chain)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.refresh()

    def _make_dropdown_section(self, title: str) -> tuple[QToolButton, QVBoxLayout]:
        button = QToolButton(self)
        button.setText(title)
        button.setCheckable(True)
        button.setChecked(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setArrowType(Qt.ArrowType.DownArrow)
        button.setAutoRaise(False)

        section = QVBoxLayout()
        section.setContentsMargins(0, 0, 0, 0)

        def toggle(checked: bool) -> None:
            button.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )
            for index in range(section.count()):
                item = section.itemAt(index)
                widget = item.widget()
                if widget is not None:
                    widget.setVisible(checked)

        button.toggled.connect(toggle)
        return button, section

    def _make_table(self, headers: List[str]) -> QTableWidget:
        table = QTableWidget(self)
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        table.verticalHeader().setDefaultSectionSize(22)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        return table

    def _remember_detail_table(self, table: QTableWidget) -> None:
        if table.selectionModel().hasSelection():
            self._last_detail_table = table

    def refresh(self) -> None:
        progress = QProgressDialog("Preparing BSA conflict scan...", "Cancel", 0, 0, self)
        progress.setWindowTitle("BSA Conflict Viewer")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setFixedWidth(560)
        progress.show()

        def update_progress(
            message: str, value: Optional[int] = None, maximum: Optional[int] = None
        ) -> bool:
            progress.setLabelText(self._short_progress_message(message))
            progress.setToolTip(message)
            if maximum is None:
                progress.setRange(0, 0)
            else:
                progress.setRange(0, maximum)
                progress.setValue(value or 0)
            QApplication.processEvents()
            return not progress.wasCanceled()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = self._plugin.scan(update_progress)
            self._conflicts = result.conflicts
            self._conflict_by_path = {conflict.path: conflict for conflict in self._conflicts}
            self._archives = result.archives
            self._warnings = result.warnings
            self._mod_summaries = self._build_mod_summaries()
        finally:
            QApplication.restoreOverrideCursor()
            progress.close()
        self.populate()

    @staticmethod
    def _short_progress_message(message: str, limit: int = 96) -> str:
        if len(message) <= limit:
            return message
        head = max(20, (limit - 5) // 2)
        tail = max(20, limit - head - 5)
        return f"{message[:head]} ... {message[-tail:]}"

    def _schedule_populate(self) -> None:
        self._populate_timer.start()

    def populate(self) -> None:
        if self._populate_timer.isActive():
            self._populate_timer.stop()
        enabled_categories = self._enabled_categories()
        selected_summary, selected_archive = self._selected_scope()
        selected_mod_name = selected_summary.mod_name if selected_summary is not None else None
        selected_archive_name = (
            selected_archive.archive_name if selected_archive is not None else None
        )

        mod_filter = self._filter_text(self._mod_filter)
        file_filter = self._filter_text(self._file_filter)
        visible_mods = [
            summary
            for summary in self._mod_summaries
            if self._summary_matches_filters(
                summary, enabled_categories, mod_filter, file_filter
            )
        ]
        self._sort_mod_summaries(visible_mods, enabled_categories, file_filter)
        visible_archive_files = sum(
            self._visible_file_count(summary.files, enabled_categories, file_filter)
            for summary in visible_mods
        )
        self._summary.setText(
            f"{len(visible_mods)} BSA mods, "
            f"{len(self._archives)} archives, {visible_archive_files} visible files. "
            f"{len(self._warnings)} warnings."
        )
        self._summary.setToolTip("\n".join(self._warnings))

        self._mod_list.setUpdatesEnabled(False)
        previous_signal_state = self._mod_list.blockSignals(True)
        try:
            self._mod_list.clear()
            next_selection_item = None
            first_item = None
            for summary in visible_mods:
                visible_file_count = self._visible_file_count(
                    summary.files, enabled_categories, file_filter
                )
                item = QTreeWidgetItem(
                    [
                        (
                            f"{summary.display_mod_name} "
                            f"({len(summary.archives)} BSA, {visible_file_count} files)"
                        )
                    ]
                )
                item.setData(0, ROLE_MOD_NAME, summary.mod_name)
                item.setData(0, ROLE_ARCHIVE_NAME, "")
                item.setToolTip(0, "\n".join(archive.archive_name for archive in summary.archives))
                self._mod_list.addTopLevelItem(item)
                if first_item is None:
                    first_item = item
                if selected_mod_name == summary.mod_name and selected_archive_name is None:
                    next_selection_item = item

                for archive in sorted(
                    summary.archives,
                    key=lambda archive: (archive.archive_order, archive.archive_name.lower()),
                ):
                    archive_file_count = self._visible_file_count(
                        archive.files, enabled_categories, file_filter
                    )
                    child = QTreeWidgetItem(
                        [
                            (
                                f"{archive.archive_name} "
                                f"({archive_file_count} files)"
                            )
                        ]
                    )
                    child.setData(0, ROLE_MOD_NAME, summary.mod_name)
                    child.setData(0, ROLE_ARCHIVE_NAME, archive.archive_name)
                    child.setToolTip(0, archive.real_archive_path)
                    item.addChild(child)
                    if (
                        selected_mod_name == summary.mod_name
                        and selected_archive_name == archive.archive_name
                    ):
                        next_selection_item = child
                if selected_mod_name == summary.mod_name:
                    item.setExpanded(True)
                else:
                    item.setExpanded(False)
        finally:
            self._mod_list.blockSignals(previous_signal_state)
            self._mod_list.setUpdatesEnabled(True)

        if first_item is not None:
            self._mod_list.setCurrentItem(next_selection_item or first_item)
        else:
            self._populate_mod_details(None, None)

    def _on_selected_mod_changed(self, *_args) -> None:
        current = self._mod_list.currentItem()
        if current is not None and current.parent() is None:
            current.setExpanded(True)
        summary, archive = self._selected_scope()
        self._populate_mod_details(summary, archive)

    def _on_mod_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.parent() is None:
            item.setExpanded(True)

    def _selected_summary(self) -> Optional[ModArchiveSummary]:
        summary, _archive = self._selected_scope()
        return summary

    def _selected_scope(self) -> tuple[Optional[ModArchiveSummary], Optional[ArchiveListing]]:
        current = self._mod_list.currentItem() if hasattr(self, "_mod_list") else None
        if current is None:
            return None, None
        mod_name = current.data(0, ROLE_MOD_NAME)
        archive_name = current.data(0, ROLE_ARCHIVE_NAME)
        selected_summary = None
        for summary in self._mod_summaries:
            if summary.mod_name == mod_name:
                selected_summary = summary
                break
        if selected_summary is None:
            return None, None
        if archive_name:
            for archive in selected_summary.archives:
                if archive.archive_name == archive_name:
                    return selected_summary, archive
        return selected_summary, None

    def _build_mod_summaries(self) -> List[ModArchiveSummary]:
        by_mod: Dict[str, List[ArchiveListing]] = {}
        for archive in self._archives:
            by_mod.setdefault(archive.mod_name, []).append(archive)

        summaries = []
        for mod_name, archives in by_mod.items():
            files = sorted({path for archive in archives for path in archive.files})
            display = archives[0].display_mod_name if archives else mod_name
            load_order = self._plugin._origin_conflict_order(mod_name)
            summaries.append(ModArchiveSummary(mod_name, display, load_order, archives, files))

        summaries.sort(key=lambda summary: (summary.load_order, summary.display_mod_name.lower()))
        return summaries

    def _summary_matches_filters(
        self,
        summary: ModArchiveSummary,
        enabled_categories: set[str],
        mod_filter: str,
        file_filter: str,
    ) -> bool:
        if mod_filter:
            searchable_mod = " ".join(
                [summary.display_mod_name, summary.mod_name]
                + [archive.archive_name for archive in summary.archives]
            ).lower()
            if mod_filter not in searchable_mod:
                return False

        return any(
            category_for_path(path) in enabled_categories
            and (not file_filter or file_filter in path.lower())
            for path in summary.files
        )

    def _sort_mod_summaries(
        self,
        summaries: List[ModArchiveSummary],
        enabled_categories: set[str],
        file_filter: str,
    ) -> None:
        sort_mode = self._mod_sort.currentData() if hasattr(self, "_mod_sort") else "alphabetic"
        if sort_mode == "files":
            summaries.sort(
                key=lambda summary: (
                    -self._visible_file_count(summary.files, enabled_categories, file_filter),
                    summary.display_mod_name.lower(),
                )
            )
        elif sort_mode == "load_order":
            summaries.sort(
                key=lambda summary: (
                    summary.load_order,
                    summary.display_mod_name.lower(),
                )
            )
        else:
            summaries.sort(key=lambda summary: summary.display_mod_name.lower())

    @staticmethod
    def _visible_file_count(
        files: List[str], enabled_categories: set[str], file_filter: str
    ) -> int:
        return sum(
            1
            for path in files
            if category_for_path(path) in enabled_categories
            and (not file_filter or file_filter in path.lower())
        )

    def _populate_mod_details(
        self, summary: Optional[ModArchiveSummary], archive: Optional[ArchiveListing]
    ) -> None:
        enabled_categories = self._enabled_categories()
        file_filter = self._filter_text(self._file_filter)
        if summary is None:
            self._set_table_rows(self._winning_table, [])
            self._set_table_rows(self._losing_table, [])
            self._set_table_rows(self._no_conflict_table, [])
            self._winning_header.setText("Winning file conflicts: 0")
            self._losing_header.setText("Losing file conflicts: 0")
            self._no_conflict_header.setText("Files without conflicts: 0")
            return

        scope_files = archive.files if archive is not None else summary.files
        selected_files = {
            path
            for path in scope_files
            if category_for_path(path) in enabled_categories
            and (not file_filter or file_filter in path.lower())
        }
        conflict_paths = set()
        winning_rows = []
        winning_tooltips = []
        losing_rows = []
        losing_tooltips = []

        for path in sorted(selected_files):
            conflict = self._conflict_by_path.get(path)
            if conflict is None:
                continue
            selected_providers = [
                provider
                for provider in conflict.chain
                if provider.kind == "archive" and provider.mod_name == summary.mod_name
                and (archive is None or provider.archive_name == archive.archive_name)
            ]
            if not selected_providers:
                continue

            conflict_paths.add(conflict.path)
            selected_winner = (
                conflict.winner.kind == "archive"
                and conflict.winner.mod_name == summary.mod_name
                and (archive is None or conflict.winner.archive_name == archive.archive_name)
            )
            if selected_winner:
                overwritten = [
                    provider.label()
                    for provider in conflict.chain[:-1]
                    if archive is not None
                    or provider.mod_name != summary.mod_name
                ]
                winning_rows.append(
                    [conflict.path, ", ".join(overwritten) or "(same mod archives)"]
                )
                winning_tooltips.append(conflict.chain_label())
            else:
                losing_rows.append([conflict.path, conflict.winner.label()])
                losing_tooltips.append(conflict.chain_label())

        no_conflict_rows = [[path] for path in sorted(selected_files - conflict_paths)]
        no_conflict_tooltips = [row[0] for row in no_conflict_rows]

        self._winning_header.setText(f"Winning file conflicts: {len(winning_rows)}")
        self._losing_header.setText(f"Losing file conflicts: {len(losing_rows)}")
        self._no_conflict_header.setText(
            f"Files without conflicts: {len(no_conflict_rows)}"
        )
        self._set_table_rows(self._winning_table, winning_rows, winning_tooltips)
        self._set_table_rows(self._losing_table, losing_rows, losing_tooltips)
        self._set_table_rows(self._no_conflict_table, no_conflict_rows, no_conflict_tooltips)

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(max(0, size))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                if unit == "B":
                    return f"{int(value)} B"
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{int(size)} B"

    def _set_table_rows(
        self,
        table: QTableWidget,
        rows: List[List[str]],
        tooltips: Optional[List[str]] = None,
    ) -> None:
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        table.clearContents()
        table.setRowCount(len(rows))
        try:
            for row_index, row in enumerate(rows):
                tooltip = tooltips[row_index] if tooltips is not None else " | ".join(row)
                for column, value in enumerate(row):
                    item = QTableWidgetItem(value)
                    item.setToolTip(tooltip)
                    table.setItem(row_index, column, item)
        finally:
            table.setSortingEnabled(True)
            table.setUpdatesEnabled(True)

    def _enabled_categories(self) -> set[str]:
        return {
            key
            for key, checkbox in self._category_checks.items()
            if checkbox.isChecked()
        }

    @staticmethod
    def _filter_text(field: QLineEdit) -> str:
        return field.text().strip().lower()

    def export_csv(self) -> None:
        summary, archive = self._selected_scope()
        if summary is None:
            QMessageBox.information(self, "Export CSV", "Select a mod first.")
            return

        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Selected Mod Conflicts", "", "CSV files (*.csv)"
        )
        if not filename:
            return

        try:
            with open(filename, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                selection_name = summary.display_mod_name
                if archive is not None:
                    selection_name = f"{summary.display_mod_name} :: {archive.archive_name}"
                writer.writerow([selection_name])
                writer.writerow([])
                self._write_table_to_csv(writer, "winning_file_conflicts", self._winning_table)
                self._write_table_to_csv(writer, "losing_file_conflicts", self._losing_table)
                self._write_table_to_csv(writer, "files_without_conflicts", self._no_conflict_table)
        except OSError as error:
            QMessageBox.warning(self, "Export CSV", f"Could not write CSV:\n{error}")

    def _write_table_to_csv(
        self, writer: csv.writer, section_name: str, table: QTableWidget
    ) -> None:
        writer.writerow([section_name])
        writer.writerow(
            [
                table.horizontalHeaderItem(column).text()
                for column in range(table.columnCount())
            ]
        )
        for row in range(table.rowCount()):
            writer.writerow(
                [
                    table.item(row, column).text() if table.item(row, column) else ""
                    for column in range(table.columnCount())
                ]
            )
        writer.writerow([])

    def copy_selected_chain(self) -> None:
        table = self._selected_detail_table()
        if table is None:
            return
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        item = table.item(rows[0].row(), 0)
        if item is not None:
            QApplication.clipboard().setText(item.toolTip())

    def _selected_detail_table(self) -> Optional[QTableWidget]:
        focus = QApplication.focusWidget()
        tables = getattr(self, "_detail_tables", ())
        for table in tables:
            if focus is not None and (
                focus is table or focus is table.viewport() or table.isAncestorOf(focus)
            ):
                return table
        if (
            self._last_detail_table is not None
            and self._last_detail_table.selectionModel().hasSelection()
        ):
            return self._last_detail_table
        for table in tables:
            if table.selectionModel().hasSelection():
                return table
        return None


class BsaConflictViewerPlugin(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._organizer: Optional[mobase.IOrganizer] = None
        self._parent = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self) -> str:
        return "BSA Conflict Viewer"

    def localizedName(self) -> str:
        return self._tr("BSA Conflict Viewer")

    def displayName(self) -> str:
        return self.localizedName()

    def author(self) -> str:
        return "MO2 community"

    def description(self) -> str:
        return self._tr("Shows archive and loose-file overwrite chains without extraction.")

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(1, 1, 0, mobase.ReleaseType.FINAL)

    def isActive(self) -> bool:
        return True

    def settings(self):
        return [
            mobase.PluginSetting(
                "include_vanilla_archives",
                "include vanilla game archives in the conflict scan",
                True,
            ),
            mobase.PluginSetting(
                "scan_enabled_mod_archives",
                "scan BSA/BA2 files in enabled mods when MO2 DataArchives omits them",
                True,
            ),
        ]

    def tooltip(self) -> str:
        return self._tr("Scan active BSA/BA2 contents and show full overwrite chains.")

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, widget) -> None:
        self._parent = widget

    def display(self) -> None:
        dialog = BsaConflictDialog(self, self._parent)
        dialog.exec()

    def scan(self, progress: Optional[ProgressCallback] = None) -> ScanResult:
        providers: Dict[str, List[Provider]] = {}
        archives: List[ArchiveListing] = []
        warnings: List[str] = []
        if progress and not progress("Reading active archive list...", None, None):
            return ScanResult([], [], ["Scan canceled before archive list was read."])
        canceled = not self._collect_archives(providers, archives, warnings, progress)
        archive_paths = set(providers)
        if not canceled:
            if progress and not progress("Scanning loose files...", None, None):
                canceled = True
            else:
                canceled = not self._collect_loose_files(providers, archive_paths, progress)

        if progress:
            progress("Building conflict chains...", None, None)
        conflicts: List[Conflict] = []
        for path, chain in providers.items():
            if len(chain) < 2:
                continue
            if not any(provider.kind == "archive" for provider in chain):
                continue

            chain = sorted(
                chain,
                key=self._provider_sort_key,
            )
            conflicts.append(Conflict(path, chain))

        conflicts.sort(key=lambda conflict: conflict.path)
        if canceled:
            warnings.append("Scan canceled. Showing partial results.")
        return ScanResult(conflicts, archives, warnings)

    @staticmethod
    def _provider_sort_key(provider: Provider) -> tuple[int, int, int]:
        archive_order = provider.archive_order if provider.kind == "archive" else 2_000_000_000
        tie_breaker = 1 if provider.kind == "loose" else 0
        return (provider.order, archive_order, tie_breaker)

    def _collect_archives(
        self,
        providers: Dict[str, List[Provider]],
        archive_list: List[ArchiveListing],
        warnings: List[str],
        progress: Optional[ProgressCallback] = None,
    ) -> bool:
        feature = self._organizer.gameFeatures().gameFeature(mobase.DataArchives)
        if feature is None:
            warnings.append("Current game does not expose DataArchives.")
            return True

        archive_names = []
        if bool(self._organizer.pluginSetting(self.name(), "include_vanilla_archives")):
            archive_names.extend(feature.vanillaArchives())

        archive_names.extend(feature.archives(self._organizer.profile()))
        archive_names = unique_preserving_order(archive_names)
        archive_specs = [
            (name, self._archive_path(name), order, None)
            for order, name in enumerate(archive_names)
        ]

        if bool(self._organizer.pluginSetting(self.name(), "scan_enabled_mod_archives")):
            archive_specs.extend(
                self._enabled_mod_archive_specs(
                    {name.lower() for name in archive_names}, len(archive_specs)
                )
            )

        for index, (archive_name, archive_path, order, origin_override) in enumerate(
            archive_specs
        ):
            if progress and not progress(
                f"Indexing archive {index + 1}/{len(archive_specs)}: {archive_name}",
                index,
                len(archive_specs),
            ):
                return False

            if archive_path is None:
                warnings.append(f"Could not resolve archive: {archive_name}")
                continue

            index = parse_archive_index(archive_path)
            if index is None:
                warnings.append(f"Could not read archive index: {archive_path}")
                continue

            origins = self._organizer.getFileOrigins(archive_name)
            origin = origin_override or (origins[0] if origins else "data")
            provider_base = Provider(
                kind="archive",
                path="",
                mod_name=origin,
                display_mod_name=self._display_name_for_origin(origin),
                archive_name=archive_name,
                real_archive_path=str(archive_path),
                order=self._origin_conflict_order(origin),
                archive_order=order,
            )

            archive_files = sorted(entry.path for entry in index.files)
            archive_list.append(
                ArchiveListing(
                    mod_name=provider_base.mod_name,
                    archive_name=archive_name,
                    display_mod_name=provider_base.display_mod_name,
                    real_archive_path=str(archive_path),
                    archive_order=provider_base.archive_order,
                    files=archive_files,
                )
            )

            for entry in index.files:
                provider = Provider(
                    kind=provider_base.kind,
                    path=entry.path,
                    mod_name=provider_base.mod_name,
                    display_mod_name=provider_base.display_mod_name,
                    archive_name=provider_base.archive_name,
                    real_archive_path=provider_base.real_archive_path,
                    order=provider_base.order,
                    archive_order=provider_base.archive_order,
                    size=entry.size,
                )
                providers.setdefault(entry.path, []).append(provider)
        return True

    def _enabled_mod_archive_specs(
        self, existing_names: set[str], start_order: int
    ) -> list[tuple[str, Optional[Path], int, Optional[str]]]:
        specs: list[tuple[str, Optional[Path], int, Optional[str]]] = []
        seen_paths: set[str] = set()
        plugin_orders = self._active_plugin_orders()

        for mod_name in self._organizer.modList().allModsByProfilePriority():
            state = self._organizer.modList().state(mod_name)
            if not int(state) & 2:
                continue

            mod = self._organizer.modList().getMod(mod_name)
            if mod is None:
                continue

            mod_path = Path(mod.absolutePath())
            if not mod_path.exists():
                continue

            for archive_path in sorted(
                list(mod_path.glob("*.bsa")) + list(mod_path.glob("*.ba2")),
                key=lambda path: path.name.lower(),
            ):
                key = archive_path.name.lower()
                real_key = str(archive_path).lower()
                if key in existing_names or real_key in seen_paths:
                    continue

                plugin_order = self._associated_plugin_order(archive_path.name, plugin_orders)
                if plugin_order is not None:
                    order = start_order + plugin_order
                else:
                    order = start_order + 100000 + max(
                        0, self._organizer.modList().priority(mod_name)
                    )

                seen_paths.add(real_key)
                specs.append((archive_path.name, archive_path, order, mod_name))

        specs.sort(key=lambda spec: spec[2])
        return specs

    def _active_plugin_orders(self) -> dict[str, int]:
        orders: dict[str, int] = {}
        plugin_list = self._organizer.pluginList()
        for plugin_name in plugin_list.pluginNames():
            if int(plugin_list.state(plugin_name)) != 2:
                continue
            load_order = plugin_list.loadOrder(plugin_name)
            if load_order >= 0:
                orders[Path(plugin_name).stem.lower()] = load_order
        return orders

    @staticmethod
    def _associated_plugin_order(
        archive_name: str, plugin_orders: dict[str, int]
    ) -> Optional[int]:
        archive_stem = Path(archive_name).stem.lower()
        best_order = None
        best_length = -1
        for plugin_stem, load_order in plugin_orders.items():
            if archive_stem == plugin_stem or archive_stem.startswith(
                plugin_stem + " -"
            ):
                if len(plugin_stem) > best_length:
                    best_length = len(plugin_stem)
                    best_order = load_order
        return best_order

    def _collect_loose_files(
        self,
        providers: Dict[str, List[Provider]],
        archive_paths: set[str],
        progress: Optional[ProgressCallback] = None,
    ) -> bool:
        if not archive_paths:
            return True
        specs = self._loose_origin_specs()
        for index, (origin, root, order) in enumerate(specs):
            if progress and not progress(
                f"Scanning loose files {index + 1}/{len(specs)}: {origin}",
                index,
                len(specs),
            ):
                return False
            self._collect_loose_files_from_origin(
                origin, root, order, providers, archive_paths
            )
        return True

    def _loose_origin_specs(self) -> list[tuple[str, Path, int]]:
        specs: list[tuple[str, Path, int]] = []
        seen_roots: set[str] = set()

        data_root = self._origin_root_path("data")
        if data_root is not None and data_root.exists():
            specs.append(("data", data_root, self._origin_conflict_order("data")))
            seen_roots.add(str(data_root).lower())

        for mod_name in self._organizer.modList().allModsByProfilePriority():
            state = self._organizer.modList().state(mod_name)
            if not int(state) & 2:
                continue
            mod = self._organizer.modList().getMod(mod_name)
            if mod is None:
                continue
            root = Path(mod.absolutePath())
            root_key = str(root).lower()
            if not root.exists() or root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            specs.append(
                (
                    mod_name,
                    root,
                    self._origin_conflict_order(mod_name),
                )
            )

        overwrite_root = self._origin_root_path("overwrite")
        if overwrite_root is not None and overwrite_root.exists():
            root_key = str(overwrite_root).lower()
            if root_key not in seen_roots:
                specs.append(
                    ("overwrite", overwrite_root, self._origin_conflict_order("overwrite"))
                )

        specs.sort(key=lambda spec: spec[2])
        return specs

    def _origin_conflict_order(self, origin: str) -> int:
        lowered = origin.lower()
        if lowered == "data":
            return -1_000_000_000
        if lowered == "overwrite":
            return 2_000_000_000
        return max(0, self._organizer.modList().priority(origin))

    def _collect_loose_files_from_origin(
        self,
        origin: str,
        root: Path,
        order: int,
        providers: Dict[str, List[Provider]],
        archive_paths: set[str],
    ) -> None:
        display_name = self._display_name_for_origin(origin)
        stack = [(str(root), "")]
        while stack:
            directory, relative_prefix = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        name = entry.name
                        relative_path = (
                            f"{relative_prefix}\\{name}" if relative_prefix else name
                        )
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append((entry.path, relative_path))
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                        except OSError:
                            continue

                        path = normalize_asset_path(relative_path)
                        if (
                            not path
                            or path not in archive_paths
                            or self._is_archive_container(path)
                            or self._is_mod_metadata_file(path)
                        ):
                            continue
                        try:
                            size = entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            size = 0
                        provider = Provider(
                            kind="loose",
                            path=path,
                            mod_name=origin,
                            display_mod_name=display_name,
                            archive_name="",
                            real_archive_path=entry.path,
                            order=order,
                            size=size,
                        )
                        providers.setdefault(path, []).append(provider)
            except OSError:
                continue

    def _archive_path(self, archive_name: str) -> Optional[Path]:
        resolved = self._organizer.resolvePath(archive_name)
        if resolved:
            return Path(resolved)

        game = self._organizer.managedGame()
        if game is not None:
            candidate = Path(game.dataDirectory().absoluteFilePath(archive_name))
            if candidate.exists():
                return candidate
        return None

    def _loose_file_size(self, origin: str, virtual_path: str) -> int:
        loose_path = self._loose_file_path(origin, virtual_path)
        if loose_path is None:
            return 0
        try:
            return loose_path.stat().st_size
        except OSError:
            return 0

    def _loose_file_path(self, origin: str, virtual_path: str) -> Optional[Path]:
        root = self._origin_root_path(origin)
        if root is None:
            return None

        parts = [part for part in normalize_asset_path(virtual_path).split("\\") if part]
        if not parts:
            return None
        return root.joinpath(*parts)

    def _origin_root_path(self, origin: str) -> Optional[Path]:
        lowered = origin.lower()
        if lowered == "overwrite":
            return Path(self._organizer.overwritePath())
        if lowered == "data":
            game = self._organizer.managedGame()
            if game is None:
                return None
            return Path(game.dataDirectory().absolutePath())

        mod = self._organizer.modList().getMod(origin)
        if mod is None:
            return None
        return Path(mod.absolutePath())

    def _display_name_for_origin(self, origin: str) -> str:
        if origin.lower() == "data":
            return "Data"
        if origin.lower() == "overwrite":
            return "Overwrite"
        display = self._organizer.modList().displayName(origin)
        return display or origin

    @staticmethod
    def _is_archive_container(path: str) -> bool:
        return path.lower().endswith((".bsa", ".ba2"))

    @staticmethod
    def _is_mod_metadata_file(path: str) -> bool:
        return path.lower() in {
            "meta.ini",
            "desktop.ini",
        }

    def _tr(self, text: str) -> str:
        return QCoreApplication.translate("BsaConflictViewerPlugin", text)


def createPlugin() -> mobase.IPlugin:
    return BsaConflictViewerPlugin()
