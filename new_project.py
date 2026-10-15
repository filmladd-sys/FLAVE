# new_project.py
# New Project window for Ladd Video Editor
# Single text box for full path (folder + filename). Creates the SQLite database.
# ASCII only.

import os
import sqlite3
from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget,
    QPushButton, QLineEdit, QMessageBox
)
from PySide6.QtCore import Qt, Signal


class NewProjectWindow(QMainWindow):
    project_created = Signal(str)  # emits the full path to the new .db

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.resize(520, 160)

        central = QWidget()
        layout = QVBoxLayout(central)

        label = QLabel("Full path to project file (example: P:\\Projects\\MyVideo.db):")
        layout.addWidget(label)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("P:\\Projects\\MyVideo.db")
        layout.addWidget(self.path_edit)

        create_btn = QPushButton("Create")
        create_btn.clicked.connect(self._create)
        layout.addWidget(create_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        layout.addWidget(cancel_btn)

        self.setCentralWidget(central)

    def _create(self):
        db_path = self.path_edit.text().strip()

        if not db_path:
            QMessageBox.warning(self, "New Project", "Path is required.")
            return

        if not db_path.lower().endswith(".db"):
            db_path += ".db"

        parent = os.path.dirname(db_path)
        if not parent or not os.path.isdir(parent):
            QMessageBox.warning(self, "New Project", "The folder in the path does not exist.")
            return

        if os.path.exists(db_path):
            QMessageBox.warning(self, "New Project", "That file already exists.")
            return

        try:
            self._init_db(db_path)
        except Exception as e:
            QMessageBox.critical(self, "New Project", f"Could not create project:\n{e}")
            return

        self.project_created.emit(db_path)
        self.close()

    def _init_db(self, db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                fps REAL NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sequence_children (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER NOT NULL,
                child_id INTEGER NOT NULL,
                UNIQUE(parent_id, child_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                track_type TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                solo INTEGER DEFAULT 0,
                height_px INTEGER DEFAULT 64,
                bg_color TEXT,
                language TEXT DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS footage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_path TEXT NOT NULL,
                filename TEXT,
                duration REAL,
                width INTEGER,
                height INTEGER,
                fps REAL,
                has_audio INTEGER DEFAULT 0,
                size_bytes INTEGER,
                file_type TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS icons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                footage_id INTEGER NOT NULL,
                width INTEGER,
                height INTEGER,
                is_original INTEGER DEFAULT 0,
                frame_number INTEGER DEFAULT 0,
                data BLOB
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                footage_id INTEGER NOT NULL,
                start_frame INTEGER NOT NULL,
                length_frames INTEGER NOT NULL,
                in_frame INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                solo INTEGER DEFAULT 0,
                link_id INTEGER
            )
        """)

        # Caption clips: own table, never footage rows
        cur.execute("""
            CREATE TABLE IF NOT EXISTS captions (
                id INTEGER PRIMARY KEY,
                sequence_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                start_frame INTEGER NOT NULL,
                length_frames INTEGER NOT NULL,
                text TEXT NOT NULL,
                font_family TEXT,
                font_size INTEGER,
                pos_x INTEGER,
                pos_y INTEGER,
                scale REAL
            )
        """)

        # Waveform peaks: signed 16-bit LE (min, max) pairs, mono, 100/s
        cur.execute("""
            CREATE TABLE IF NOT EXISTS peaks (
                footage_id INTEGER PRIMARY KEY,
                peaks_per_second INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                data BLOB NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def closeEvent(self, event):
        event.accept()
