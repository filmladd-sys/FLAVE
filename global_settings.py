# global_settings.py
# Global Settings window for Ladd Video Editor
# Current Project Folder and Dark Mode.
# ASCII only.

import os
import sqlite3
from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget, QPushButton,
    QLineEdit, QCheckBox, QApplication, QComboBox
)
from PySide6.QtCore import Qt


def _global_db_path():
    return r"P:\globalsettings.db"  # Fixed global settings location


def _ensure_global_db():
    path = _global_db_path()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()
    return path


def _set_global_setting(key, value):
    _ensure_global_db()
    conn = sqlite3.connect(_global_db_path())
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _get_global_setting(key, default=None):
    _ensure_global_db()
    conn = sqlite3.connect(_global_db_path())
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
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
        self.folder_edit.setPlaceholderText("P:\\Projects")
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

        burst_label = QLabel("Preview burst frames")
        layout.addWidget(burst_label)
        self.burst_edit = QLineEdit()
        self.burst_edit.setPlaceholderText("15")
        layout.addWidget(self.burst_edit)

        idle_label = QLabel("Preview idle (ms)")
        layout.addWidget(idle_label)
        self.idle_edit = QLineEdit()
        self.idle_edit.setPlaceholderText("250")
        layout.addWidget(self.idle_edit)

        batch_label = QLabel("Preview batch (seconds)")
        layout.addWidget(batch_label)
        self.batch_edit = QLineEdit()
        self.batch_edit.setPlaceholderText("1")
        layout.addWidget(self.batch_edit)

        gpu_batch_label = QLabel("Preview GPU batch frames")
        layout.addWidget(gpu_batch_label)
        self.gpu_batch_edit = QLineEdit()
        self.gpu_batch_edit.setPlaceholderText("8")
        layout.addWidget(self.gpu_batch_edit)

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
        self.burst_edit.setText(_get_global_setting("previewBurstFrames", "15") or "15")
        self.idle_edit.setText(_get_global_setting("previewIdleMs", "250") or "250")
        self.batch_edit.setText(_get_global_setting("previewBatchSeconds", "1") or "1")
        self.gpu_batch_edit.setText(_get_global_setting("previewGpuBatchFrames", "8") or "8")

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
                (self.burst_edit, "previewBurstFrames", 15, 1, 300),
                (self.idle_edit, "previewIdleMs", 250, 0, 5000),
                (self.batch_edit, "previewBatchSeconds", 1, 1, 10),
                (self.gpu_batch_edit, "previewGpuBatchFrames", 8, 1, 30)):
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
