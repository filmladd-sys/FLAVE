# footage_manager.py
# Footage Manager window for Ladd Video Editor
# Menus: top-level Import and Sort By.
# ASCII only.

import os
import sqlite3
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QMenuBar, QMenu, QFileDialog,
    QMessageBox, QStatusBar, QWidget, QVBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QSizePolicy
)
from PySide6.QtCore import Qt, QPoint, QMimeData
from PySide6.QtGui import QAction, QDrag, QCursor

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
VIDEO_EXTENSIONS = (".mov", ".mp4", ".mkv", ".avi", ".m4v")


def footage_is_image(project_db, footage_id):
    # True when the footage filename or path ends in an image extension
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT filename, original_path FROM footage WHERE id = ?", (footage_id,))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return False
    if not row:
        return False
    for value in row:
        if value and str(value).lower().endswith(IMAGE_EXTENSIONS):
            return True
    return False


AUDIO_EXTENSIONS = (".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg")


def footage_is_audio(project_db, footage_id):
    # True when the footage filename or path ends in an audio extension
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT filename, original_path FROM footage WHERE id = ?", (footage_id,))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return False
    if not row:
        return False
    for value in row:
        if value and str(value).lower().endswith(AUDIO_EXTENSIONS):
            return True
    return False


def footage_is_video(project_db, footage_id):
    # True when the footage filename or path ends in a video extension
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT filename, original_path FROM footage WHERE id = ?", (footage_id,))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return False
    if not row:
        return False
    for value in row:
        if value and str(value).lower().endswith(VIDEO_EXTENSIONS):
            return True
    return False


class FootageTable(QTableWidget):
    # Footage table that drags image, video and audio footage as text/plain "footage:<id>"
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.setDragEnabled(True)
        self.setDragDropMode(QTableWidget.DragOnly)

    def startDrag(self, supported_actions):
        row = self.currentRow()
        name_item = self.item(row, 1) if row >= 0 else None
        if name_item is None:
            return
        footage_id = name_item.data(Qt.UserRole)
        if footage_id is None:
            return
        db = self.manager.project_db
        if not (footage_is_image(db, footage_id) or footage_is_video(db, footage_id)
                or footage_is_audio(db, footage_id)):
            self.manager.status_bar.showMessage(
                "Only image, video or audio footage can be dragged to a track", 3000)
            return
        mime = QMimeData()
        mime.setText(f"footage:{footage_id}")
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

try:
    from footage_importer import import_footage
except ImportError:
    import_footage = None

try:
    from footage_manager_display import get_display_rows
except ImportError:
    get_display_rows = None


def _global_db_path():
    return r"P:\globalsettings.db"  # Fixed global settings location


def _get_global_setting(key, default=None):
    import sqlite3
    path = _global_db_path()
    if not os.path.exists(path):
        return default
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def _set_global_setting(key, value):
    import sqlite3
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


