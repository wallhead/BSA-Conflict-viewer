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
    QTabWidget,
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
        self._archives: List[ArchiveListing] = []
        self._mod_summaries: List[ModArchiveSummary] = []
        self._warnings: List[str] = []
        self._category_checks: Dict[str, QCheckBox] = {}
        self._reclaimable_by_mod: Dict[str, int] = {}
        self._total_reclaimable_bytes = 0
        self._visible_reclaimable_bytes = 0
        self._hidden_reclaimable_bytes = 0
        self._cleanup_dirty = True
        self._populate_timer = QTimer(self)
        self._populate_timer.setSingleShot(True)
        self._populate_timer.setInterval(300)
        self._populate_timer.timeout.connect(self.populate)

        self.setWindowTitle("CleanUp")
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

        self._tabs = QTabWidget(self)
        conflicts_page = QWidget(self)
        conflicts_layout = QVBoxLayout(conflicts_page)
        conflicts_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal, conflicts_page)
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

        self._space_summary = QLabel(self)
        self._space_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._space_summary.setToolTip(
            self._cleanup_rule_text()
        )
        details.addWidget(self._space_summary)

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

        splitter.addWidget(details_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        conflicts_layout.addWidget(splitter)
        self._tabs.addTab(conflicts_page, "Conflicts")

        self._cleanup_page = QWidget(self)
        cleanup_layout = QVBoxLayout(self._cleanup_page)
        cleanup_layout.setContentsMargins(0, 0, 0, 0)
        self._cleanup_summary = QLabel(self)
        self._cleanup_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._cleanup_summary.setToolTip(self._cleanup_rule_text())
        cleanup_layout.addWidget(self._cleanup_summary)

        cleanup_controls = QHBoxLayout()
        cleanup_controls.addWidget(QLabel("Sort:", self))
        self._cleanup_sort = QComboBox(self)
        self._cleanup_sort.addItem("Alphabetic", "alphabetic")
        self._cleanup_sort.addItem("Cleanup space", "cleanup")
        self._cleanup_sort.addItem("File quantity", "files")
        self._cleanup_sort.setCurrentIndex(1)
        self._cleanup_sort.currentIndexChanged.connect(self.populate)
        cleanup_controls.addWidget(self._cleanup_sort)

        cleanup_controls.addWidget(QLabel("Type:", self))
        self._cleanup_type = QComboBox(self)
        self._cleanup_type.addItem("All", "all")
        self._cleanup_type.addItem("BSA/BA2", "archive")
        self._cleanup_type.addItem("Loose", "loose")
        self._cleanup_type.currentIndexChanged.connect(self.populate)
        cleanup_controls.addWidget(self._cleanup_type)

        cleanup_controls.addStretch(1)
        cleanup_layout.addLayout(cleanup_controls)

        self._cleanup_tree = QTreeWidget(self)
        self._cleanup_tree.setColumnCount(6)
        self._cleanup_tree.setHeaderLabels(
            ["Mod / File", "Size", "Type", "Source", "Overwritten", "Winner"]
        )
        self._cleanup_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._cleanup_tree.setSortingEnabled(False)
        self._cleanup_tree.setUniformRowHeights(True)
        cleanup_header = self._cleanup_tree.header()
        cleanup_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 6):
            cleanup_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        cleanup_layout.addWidget(self._cleanup_tree, 1)
        self._tabs.addTab(self._cleanup_page, "Cleanup")
        self._tabs.removeTab(0)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        layout.addWidget(self._tabs, 1)

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

    def refresh(self) -> None:
        progress = QProgressDialog("Preparing BSA conflict scan...", "Cancel", 0, 0, self)
        progress.setWindowTitle("CleanUp")
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
            self._archives = result.archives
            self._warnings = result.warnings
            self._mod_summaries = self._build_mod_summaries()
            self._total_reclaimable_bytes = self._total_cleanup_bytes()
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
        self._summary.setText(
            f"{len(self._conflicts)} conflict paths, "
            f"{len(self._archives)} archives. "
            f"{len(self._warnings)} warnings."
        )
        tooltip_lines = [
            self._cleanup_rule_text(),
            "Cleanup counts losing providers before the final winner.",
        ]
        tooltip_lines.extend(self._warnings)
        self._summary.setToolTip("\n".join(tooltip_lines))
        self._populate_cleanup_page(enabled_categories, mod_filter, file_filter)
        return

        visible_mods = [
            summary
            for summary in self._mod_summaries
            if self._summary_matches_filters(
                summary, enabled_categories, mod_filter, file_filter
            )
        ]
        self._reclaimable_by_mod = self._reclaimable_bytes_by_mod(
            enabled_categories, file_filter
        )
        self._sort_mod_summaries(visible_mods, enabled_categories, file_filter)
        self._visible_reclaimable_bytes = sum(
            self._reclaimable_by_mod.get(summary.mod_name, 0)
            for summary in visible_mods
        )
        filtered_reclaimable = sum(self._reclaimable_by_mod.values())
        self._hidden_reclaimable_bytes = max(
            0, filtered_reclaimable - self._visible_reclaimable_bytes
        )
        visible_archive_files = sum(
            self._visible_file_count(summary.files, enabled_categories, file_filter)
            for summary in visible_mods
        )
        self._summary.setText(
            f"{len(visible_mods)} BSA mods, "
            f"{len(self._archives)} archives, {visible_archive_files} visible files. "
            f"Cleanup: {self._format_size(self._visible_reclaimable_bytes)} shown, "
            f"{self._format_size(self._hidden_reclaimable_bytes)} not shown, "
            f"{self._format_size(self._total_reclaimable_bytes)} total. "
            f"{len(self._warnings)} warnings."
        )
        tooltip_lines = [
            self._cleanup_rule_text(),
            "shown = cleanup from BSA mods currently listed in the left tree",
            "not shown = cleanup from enabled loose-only mods, Data, Overwrite, or filtered-out mods",
            "total = whole scan without current filters",
        ]
        tooltip_lines.extend(self._warnings)
        self._summary.setToolTip("\n".join(tooltip_lines))

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
                            f"({len(summary.archives)} BSA, {visible_file_count} files, "
                            f"free {self._format_size(self._reclaimable_by_mod.get(summary.mod_name, 0))})"
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
                    summary.archives, key=lambda archive: archive.archive_name.lower()
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
        self._cleanup_dirty = True
        if self._tabs.currentWidget() is self._cleanup_page:
            self._populate_cleanup_page(enabled_categories, mod_filter, file_filter)

    def _on_selected_mod_changed(self, *_args) -> None:
        current = self._mod_list.currentItem()
        if current is not None and current.parent() is None:
            current.setExpanded(True)
        summary, archive = self._selected_scope()
        self._populate_mod_details(summary, archive)

    def _on_mod_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.parent() is None:
            item.setExpanded(True)

    def _on_tab_changed(self, _index: int) -> None:
        if self._tabs.currentWidget() is self._cleanup_page and self._cleanup_dirty:
            self._populate_cleanup_page(
                self._enabled_categories(),
                self._filter_text(self._mod_filter),
                self._filter_text(self._file_filter),
            )

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
            summaries.append(ModArchiveSummary(mod_name, display, archives, files))

        summaries.sort(key=lambda summary: summary.display_mod_name.lower())
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
        if sort_mode == "cleanup":
            summaries.sort(
                key=lambda summary: (
                    -self._reclaimable_by_mod.get(summary.mod_name, 0),
                    summary.display_mod_name.lower(),
                )
            )
        elif sort_mode == "files":
            summaries.sort(
                key=lambda summary: (
                    -self._visible_file_count(summary.files, enabled_categories, file_filter),
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

    def _populate_cleanup_page(
        self, enabled_categories: set[str], mod_filter: str, file_filter: str
    ) -> None:
        cleanup_by_mod: Dict[str, dict] = {}
        cleanup_type = (
            self._cleanup_type.currentData()
            if hasattr(self, "_cleanup_type")
            else "all"
        )
        for conflict in self._conflicts:
            if category_for_path(conflict.path) not in enabled_categories:
                continue
            if file_filter and file_filter not in conflict.path.lower():
                continue

            for provider_index, provider in enumerate(conflict.chain[:-1]):
                if provider.size <= 0:
                    continue
                if cleanup_type != "all" and provider.kind != cleanup_type:
                    continue
                if not self._cleanup_provider_matches_mod_filter(provider, mod_filter):
                    continue

                entry = cleanup_by_mod.setdefault(
                    provider.mod_name,
                    {
                        "display": provider.display_mod_name,
                        "total": 0,
                        "rows": [],
                    },
                )
                entry["total"] += provider.size
                entry["rows"].append(
                    (
                        conflict.path,
                        provider.size,
                        provider.kind_label(),
                        provider.archive_name if provider.kind == "archive" else "(loose)",
                        len(conflict.chain) - provider_index - 1,
                        conflict.winner.label(),
                        conflict.chain_label(),
                    )
                )

        sorted_entries = self._sorted_cleanup_entries(list(cleanup_by_mod.values()))
        total_files = sum(len(entry["rows"]) for entry in sorted_entries)
        total_bytes = sum(entry["total"] for entry in sorted_entries)
        type_label = self._cleanup_type.currentText() if hasattr(self, "_cleanup_type") else "All"
        self._cleanup_summary.setText(
            f"{len(sorted_entries)} cleanup mods, {total_files} losing files, "
            f"{self._format_size(total_bytes)} shown, type: {type_label}."
        )

        self._cleanup_tree.setUpdatesEnabled(False)
        try:
            self._cleanup_tree.clear()
            for entry in sorted_entries:
                rows = sorted(entry["rows"], key=lambda row: (-row[1], row[0]))
                parent = QTreeWidgetItem(
                    [
                        f"{entry['display']} ({len(rows)} files)",
                        self._format_size(entry["total"]),
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                parent.setToolTip(0, self._cleanup_rule_text())
                parent.setExpanded(True)
                self._cleanup_tree.addTopLevelItem(parent)
                for path, size, kind, archive_name, overwritten_count, winner, chain in rows:
                    child = QTreeWidgetItem(
                        [
                            path,
                            self._format_size(size),
                            kind,
                            archive_name,
                            str(overwritten_count),
                            winner,
                        ]
                    )
                    child.setToolTip(0, chain)
                    child.setToolTip(5, chain)
                    parent.addChild(child)
        finally:
            self._cleanup_dirty = False
            self._cleanup_tree.setUpdatesEnabled(True)

    @staticmethod
    def _cleanup_provider_matches_mod_filter(provider: Provider, mod_filter: str) -> bool:
        if not mod_filter:
            return True
        searchable = " ".join(
            [
                provider.display_mod_name,
                provider.mod_name,
                provider.archive_name,
                provider.real_archive_path,
            ]
        ).lower()
        return mod_filter in searchable

    def _sorted_cleanup_entries(self, entries: List[dict]) -> List[dict]:
        sort_mode = (
            self._cleanup_sort.currentData()
            if hasattr(self, "_cleanup_sort")
            else "cleanup"
        )
        if sort_mode == "alphabetic":
            return sorted(entries, key=lambda entry: entry["display"].lower())
        if sort_mode == "files":
            return sorted(
                entries,
                key=lambda entry: (-len(entry["rows"]), entry["display"].lower()),
            )
        return sorted(
            entries,
            key=lambda entry: (-entry["total"], entry["display"].lower()),
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
            self._space_summary.setText(
                "Potential cleanup: 0 B selected, "
                f"{self._format_size(self._hidden_reclaimable_bytes)} not shown, "
                f"{self._format_size(self._total_reclaimable_bytes)} total."
            )
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
        losing_rows = []

        for conflict in self._conflicts:
            if conflict.path not in selected_files:
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
            else:
                losing_rows.append([conflict.path, conflict.winner.label()])

        no_conflict_rows = [[path] for path in sorted(selected_files - conflict_paths)]
        winning_rows.sort(key=lambda row: row[0])
        losing_rows.sort(key=lambda row: row[0])

        self._winning_header.setText(f"Winning file conflicts: {len(winning_rows)}")
        self._losing_header.setText(f"Losing file conflicts: {len(losing_rows)}")
        self._no_conflict_header.setText(
            f"Files without conflicts: {len(no_conflict_rows)}"
        )
        if archive is None:
            selected_reclaimable = self._reclaimable_by_mod.get(summary.mod_name, 0)
            selection_label = "selected"
        else:
            selected_reclaimable = self._reclaimable_bytes_for_archive(
                archive, enabled_categories, file_filter
            )
            selection_label = "selected BSA"
        self._space_summary.setText(
            "Cleanup from losing files if only winners are kept: "
            f"{self._format_size(selected_reclaimable)} {selection_label}, "
            f"{self._format_size(self._visible_reclaimable_bytes)} shown, "
            f"{self._format_size(self._hidden_reclaimable_bytes)} not shown, "
            f"{self._format_size(self._total_reclaimable_bytes)} total."
        )
        self._set_table_rows(self._winning_table, winning_rows)
        self._set_table_rows(self._losing_table, losing_rows)
        self._set_table_rows(self._no_conflict_table, no_conflict_rows)

    def _reclaimable_bytes_by_mod(
        self, enabled_categories: set[str], file_filter: str
    ) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for conflict in self._conflicts:
            if category_for_path(conflict.path) not in enabled_categories:
                continue
            if file_filter and file_filter not in conflict.path.lower():
                continue
            for provider in conflict.chain[:-1]:
                if provider.size <= 0:
                    continue
                totals[provider.mod_name] = totals.get(provider.mod_name, 0) + provider.size
        return totals

    def _total_cleanup_bytes(self) -> int:
        total = 0
        for conflict in self._conflicts:
            for provider in conflict.chain[:-1]:
                if provider.size > 0:
                    total += provider.size
        return total

    @staticmethod
    def _cleanup_rule_text() -> str:
        return (
            "Cleanup counts every losing provider before the winner in a conflict chain. "
            "Example: bsa1 -> bsa2 -> loose(win) counts bsa1 + bsa2. "
            "Example: loose1 -> bsa1 -> bsa2 -> loose2(win) counts loose1 + bsa1 + bsa2. "
            "Archive files use indexed entry sizes; loose files use their real file sizes on disk."
        )

    def _reclaimable_bytes_for_archive(
        self,
        archive: ArchiveListing,
        enabled_categories: set[str],
        file_filter: str,
    ) -> int:
        archive_files = set(archive.files)
        total = 0
        for conflict in self._conflicts:
            if conflict.path not in archive_files:
                continue
            if category_for_path(conflict.path) not in enabled_categories:
                continue
            if file_filter and file_filter not in conflict.path.lower():
                continue
            for provider in conflict.chain[:-1]:
                if (
                    provider.kind == "archive"
                    and provider.mod_name == archive.mod_name
                    and provider.archive_name == archive.archive_name
                    and provider.size > 0
                ):
                    total += provider.size
        return total

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

    def _set_table_rows(self, table: QTableWidget, rows: List[List[str]]) -> None:
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        table.clearContents()
        table.setRowCount(len(rows))
        try:
            for row_index, row in enumerate(rows):
                tooltip = " | ".join(row)
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
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Cleanup Candidates", "", "CSV files (*.csv)"
        )
        if not filename:
            return

        try:
            with open(filename, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["mod_or_file", "size", "type", "source", "overwritten", "winner"])
                for index in range(self._cleanup_tree.topLevelItemCount()):
                    parent = self._cleanup_tree.topLevelItem(index)
                    writer.writerow([parent.text(0), parent.text(1), "", "", "", ""])
                    for child_index in range(parent.childCount()):
                        child = parent.child(child_index)
                        writer.writerow(
                            [
                                child.text(column)
                                for column in range(self._cleanup_tree.columnCount())
                            ]
                        )
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
        if QApplication.focusWidget() is self._cleanup_tree:
            item = self._cleanup_tree.currentItem()
            if item is not None:
                tooltip = item.toolTip(0) or item.toolTip(5) or item.text(0)
                QApplication.clipboard().setText(tooltip)
            return

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
        for table in (
            self._winning_table,
            self._losing_table,
            self._no_conflict_table,
        ):
            if focus is table or table.selectionModel().hasSelection():
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
        return "CleanUp"

    def localizedName(self) -> str:
        return self._tr("CleanUp")

    def displayName(self) -> str:
        return self.localizedName()

    def author(self) -> str:
        return "MO2 community"

    def description(self) -> str:
        return self._tr("Shows cleanup candidates from losing loose files and archive entries.")

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(0, 1, 0, mobase.ReleaseType.PREALPHA)

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
        return self._tr("Scan active files and group cleanup candidates by mod.")

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
        if not canceled:
            if progress and not progress("Scanning loose files...", None, None):
                canceled = True
            else:
                canceled = not self._collect_loose_files(providers, progress)

        if progress:
            progress("Building conflict chains...", None, None)
        conflicts: List[Conflict] = []
        for path, chain in providers.items():
            if len(chain) < 2:
                continue

            chain = sorted(
                chain,
                key=lambda provider: (0 if provider.kind == "archive" else 1, provider.order),
            )
            conflicts.append(Conflict(path, chain))

        conflicts.sort(key=lambda conflict: conflict.path)
        if canceled:
            warnings.append("Scan canceled. Showing partial results.")
        return ScanResult(conflicts, archives, warnings)

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
                order=order,
            )

            archive_files = sorted(entry.path for entry in index.files)
            archive_list.append(
                ArchiveListing(
                    mod_name=provider_base.mod_name,
                    archive_name=archive_name,
                    display_mod_name=provider_base.display_mod_name,
                    real_archive_path=str(archive_path),
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
        progress: Optional[ProgressCallback] = None,
    ) -> bool:
        specs = self._loose_origin_specs()
        for index, (origin, root, order) in enumerate(specs):
            if progress and not progress(
                f"Scanning loose files {index + 1}/{len(specs)}: {origin}",
                index,
                len(specs),
            ):
                return False
            self._collect_loose_files_from_origin(origin, root, order, providers)
        return True

    def _loose_origin_specs(self) -> list[tuple[str, Path, int]]:
        specs: list[tuple[str, Path, int]] = []
        seen_roots: set[str] = set()

        data_root = self._origin_root_path("data")
        if data_root is not None and data_root.exists():
            specs.append(("data", data_root, -1))
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
                    max(0, self._organizer.modList().priority(mod_name)),
                )
            )

        overwrite_root = self._origin_root_path("overwrite")
        if overwrite_root is not None and overwrite_root.exists():
            root_key = str(overwrite_root).lower()
            if root_key not in seen_roots:
                specs.append(("overwrite", overwrite_root, 2_000_000_000))

        specs.sort(key=lambda spec: spec[2])
        return specs

    def _collect_loose_files_from_origin(
        self,
        origin: str,
        root: Path,
        order: int,
        providers: Dict[str, List[Provider]],
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
