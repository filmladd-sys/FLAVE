# global_settings.py
# Global Settings window for Ladd Video Editor, and the global settings
# store (globalsettings.db) that every module uses.
# ASCII only.

import os
import sqlite3
import threading
from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget, QPushButton,
    QLineEdit, QCheckBox, QApplication, QComboBox
)
from PySide6.QtCore import Qt


# ---------------------------------------------------------- settings DB
# The one global settings store of the app: a SQLite key-value table
# (settings: key TEXT PRIMARY KEY, value TEXT) in globalsettings.db next
# to the code files. Every module reads and writes it through
# _get_global_setting / _set_global_setting here.
# On the first use in a process the file is created if missing and every
# key in GLOBAL_DEFAULTS that has no row yet gets its default (INSERT OR
# IGNORE: existing values are never changed). The defaults are the same
# fallback values the getters across the app use, so a fresh file gives
# the same behavior as no row at all. Keys that only hold state (current
# project, window geometry, open windows, ...) are not seeded: an absent
# row reads as the caller's default ("").

GLOBAL_DEFAULTS = (
    ("dark_mode", "0"),
    ("FootageManager_IconMaxWidthPx", "64"),
    ("FootageManager_IconMaxHeightPx", "64"),
    ("playercontrols_alignment", "left"),
    ("SequencePlayer_TicCenterLengthPixels", "10"),
    ("SequencePlayer_TicsOuterLengthPixels", "6"),
    ("previewJPEG_quality", "90"),
    ("previewFrameCap", "120"),
    ("UndoStackMax", "50"),
    ("Pixel Snap-To", "2"),
    ("previewBatchSeconds", "1"),
    ("previewGpuBatchFrames", "8"),
    ("previewBurstFrames", "15"),
    ("previewIdleMs", "250"),
)

_DB_LOCK = threading.Lock()
_DB_READY = [None]      # path checked (schema + defaults) in this process


def _global_db_path():
    # globalsettings.db in the folder that holds the code files
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "globalsettings.db")


def _ensure_global_db():
    # Create the file / settings table if missing and add any missing
    # default (first call per process, or again if the file was removed).
    # Returns the path.
    path = _global_db_path()
    if _DB_READY[0] == path and os.path.isfile(path):
        return path
    with _DB_LOCK:
        conn = sqlite3.connect(path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.executemany("INSERT OR IGNORE INTO settings (key, value) "
                             "VALUES (?, ?)", GLOBAL_DEFAULTS)
            conn.commit()
        finally:
            conn.close()
        _DB_READY[0] = path
    return path


def _set_global_setting(key, value):
    _ensure_global_db()
    conn = sqlite3.connect(_global_db_path())
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     (key, value))
        conn.commit()
    finally:
        conn.close()


def _get_global_setting(key, default=None):
    # Stored value, or default when the key has no row
    _ensure_global_db()
    conn = sqlite3.connect(_global_db_path())
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    return row[0] if row else default