class FootageManager(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Footage Manager")
        self.resize(800, 500)

        self.project_db = _get_global_setting("current_project", "")
        self.selected_footage = None

        self.table = FootageTable(self)
        # Columns: 0 Icon, 1 Name, 2 #Proxies, 3 Proxy Default, 4 Type,
        # 5 Size, 6 Length
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["Icon", "Name", "#Proxies", "Proxy Default", "Type", "Size", "Length"])
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_context_menu)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setMinimumSectionSize(40)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 10, 0, 0)
        container_layout.addWidget(self.table, 1)
        self.setCentralWidget(container)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        if self.project_db:
            self.status_bar.showMessage(f"Project: {self.project_db}")
        else:
            self.status_bar.showMessage("No project")

        self._create_menus()
        self._restore_geometry()
        self.refresh()

    def _create_menus(self):
        menubar = self.menuBar()

        # Top-level Import
        import_action = QAction("Import", self)
        import_action.triggered.connect(self._import_footage)
        menubar.addAction(import_action)

        # Sort By menu
        sort_menu = menubar.addMenu("Sort By")

        sort_name = QAction("Name", self)
        sort_name.triggered.connect(self._sort_by_name)
        sort_menu.addAction(sort_name)

        sort_type = QAction("Type", self)
        sort_type.triggered.connect(self._sort_by_type)
        sort_menu.addAction(sort_type)

        sort_size = QAction("Size", self)
        sort_size.triggered.connect(self._sort_by_size)
        sort_menu.addAction(sort_size)

        sort_length = QAction("Length", self)
        sort_length.triggered.connect(self._sort_by_length)
        sort_menu.addAction(sort_length)

    def _import_footage(self):
        if not self.project_db:
            QMessageBox.warning(self, "Import", "No project open")
            return
        if import_footage is None:
            QMessageBox.warning(self, "Import", "footage_importer.py not found")
            return
        start_dir = _get_global_setting("last_project_folder", "")
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Footage",
            start_dir,
            "Media Files (*.mp4 *.mov *.mkv *.avi *.mp3 *.wav *.png *.jpg *.jpeg);;All Files (*)"
        )
        if not paths:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            imported = 0
            for path in paths:
                new_id = import_footage(self.project_db, path)
                if new_id:
                    imported += 1
            if imported:
                self.status_bar.showMessage(f"Imported {imported} file(s)")
                self.refresh()
            else:
                QMessageBox.warning(self, "Import", "Could not import files")
        finally:
            QApplication.restoreOverrideCursor()

    def refresh(self):
        self.table.setRowCount(0)
        self.project_db = _get_global_setting("current_project", "") or self.project_db

        if not self.project_db or get_display_rows is None:
            self.status_bar.showMessage("No project or display module missing")
            return

        try:
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            cur.execute("SELECT id FROM footage ORDER BY id")
            id_list = [row[0] for row in cur.fetchall()]
            conn.close()
        except Exception as e:
            self.status_bar.showMessage(f"DB error: {e}")
            return

        if not id_list:
            self.status_bar.showMessage("No footage in project")
            return

        try:
            rows = get_display_rows(self.project_db, id_list)
        except Exception as e:
            self.status_bar.showMessage(f"Display error: {e}")
            return

        self.table.setRowCount(len(rows))
        max_h = 64
        try:
            max_h = int(_get_global_setting("FootageManager_IconMaxHeightPx", "64") or "64")
        except ValueError:
            pass
        self.table.verticalHeader().setDefaultSectionSize(max(max_h + 8, 24))
        for i, row in enumerate(rows):
            items = row["items"]
            icon_widget = row.get("icon_widget")
            for col, item in enumerate(items):
                if col == 1 and "id" in row:
                    item.setData(Qt.UserRole, row["id"])
                # Display rows have no Proxy Default: columns 3+ shift by one
                self.table.setItem(i, col if col < 3 else col + 1, item)
            self.table.setItem(i, 3, self._proxy_default_item(items))
            if icon_widget is not None:
                self.table.setCellWidget(i, 0, icon_widget)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.status_bar.showMessage(f"{len(rows)} footage item(s)")

    def _proxy_default_item(self, items):
        # Video / image rows (the #Proxies cell carries the footage id):
        # 'None' or 'NN%', clickable. Audio rows: empty, inert.
        item = QTableWidgetItem("")
        item.setTextAlignment(Qt.AlignCenter)
        fid = items[2].data(Qt.UserRole + 1) if len(items) > 2 else None
        if fid is None:
            return item
        try:
            from clip_proxy import footage_proxy_percent, proxy_label
            item.setText(proxy_label(footage_proxy_percent(self.project_db, fid)))
        except Exception:
            item.setText("None")
        item.setData(Qt.UserRole + 1, fid)
        font = item.font()
        font.setUnderline(True)
        item.setFont(font)
        item.setToolTip("Choose the proxy for new clips of this footage")
        return item

    def _proxy_default_menu(self, row):
        item = self.table.item(row, 3)
        fid = item.data(Qt.UserRole + 1) if item is not None else None
        if fid is None:
            return
        from clip_proxy import (proxy_options, footage_proxy_percent,
                                build_proxy_menu, set_footage_proxy_percent)
        menu = QMenu(self)
        actions = build_proxy_menu(menu, proxy_options(self.project_db, fid),
                                   footage_proxy_percent(self.project_db, fid))
        rect = self.table.visualItemRect(item)
        chosen = menu.exec(self.table.viewport().mapToGlobal(rect.bottomLeft()))
        if chosen is None or chosen not in actions:
            return
        try:
            ok = set_footage_proxy_percent(self.project_db, fid, actions[chosen])
        except Exception as e:
            QMessageBox.warning(self, "Proxy Default", f"Could not save: {e}")
            return
        if not ok:
            QMessageBox.information(
                self, "Proxy Default",
                "Proxy Default is not available in this project because it was "
                "created with an older version.")
            return
        self.refresh()

    def _cell_clicked(self, row, col):
        # #Proxies cell (video / image only): cellClicked fires for a click
        # anywhere in the cell; audio cells carry no id and stay inert
        if col == 3:
            self._proxy_default_menu(row)
            return
        if col != 2:
            return
        item = self.table.item(row, 2)
        footage_id = item.data(Qt.UserRole + 1) if item is not None else None
        if footage_id is None:
            return
        name_item = self.table.item(row, 1)
        name = name_item.text() if name_item is not None else ""
        try:
            from proxy_manager import open_proxy_manager
            open_proxy_manager(self, self.project_db, footage_id, name,
                               on_change=self.refresh)
        except Exception as e:
            QMessageBox.warning(self, "Proxies", f"Could not open Proxies: {e}")

    def _row_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        name_item = self.table.item(row, 1)
        if name_item is None:
            return
        footage_id = name_item.data(Qt.UserRole)
        if footage_id is None:
            return

        menu = QMenu(self)
        replace_action = menu.addAction("Replace")
        remove_action = menu.addAction("Remove")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action == replace_action:
            self._replace_footage(footage_id)
        elif action == remove_action:
            self._remove_footage(footage_id)

    def _replace_footage(self, footage_id):
        QMessageBox.information(self, "Replace", f"Replace stub for id {footage_id}")

    def _remove_footage(self, footage_id):
        reply = QMessageBox.question(
            self,
            "Remove",
            f"Remove footage id {footage_id} from the project?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        try:
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(icons)")
            if cur.fetchall():
                cur.execute("DELETE FROM icons WHERE footage_id = ?", (footage_id,))
            cur.execute("DELETE FROM footage WHERE id = ?", (footage_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Remove", f"Could not remove footage: {e}")
            return
        self.status_bar.showMessage("Removed footage")
        self.refresh()

    def _sort_by_name(self):
        self.status_bar.showMessage("Sort by Name (stub)")

    def _sort_by_type(self):
        self.status_bar.showMessage("Sort by Type (stub)")

    def _sort_by_size(self):
        self.status_bar.showMessage("Sort by Size (stub)")

    def _sort_by_length(self):
        self.status_bar.showMessage("Sort by Length (stub)")

    def _restore_geometry(self):
        geo = _get_global_setting("footage_manager_geometry", "")
        if geo:
            try:
                parts = [int(p) for p in geo.split(",")]
                if len(parts) == 4:
                    self.setGeometry(parts[0], parts[1], parts[2], parts[3])
            except Exception:
                pass

    def _save_geometry(self):
        g = self.geometry()
        value = f"{g.x()},{g.y()},{g.width()},{g.height()}"
        _set_global_setting("footage_manager_geometry", value)

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
