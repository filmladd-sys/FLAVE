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
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


SEQUENCE_MIME = "application/x-ladd-sequence-id"


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

    def mimeData(self, items):
        # BUILD 105: default row data (internal nesting drop) plus the
        # sequence id for a drop onto a Sequence Timeline
        mime = super().mimeData(items)
        try:
            sid = self._row_id(self.currentRow())
            if mime is not None and sid is not None:
                from PySide6.QtCore import QByteArray
                mime.setData(SEQUENCE_MIME, QByteArray(str(int(sid)).encode("ascii")))
        except Exception:
            pass
        return mime

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


CLIP_COUNT_TYPES = ("video", "image", "audio")


def sequence_stats(cur):
    # {sequence_id: {frames, video, image, audio, caption}}. frames is the
    # sequence length (latest out-point of clips, captions and keyframe
    # clips). Counts are clips per track type, captions per sequence.
    out = {}

    def get(sid):
        return out.setdefault(int(sid), {"frames": 0, "video": 0, "image": 0,
                                         "audio": 0, "caption": 0})
    # BUILD 105: sequence clips (sequence_id_ref set) count toward length
    # but not toward Video / Image / Audio; Sequences is sequence_children
    _q = ("SELECT t.sequence_id, LOWER(COALESCE(t.track_type, '')), "
          "SUM(CASE WHEN %s THEN 0 ELSE 1 END), "
          "MAX(c.start_frame + c.length_frames) FROM clips c "
          "JOIN tracks t ON t.id = c.track_id GROUP BY t.sequence_id, 2")
    rows = None
    for cond in ("c.sequence_id_ref IS NOT NULL", "0"):
        try:
            rows = cur.execute(_q % cond).fetchall()
            break
        except sqlite3.OperationalError:
            rows = None
    for sid, ttype, n, end in (rows or []):
        st = get(sid)
        if ttype in CLIP_COUNT_TYPES:
            st[ttype] += int(n or 0)
        st["frames"] = max(st["frames"], int(end or 0))
    try:
        for sid, n, end in cur.execute(
                "SELECT sequence_id, COUNT(*), MAX(start_frame + length_frames) "
                "FROM captions GROUP BY sequence_id").fetchall():
            st = get(sid)
            st["caption"] = int(n)
            st["frames"] = max(st["frames"], int(end or 0))
    except sqlite3.OperationalError:
        pass
    try:
        for sid, end in cur.execute(
                "SELECT sequence_id, MAX(out_frame) FROM keyframe_clips "
                "GROUP BY sequence_id").fetchall():
            st = get(sid)
            st["frames"] = max(st["frames"], int(end or 0))
    except sqlite3.OperationalError:
        pass
    return out


def format_duration(frames, fps):
    # HH:MM:SS:FF at the sequence fps (rounded to whole frames per second)
    try:
        rate = max(1, int(round(float(fps or 0)))) if fps else 30
    except (TypeError, ValueError):
        rate = 30
    frames = max(0, int(frames or 0))
    secs, ff = divmod(frames, rate)
    mins, ss = divmod(secs, 60)
    hh, mm = divmod(mins, 60)
    return "%02d:%02d:%02d:%02d" % (hh, mm, ss, ff)


def _retitle_windows(sequence_id, name):
    # Open timeline (and its player) titles follow a rename
    win = _open_windows.get(sequence_id) if _open_windows is not None else None
    if win is None:
        return
    try:
        win.setWindowTitle(f"Sequence Timeline: {name}: #{sequence_id}")
        player = getattr(win, "_player", None)
        if player is not None and not getattr(player, "closed", False):
            player.setTitle(f"Sequence Player: {name}: #{sequence_id}")
    except Exception:
        pass


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
        # Name (column 0) edits in place; other columns are read-only items
        self.table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        self._loading = False
        self.table.itemChanged.connect(self._name_changed)
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
            stats = sequence_stats(cur)
            conn.close()
        except Exception as e:
            self.status_bar.showMessage(f"DB error: {e}")
            return

        self._loading = True
        self.table.setRowCount(len(rows))
        for i, (seq_id, name, width, height, fps) in enumerate(rows):
            name_item = QTableWidgetItem(name or "")
            name_item.setData(Qt.UserRole, seq_id)
            name_item.setData(Qt.UserRole + 1, name or "")
            # Default flags (drag / drop for nesting) stay; Name is editable
            fps_text = f"{fps:g}" if fps is not None else ""
            st = stats.get(seq_id, {})
            values = [
                str(width) if width is not None else "",
                str(height) if height is not None else "",
                fps_text,
                format_duration(st.get("frames", 0), fps),
                str(st.get("video", 0)),
                str(st.get("image", 0)),
                str(st.get("audio", 0)),
                str(st.get("caption", 0)),
                str(child_counts.get(seq_id, 0)),  # Sequences (nested children)
            ]
            self.table.setItem(i, 0, name_item)
            for col, text in enumerate(values, start=1):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(i, col, item)
        self._loading = False
        self._apply_column_sizing()
        self.status_bar.showMessage(f"{len(rows)} sequence(s)")

    def _name_changed(self, item):
        # In-place rename of column 0: write sequences.name (undoable);
        # an empty name reverts to the old one
        if self._loading or item is None or item.column() != 0:
            return
        seq_id = item.data(Qt.UserRole)
        old = item.data(Qt.UserRole + 1) or ""
        new = (item.text() or "").strip()
        if seq_id is None or new == old:
            return
        if not new:
            self._loading = True
            item.setText(old)
            self._loading = False
            self.status_bar.showMessage("Sequence name cannot be empty", 3000)
            return
        try:
            from undo import push_undo
            push_undo(self.project_db, "Rename sequence")
        except Exception:
            pass
        try:
            conn = sqlite3.connect(self.project_db)
            conn.execute("UPDATE sequences SET name = ? WHERE id = ?", (new, int(seq_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Rename", f"Could not rename sequence: {e}")
            self._loading = True
            item.setText(old)
            self._loading = False
            return
        self._loading = True
        item.setData(Qt.UserRole + 1, new)
        if new != item.text():
            item.setText(new)
        self._loading = False
        _retitle_windows(int(seq_id), new)
        self.status_bar.showMessage(f"Renamed to {new}")

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