def apply_dark_mode(enabled):
    app = QApplication.instance()
    if app is None:
        return
    if enabled:
        style = """
            QMainWindow, QWidget, QDialog {
                background-color: black;
                color: rgb(250, 250, 250);
            }
            QMenuBar {
                background-color: rgb(120, 120, 120);
                color: rgb(250, 250, 250);
            }
            QMenuBar::item {
                background-color: rgb(140, 140, 140);
                color: rgb(250, 250, 250);
            }
            QMenuBar::item:selected {
                background-color: rgb(160, 160, 160);
            }
            QMenu {
                background-color: rgb(140, 140, 140);
                color: rgb(250, 250, 250);
            }
            QMenu::item:selected {
                background-color: rgb(160, 160, 160);
            }
            QLineEdit, QTextEdit, QPlainTextEdit {
                background-color: rgb(30, 30, 30);
                color: rgb(250, 250, 250);
                border: 1px solid rgb(80, 80, 80);
            }
            QPushButton {
                background-color: rgb(60, 60, 60);
                color: rgb(250, 250, 250);
                border: 1px solid rgb(100, 100, 100);
                padding: 4px 12px;
            }
            QPushButton:hover {
                background-color: rgb(80, 80, 80);
            }
            QCheckBox {
                color: rgb(250, 250, 250);
            }
            QLabel {
                color: rgb(250, 250, 250);
            }
            QStatusBar {
                background-color: rgb(40, 40, 40);
                color: rgb(250, 250, 250);
            }
            QHeaderView::section {
                background-color: rgb(60, 60, 60);
                color: rgb(250, 250, 250);
                border: 1px solid rgb(80, 80, 80);
                padding: 4px;
            }
            QTableWidget {
                background-color: black;
                color: rgb(250, 250, 250);
                gridline-color: rgb(60, 60, 60);
            }
            QTableWidget::item {
                background-color: black;
                color: rgb(250, 250, 250);
            }
            QTableWidget::item:selected {
                background-color: rgb(50, 50, 50);
            }
        """
        app.setStyleSheet(style)
    else:
        app.setStyleSheet("")
    # Windows title bars follow the theme (no-op elsewhere)
    try:
        from dark_titlebar import set_dark_titlebars
        set_dark_titlebars(bool(enabled))
    except Exception:
        pass


class GlobalSettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Settings")
        self.resize(520, 220)

        central = QWidget()
        layout = QVBoxLayout(central)

        folder_label = QLabel("Current Project Folder:")
        layout.addWidget(folder_label)

        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("C:\\Videos\\Projects")
        layout.addWidget(self.folder_edit)

        self.dark_check = QCheckBox("Dark Mode")
        self.dark_check.stateChanged.connect(self._on_dark_changed)
        layout.addWidget(self.dark_check)

        icon_w_label = QLabel("FootageManager_IconMaxWidthPx:")
        layout.addWidget(icon_w_label)
        self.icon_w_edit = QLineEdit()
        self.icon_w_edit.setPlaceholderText("64")
        layout.addWidget(self.icon_w_edit)

        icon_h_label = QLabel("FootageManager_IconMaxHeightPx:")
        layout.addWidget(icon_h_label)
        self.icon_h_edit = QLineEdit()
        self.icon_h_edit.setPlaceholderText("64")
        layout.addWidget(self.icon_h_edit)

        align_label = QLabel("Player controls alignment:")
        layout.addWidget(align_label)
        self.align_combo = QComboBox()
        self.align_combo.addItem("Left", "left")
        self.align_combo.addItem("Center", "center")
        self.align_combo.addItem("Right", "right")
        layout.addWidget(self.align_combo)

        tic_c_label = QLabel("Playhead center tick height (px)")
        layout.addWidget(tic_c_label)
        self.tic_center_edit = QLineEdit()
        self.tic_center_edit.setPlaceholderText("10")
        layout.addWidget(self.tic_center_edit)

        tic_o_label = QLabel("Playhead outer tick height (px)")
        layout.addWidget(tic_o_label)
        self.tic_outer_edit = QLineEdit()
        self.tic_outer_edit.setPlaceholderText("6")
        layout.addWidget(self.tic_outer_edit)

        jpeg_q_label = QLabel("Preview JPEG quality")
        layout.addWidget(jpeg_q_label)
        self.jpeg_q_edit = QLineEdit()
        self.jpeg_q_edit.setPlaceholderText("90")
        layout.addWidget(self.jpeg_q_edit)

        # Preview burst / idle / batch seconds / GPU batch frames
        # fields removed (their code defaults apply); Preview Frame Cap: the
        # largest N of the explicit preview refresh (F5 / Play on dirty)
        frame_cap_label = QLabel("Preview Frame Cap")
        layout.addWidget(frame_cap_label)
        self.frame_cap_edit = QLineEdit()
        self.frame_cap_edit.setPlaceholderText("120")
        layout.addWidget(self.frame_cap_edit)

        undo_max_label = QLabel("Undo stack max")
        layout.addWidget(undo_max_label)
        self.undo_max_edit = QLineEdit()
        self.undo_max_edit.setPlaceholderText("50")
        layout.addWidget(self.undo_max_edit)

        snap_label = QLabel("Pixel Snap-To")
        layout.addWidget(snap_label)
        self.snap_px_edit = QLineEdit()
        self.snap_px_edit.setPlaceholderText("2")
        layout.addWidget(self.snap_px_edit)

        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply)
        layout.addWidget(apply_btn)

        self.setCentralWidget(central)
        self._load()

    def _load(self):
        folder = _get_global_setting("last_project_folder", "")
        self.folder_edit.setText(folder or "")

        dark = _get_global_setting("dark_mode", "0")
        self.dark_check.setChecked(dark == "1")

        icon_w = _get_global_setting("FootageManager_IconMaxWidthPx", "64")
        self.icon_w_edit.setText(icon_w or "64")

        icon_h = _get_global_setting("FootageManager_IconMaxHeightPx", "64")
        self.icon_h_edit.setText(icon_h or "64")

        tic_c = _get_global_setting("SequencePlayer_TicCenterLengthPixels", "10")
        self.tic_center_edit.setText(tic_c or "10")
        tic_o = _get_global_setting("SequencePlayer_TicsOuterLengthPixels", "6")
        self.tic_outer_edit.setText(tic_o or "6")

        jq = _get_global_setting("previewJPEG_quality", "90")
        self.jpeg_q_edit.setText(jq or "90")
        self.frame_cap_edit.setText(_get_global_setting("previewFrameCap", "120") or "120")
        self.undo_max_edit.setText(_get_global_setting("UndoStackMax", "50") or "50")
        self.snap_px_edit.setText(_get_global_setting("Pixel Snap-To", "2") or "2")

        align = _get_global_setting("playercontrols_alignment", "left")
        idx = self.align_combo.findData(align or "left")
        self.align_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_dark_changed(self, state):
        enabled = self.dark_check.isChecked()
        apply_dark_mode(enabled)

    def _apply(self):
        folder = self.folder_edit.text().strip()
        _set_global_setting("last_project_folder", folder)

        dark = "1" if self.dark_check.isChecked() else "0"
        _set_global_setting("dark_mode", dark)

        try:
            iw = int(self.icon_w_edit.text().strip() or "64")
        except ValueError:
            iw = 64
        try:
            ih = int(self.icon_h_edit.text().strip() or "64")
        except ValueError:
            ih = 64
        _set_global_setting("FootageManager_IconMaxWidthPx", str(iw))
        _set_global_setting("FootageManager_IconMaxHeightPx", str(ih))

        try:
            tc = int(self.tic_center_edit.text().strip() or "10")
        except ValueError:
            tc = 10
        try:
            to = int(self.tic_outer_edit.text().strip() or "6")
        except ValueError:
            to = 6
        _set_global_setting("SequencePlayer_TicCenterLengthPixels", str(tc))
        _set_global_setting("SequencePlayer_TicsOuterLengthPixels", str(to))

        try:
            jq = int(self.jpeg_q_edit.text().strip() or "90")
        except ValueError:
            jq = 90
        jq = max(1, min(100, jq))
        _set_global_setting("previewJPEG_quality", str(jq))

        for edit, key, default, lo, hi in (
                (self.frame_cap_edit, "previewFrameCap", 120, 1, 10000),
                (self.undo_max_edit, "UndoStackMax", 50, 1, 1000000000),
                (self.snap_px_edit, "Pixel Snap-To", 2, 0, 1000)):
            try:
                v = int(edit.text().strip() or str(default))
            except ValueError:
                v = default
            _set_global_setting(key, str(max(lo, min(hi, v))))

        _set_global_setting("playercontrols_alignment",
                            self.align_combo.currentData() or "left")

        apply_dark_mode(self.dark_check.isChecked())
        self.close()

    def closeEvent(self, event):
        event.accept()
