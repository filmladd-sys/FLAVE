# project_settings.py
# Project Settings window for Ladd Video Editor: per-project values in the
# project db settings key-value table (created by new_project.py).
# ASCII only.

import os
import sqlite3

from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget, QPushButton, QLineEdit,
    QFormLayout, QMessageBox
)

from proxy_manager import PROXY_PATH_KEY, get_proxy_path


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


class ProjectSettingsWindow(QMainWindow):
    def __init__(self, parent=None, project_db=None):
        super().__init__(parent)
        self.setWindowTitle("Project Settings")
        self.resize(500, 160)
        self.project_db = (project_db or getattr(parent, "current_project", None)
                           or _current_project_from_global())

        central = QWidget()
        layout = QVBoxLayout(central)
        form = QFormLayout()
        self.proxy_edit = QLineEdit(get_proxy_path(self.project_db))
        form.addRow(QLabel("Proxy Path:"), self.proxy_edit)
        layout.addLayout(form)
        layout.addStretch(1)

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

        self.setCentralWidget(central)

    def _save(self):
        if not self.project_db:
            QMessageBox.warning(self, "Project Settings", "No project open")
            return
        try:
            save_proxy_path(self.project_db, self.proxy_edit.text())
        except Exception as e:
            QMessageBox.warning(self, "Project Settings", "Could not save: %s" % e)
            return
        self.close()

    def closeEvent(self, event):
        # Does not exit the application
        event.accept()
