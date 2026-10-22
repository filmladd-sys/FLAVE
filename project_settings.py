# project_settings.py
# Project Settings window for Ladd Video Editor: per-project values in the
# project db settings key-value table (created by new_project.py).
# ASCII only.

import os
import sqlite3

from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget, QPushButton, QLineEdit,
    QFormLayout, QMessageBox, QHBoxLayout, QFileDialog, QComboBox
)
from PySide6.QtCore import QDir

from proxy_manager import PROXY_PATH_KEY, get_proxy_path
from sequence_preview import (PREVIEW_SIZE_CHOICES, preview_size_percent,
                              set_preview_size_percent)


# Global settings store (globalsettings.db): owned by global_settings.py
from global_settings import _get_global_setting


def _current_project_from_global():
    # current_project from the global settings, "" if unset or unreadable
    try:
        return _get_global_setting("current_project", "") or ""
    except Exception:
        return ""


def save_proxy_path(project_db, value):
    conn = sqlite3.connect(project_db)
    try:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     (PROXY_PATH_KEY, value))
        conn.commit()
    finally:
        conn.close()


def _wake_preview_worker():
    # A new Preview Size: restart the preview worker's pass (if it has a
    # timeline) so it marks the stored frames of the other size dirty and
    # builds at the new size. Not started here if it is not running yet.
    try:
        import sequence_preview_worker
        w = sequence_preview_worker._worker
        if w is not None:
            w.wake()
    except Exception as e:
        print("Preview worker wake failed: %s" % e)


class ProjectSettingsWindow(QMainWindow):
    def __init__(self, parent=None, project_db=None):
        super().__init__(parent)
        self.setWindowTitle("Project Settings")
        self.resize(500, 190)
        self.project_db = (project_db or getattr(parent, "current_project", None)
                           or _current_project_from_global())

        central = QWidget()
        layout = QVBoxLayout(central)
        form = QFormLayout()
        self.proxy_edit = QLineEdit(get_proxy_path(self.project_db))
        # Browse... beside the field: folder picker that fills the text box
        # (saved with Save like a typed path)
        self.proxy_browse_btn = QPushButton("Browse...")
        self.proxy_browse_btn.clicked.connect(self._browse_proxy_path)
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(self.proxy_edit, 1)
        proxy_row.addWidget(self.proxy_browse_btn)
        form.addRow(QLabel("Proxy Path:"), proxy_row)
        # Preview Size: percent of the sequence size the preview frames are
        # stored at (smaller = less disk and faster preview builds; the
        # Sequence Player scales them up to fill its view)
        self.preview_size_combo = QComboBox()
        for pct in PREVIEW_SIZE_CHOICES:
            self.preview_size_combo.addItem("%d%%" % pct, pct)
        self._preview_pct = preview_size_percent(self.project_db)
        idx = self.preview_size_combo.findData(self._preview_pct)
        self.preview_size_combo.setCurrentIndex(max(0, idx))
        self.preview_size_combo.setToolTip(
            "Size of the stored preview frames, as a percent of the sequence "
            "size.\nChanging it marks the stored preview frames dirty; they "
            "are rebuilt at the new size (F5 / Refresh Preview).")
        form.addRow(QLabel("Preview Size:"), self.preview_size_combo)
        layout.addLayout(form)
        layout.addStretch(1)

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

        self.setCentralWidget(central)

    def _browse_proxy_path(self):
        # Starts at the field's folder if it exists, else the project folder;
        # a picked folder replaces the text (native separators), Cancel
        # leaves it
        start = self.proxy_edit.text().strip()
        if not start or not os.path.isdir(start):
            start = (os.path.dirname(os.path.abspath(self.project_db))
                     if self.project_db else "")
        folder = QFileDialog.getExistingDirectory(self, "Proxy Path", start)
        if folder:
            self.proxy_edit.setText(QDir.toNativeSeparators(folder))

    def _save(self):
        if not self.project_db:
            QMessageBox.warning(self, "Project Settings", "No project open")
            return
        try:
            save_proxy_path(self.project_db, self.proxy_edit.text())
            pct = int(self.preview_size_combo.currentData() or 100)
            if pct != self._preview_pct:
                set_preview_size_percent(self.project_db, pct)
                self._preview_pct = pct
                _wake_preview_worker()
        except Exception as e:
            QMessageBox.warning(self, "Project Settings", "Could not save: %s" % e)
            return
        self.close()

    def closeEvent(self, event):
        # Does not exit the application
        event.accept()
