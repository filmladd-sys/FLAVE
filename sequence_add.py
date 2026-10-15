# sequence_add.py
# Add Sequence dialog for Ladd Video Editor
# Inserts a new row into the current project's sequences table.
# ASCII only.

import os
import sqlite3
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit,
    QPushButton, QHBoxLayout, QVBoxLayout, QMessageBox
)


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


def ensure_sequences_table(project_db):
    conn = sqlite3.connect(project_db)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sequences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            fps REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


class SequenceAddDialog(QDialog):
    def __init__(self, parent=None, project_db=None, sequence_id=None):
        super().__init__(parent)
        self.sequence_id = sequence_id
        self.edit_mode = sequence_id is not None
        self.setWindowTitle("Sequence Settings" if self.edit_mode else "Add Sequence")
        self.project_db = project_db or _get_global_setting("current_project", "")
        self.new_id = None

        self.name_edit = QLineEdit()

        self.width_edit = QLineEdit("1920")
        self.height_edit = QLineEdit("1080")
        self.fps_edit = QLineEdit("29.97")

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Width", self.width_edit)
        form.addRow("Height", self.height_edit)
        form.addRow("FPS", self.fps_edit)

        create_btn = QPushButton("Save" if self.edit_mode else "Create")
        create_btn.setDefault(True)
        create_btn.clicked.connect(self._create)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(create_btn)
        buttons.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)

    def load(self):
        # Fill fields from the existing row. Returns False if not found.
        conn = sqlite3.connect(self.project_db)
        cur = conn.cursor()
        cur.execute(
            "SELECT name, width, height, fps FROM sequences WHERE id = ?",
            (self.sequence_id,)
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return False
        name, width, height, fps = row
        self.name_edit.setText(name or "")
        self.width_edit.setText(str(width))
        self.height_edit.setText(str(height))
        self.fps_edit.setText(f"{fps:g}")
        return True

    def _create(self):
        name = self.name_edit.text().strip()
        if not self.project_db:
            QMessageBox.warning(self, "Add Sequence", "No project open")
            return
        if not name:
            QMessageBox.warning(self, "Add Sequence", "Name is required")
            return
        try:
            width = int(self.width_edit.text().strip())
            height = int(self.height_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Add Sequence", "Width and height must be whole numbers")
            return
        try:
            fps = float(self.fps_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Add Sequence", "FPS must be a number")
            return
        if width <= 0 or height <= 0:
            QMessageBox.warning(self, "Add Sequence", "Width and height must be greater than 0")
            return
        if not (fps > 0):
            QMessageBox.warning(self, "Add Sequence", "FPS must be greater than 0")
            return

        try:
            ensure_sequences_table(self.project_db)
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            if self.edit_mode:
                cur.execute(
                    "UPDATE sequences SET name = ?, width = ?, height = ?, fps = ? WHERE id = ?",
                    (name, width, height, fps, self.sequence_id)
                )
                self.new_id = self.sequence_id
            else:
                cur.execute(
                    "INSERT INTO sequences (name, width, height, fps) VALUES (?, ?, ?, ?)",
                    (name, width, height, fps)
                )
                self.new_id = cur.lastrowid
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, self.windowTitle(), f"Could not save sequence: {e}")
            return

        self.accept()


def add_sequence(parent=None, project_db=None):
    # Returns the new sequence id, or None if cancelled.
    dlg = SequenceAddDialog(parent, project_db)
    if dlg.exec() == QDialog.Accepted:
        return dlg.new_id
    return None


def edit_sequence(parent=None, project_db=None, sequence_id=None):
    # Returns the sequence id if saved, or None if cancelled or not found.
    dlg = SequenceAddDialog(parent, project_db, sequence_id)
    try:
        found = dlg.load()
    except Exception as e:
        QMessageBox.warning(parent, "Sequence Settings", f"Could not load sequence: {e}")
        return None
    if not found:
        QMessageBox.warning(parent, "Sequence Settings", "Sequence not found")
        return None
    if dlg.exec() == QDialog.Accepted:
        return dlg.new_id
    return None
