# sequence_manager.py
# Sequence Manager window for Ladd Video Editor
# Menu: Sequence (Add New Sequence).
# ASCII only.

import os
import sqlite3
from PySide6.QtWidgets import (
    QMainWindow, QMessageBox, QStatusBar, QWidget, QVBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy, QMenu
)
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtGui import QAction

try:
    from sequence_add import add_sequence, edit_sequence, ensure_sequences_table
except ImportError:
    add_sequence = None
    edit_sequence = None
    ensure_sequences_table = None

try:
    from sequence_timeline import SequenceTimeline, open_timeline, _open_windows
except ImportError:
    SequenceTimeline = None
    open_timeline = None
    _open_windows = {}


def _global_db_path():
    return r"P:\globalsettings.db"  # Fixed global settings location


def _get_global_setting(key, default=None):
    path = _global_db_path()
    if not os.path.exists(path):
        return default
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return row[0] if row else default


def _set_global_setting(key, value):
    path = _global_db_path()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _ensure_children_table(project_db):
    conn = sqlite3.connect(project_db)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sequence_children (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER NOT NULL,
            child_id INTEGER NOT NULL,
            UNIQUE(parent_id, child_id)
        )
    """)
    conn.commit()
    conn.close()


class SequenceTable(QTableWidget):
    # Drop a row onto another row to nest it. Never reorders rows.
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropOverwriteMode(False)

    def _row_id(self, row):
        if row < 0:
            return None
        item = self.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        # Custom handler: read source and target ids, insert, never move rows
        if event.source() is not self:
            event.ignore()
            return
        source_id = self._row_id(self.currentRow())
        target_row = self.rowAt(event.position().toPoint().y())
        target_id = self._row_id(target_row)
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        if source_id is None or target_id is None:
            return
        if source_id == target_id:
            self.manager.status_bar.showMessage("Cannot nest a sequence in itself")
            return
        self.manager.nest_sequence(target_id, source_id)


class SequenceManager(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sequence Manager")
        self.resize(600, 400)

        self.project_db = _get_global_setting("current_project", "")
        self._geometry_ready = False
        # sequence_id -> SequenceTimeline window (shared with Project Manager)
        self.timeline_windows = _open_windows

        self.table = SequenceTable(self)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_context_menu)
        headers = ["Name", "Width", "Height", "FPS", "Duration",
                   "Video", "Image", "Audio", "Caption", "Sequences"]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._apply_column_sizing()

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 10, 0, 0)
        container_layout.addWidget(self.table, 1)
        self.setCentralWidget(container)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self._create_menus()
        self._restore_geometry()
        self._geometry_ready = True
        self.refresh()

    def _create_menus(self):
        menubar = self.menuBar()
        sequence_menu = menubar.addMenu("Sequence")

        self.add_action = QAction("Add New Sequence", self)
        self.add_action.triggered.connect(self._add_sequence)
        sequence_menu.addAction(self.add_action)

    def _add_sequence(self):
        if not self.project_db:
            QMessageBox.warning(self, "Add Sequence", "No project open")
            return
        if add_sequence is None:
            QMessageBox.warning(self, "Add Sequence", "sequence_add.py not found")
            return
        new_id = add_sequence(self, self.project_db)
        if new_id:
            self.refresh()
            self.status_bar.showMessage("Sequence added")

    def refresh(self):
        self.table.setRowCount(0)
        self.project_db = _get_global_setting("current_project", "") or self.project_db

        if not self.project_db:
            self.add_action.setEnabled(False)
            self.status_bar.showMessage("No project open")
            return
        self.add_action.setEnabled(add_sequence is not None)

        try:
            if ensure_sequences_table is not None:
                ensure_sequences_table(self.project_db)
            _ensure_children_table(self.project_db)
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            cur.execute("SELECT id, name, width, height, fps FROM sequences ORDER BY id")
            rows = cur.fetchall()
            cur.execute("""
                SELECT parent_id, COUNT(*) FROM sequence_children
                GROUP BY parent_id
            """)
            child_counts = dict(cur.fetchall())
            conn.close()
        except Exception as e:
            self.status_bar.showMessage(f"DB error: {e}")
            return

        self.table.setRowCount(len(rows))
        for i, (seq_id, name, width, height, fps) in enumerate(rows):
            name_item = QTableWidgetItem(name or "")
            name_item.setData(Qt.UserRole, seq_id)
            fps_text = f"{fps:g}" if fps is not None else ""
            values = [
                str(width) if width is not None else "",
                str(height) if height is not None else "",
                fps_text,
                "0",  # Duration
                "0",  # Video
                "0",  # Image
                "0",  # Audio
                "0",  # Caption
                str(child_counts.get(seq_id, 0)),  # Sequences
            ]
            self.table.setItem(i, 0, name_item)
            for col, text in enumerate(values, start=1):
                self.table.setItem(i, col, QTableWidgetItem(text))
        self._apply_column_sizing()
        self.status_bar.showMessage(f"{len(rows)} sequence(s)")

    def _row_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if item is None:
            return
        name_item = self.table.item(item.row(), 0)
        if name_item is None:
            return
        sequence_id = name_item.data(Qt.UserRole)
        if sequence_id is None:
            return

        menu = QMenu(self)
        edit_action = menu.addAction("Edit")
        settings_action = menu.addAction("Settings")
        replace_action = menu.addAction("Replace")
        remove_action = menu.addAction("Remove")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == edit_action:
            self._edit_sequence(sequence_id)
        elif action == settings_action:
            self._sequence_settings(sequence_id)
        elif action == replace_action:
            self._replace_sequence(sequence_id)
        elif action == remove_action:
            self._remove_sequence(sequence_id)

    def _edit_sequence(self, sequence_id):
        if SequenceTimeline is None:
            QMessageBox.warning(self, "Edit", "sequence_timeline.py not found")
            return
        open_timeline(self, sequence_id)

    def _sequence_settings(self, sequence_id):
        if edit_sequence is None:
            QMessageBox.warning(self, "Settings", "sequence_add.py not found")
            return
        if edit_sequence(self, self.project_db, sequence_id):
            self.refresh()
            self.status_bar.showMessage("Sequence updated")

    def _replace_sequence(self, sequence_id):
        QMessageBox.information(self, "Replace", f"Replace stub for sequence id {sequence_id}")

    def _remove_sequence(self, sequence_id):
        reply = QMessageBox.question(
            self,
            "Remove",
            f"Remove sequence id {sequence_id} from the project?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        try:
            _ensure_children_table(self.project_db)
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM sequence_children WHERE parent_id = ? OR child_id = ?",
                (sequence_id, sequence_id)
            )
            cur.execute("DELETE FROM sequences WHERE id = ?", (sequence_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Remove", f"Could not remove sequence: {e}")
            return
        self.refresh()
        self.status_bar.showMessage("Removed sequence")

    def _apply_column_sizing(self):
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.Stretch)

    def nest_sequence(self, parent_id, child_id):
        if parent_id == child_id:
            return
        try:
            _ensure_children_table(self.project_db)
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO sequence_children (parent_id, child_id) VALUES (?, ?)",
                (parent_id, child_id)
            )
            added = cur.rowcount
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Nest Sequence", f"Could not nest sequence: {e}")
            return
        self.refresh()
        if added:
            self.status_bar.showMessage("Sequence nested")
        else:
            self.status_bar.showMessage("Already nested")

    def _restore_geometry(self):
        try:
            x = int(_get_global_setting("SequenceManager_x", "") or "")
            y = int(_get_global_setting("SequenceManager_y", "") or "")
            w = int(_get_global_setting("SequenceManager_w", "") or "")
            h = int(_get_global_setting("SequenceManager_h", "") or "")
        except ValueError:
            return
        if w > 0 and h > 0:
            self.setGeometry(x, y, w, h)

    def _save_geometry(self):
        if not self._geometry_ready:
            return
        g = self.geometry()
        _set_global_setting("SequenceManager_x", str(g.x()))
        _set_global_setting("SequenceManager_y", str(g.y()))
        _set_global_setting("SequenceManager_w", str(g.width()))
        _set_global_setting("SequenceManager_h", str(g.height()))

    def moveEvent(self, event):
        super().moveEvent(event)
        self._save_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._save_geometry()

    def closeEvent(self, event):
        self._save_geometry()
        # Does not exit the application
        event.accept()
