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
        # The one definition of every project-db table (BUILD 78). Side
        # databases (.audiotmp.db, .undo.db, _preview.db, global settings)
        # are created by their own modules.
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
                -- BUILD 126: 0 = open, 1 = collapsed (16 px row, bars + names)
                collapsed INTEGER NOT NULL DEFAULT 0,
                bg_color TEXT,
                language TEXT DEFAULT '',
                -- BUILD 101: caption stream type for caption tracks.
                -- Allowed (documented, not enforced): 'CEA-608', 'CEA-708',
                -- 'mov_text', 'WebVTT'. NULL is shown as WebVTT.
                caption_type TEXT DEFAULT 'WebVTT'
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
                file_type TEXT,
                proxy_percent INTEGER NOT NULL DEFAULT 0,
                codec TEXT,
                -- BUILD 119: MKV import probe (footage_importer.py):
                -- 'progressive' / 'interlaced' / 'telecine' / 'unknown';
                -- NULL = not probed (every non-MKV file)
                field_order TEXT
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
                footage_id INTEGER,
                start_frame INTEGER NOT NULL,
                length_frames INTEGER NOT NULL,
                in_frame INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                solo INTEGER DEFAULT 0,
                link_id INTEGER,
                gain_db REAL DEFAULT 0,
                proxy_percent INTEGER NOT NULL DEFAULT 0,
                -- BUILD 105: sequence clip = sequences.id of the nested child
                -- (footage_id is NULL then); NULL for footage clips
                sequence_id_ref INTEGER
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
                scale REAL,
                link_id INTEGER
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

        # Keyframe clips (position X / Y, scale, volume) and their keys
        cur.execute("""
            CREATE TABLE IF NOT EXISTS keyframe_clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                link_id INTEGER,
                in_frame INTEGER NOT NULL,
                out_frame INTEGER NOT NULL,
                name TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS keyframes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyframe_clip_id INTEGER NOT NULL,
                frame INTEGER NOT NULL,
                value REAL NOT NULL
            )
        """)

        # Proxies of footage (path to the proxy file, size percentage).
        # BUILD 119: proxy_mode 'normal' / 'deint' / 'deint2x' / 'ivtc';
        # deinterlace 1 = deinterlaced or inverse-telecined. proxy_fps /
        # proxy_frames: the proxy file's rate and frame count; source_fps:
        # the original's rate when the proxy was made. Clip edit points
        # (in_frame, out) are always SOURCE frames; a proxy whose rate
        # differs (deint2x, ivtc) is read at proxy index =
        # floor(source index / source_fps * proxy_fps + eps).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                footage_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                percentage INTEGER NOT NULL,
                deinterlace INTEGER NOT NULL DEFAULT 0,
                proxy_mode TEXT NOT NULL DEFAULT 'normal',
                proxy_fps REAL,
                proxy_frames INTEGER,
                source_fps REAL
            )
        """)

        # Clip markers: clip_id is the timeline clip id (clips.id, or
        # -captions.id for captions); frame is relative to the clip start
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Marker (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                clip_id INTEGER NOT NULL,
                frame INTEGER NOT NULL,
                r INTEGER NOT NULL DEFAULT 50,
                g INTEGER NOT NULL DEFAULT 0,
                b INTEGER NOT NULL DEFAULT 0
            )
        """)

        # ProxyPath: folder for proxy files, default the project folder
        cur.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("ProxyPath", os.path.dirname(os.path.abspath(db_path)))
        )

        conn.commit()
        conn.close()

        # Sibling mono PCM cache next to the project file
        try:
            from peaks import ensure_audiotmp_db
            ensure_audiotmp_db(db_path)
        except Exception:
            pass

    def closeEvent(self, event):
        event.accept()
