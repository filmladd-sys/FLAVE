# new_project.py
# Project database creation for Ladd Video Editor (_init_db: every project
# table). Project Manager's File > New Project picks a folder and a name and
# calls create_project_db (BUILD 226); NewProjectWindow (one full-path text
# box) also creates through _init_db. ASCII only.

import os
import re
import sqlite3
from PySide6.QtWidgets import (
    QMainWindow, QLabel, QVBoxLayout, QWidget,
    QPushButton, QLineEdit, QMessageBox
)
from PySide6.QtCore import Qt, Signal


PROJECT_EXT = ".db"
# Icon lookups (Sequence Timeline and Footage Manager) filter on footage_id,
# frame_number, is_original and height (width too for the bin), and
# deletes on footage_id; this one index serves all of them. Created with the
# schema (_init_db).
ICONS_INDEX_NAME = "idx_icons_lookup"
ICONS_INDEX_SQL = ("CREATE INDEX IF NOT EXISTS idx_icons_lookup "
                   "ON icons (footage_id, frame_number, is_original, height)")
# BUILD 222: keys of type-border keyframe clips (one row per key: absolute
# sequence frame like keyframes.frame, border px and its RGBA color 0..255).
# BUILD 294: keys of type-move keyframe clips (absolute sequence frame, x and
# y the layer center in sequence px). BUILD 295: keys of type-crop_xy
# keyframe clips (absolute sequence frame, x and y the crop_x / crop_y
# values in source pixels). BUILD 296: keys of type-crop_edges keyframe
# clips (absolute sequence frame, top, bottom, left, right crop in source
# pixels). Other types keep their keys in
# keyframes. Run by _init_db.
KEYFRAME_BORDER_SQL = """
    CREATE TABLE IF NOT EXISTS keyframe_border (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyframe_clip_id INTEGER NOT NULL,
        frame INTEGER NOT NULL,
        px REAL NOT NULL,
        r REAL NOT NULL,
        g REAL NOT NULL,
        b REAL NOT NULL,
        a REAL NOT NULL
    )
"""
KEYFRAME_MOVE_SQL = """
    CREATE TABLE IF NOT EXISTS keyframe_move (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyframe_clip_id INTEGER NOT NULL,
        frame INTEGER NOT NULL,
        x REAL NOT NULL,
        y REAL NOT NULL
    )
"""
KEYFRAME_CROP_SQL = """
    CREATE TABLE IF NOT EXISTS keyframe_crop (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyframe_clip_id INTEGER NOT NULL,
        frame INTEGER NOT NULL,
        x REAL NOT NULL,
        y REAL NOT NULL
    )
"""
KEYFRAME_CROP_EDGES_SQL = """
    CREATE TABLE IF NOT EXISTS keyframe_crop_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyframe_clip_id INTEGER NOT NULL,
        frame INTEGER NOT NULL,
        top REAL NOT NULL,
        bottom REAL NOT NULL,
        left REAL NOT NULL,
        right REAL NOT NULL
    )
"""
# BUILD 308: WYSIWYG Text clips (track_type 'text'): one text box per clip,
# burned into the picture. start_frame / length_frames like captions;
# font_size and x / y (the box center) in sequence pixels; color '#RRGGBB'.
# BUILD 311: drop shadow: shadow_on 0 / 1, shadow_color '#RRGGBBAA',
# shadow_blur (blur radius, sequence px), shadow_angle (direction the
# shadow falls, degrees clockwise from straight up, like the Premiere /
# After Effects Drop Shadow direction: 90 = right, 135 = down-right,
# 180 = down) and shadow_offset (distance, sequence px). Run by _init_db.
TEXT_CLIPS_SQL = """
    CREATE TABLE IF NOT EXISTS text_clips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sequence_id INTEGER NOT NULL,
        track_id INTEGER NOT NULL,
        start_frame INTEGER NOT NULL,
        length_frames INTEGER NOT NULL,
        link_id INTEGER,
        speed_percent INTEGER NOT NULL DEFAULT 100,
        text TEXT NOT NULL,
        font_family TEXT,
        font_size INTEGER,
        color TEXT,
        x INTEGER,
        y INTEGER,
        shadow_on INTEGER NOT NULL DEFAULT 0,
        shadow_color TEXT DEFAULT '#000000B4',
        shadow_blur INTEGER DEFAULT 6,
        shadow_angle INTEGER DEFAULT 135,
        shadow_offset INTEGER DEFAULT 6
    )
"""
# Characters Windows does not allow in a file name, plus control characters
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = set(["CON", "PRN", "AUX", "NUL"] +
                      ["COM%d" % i for i in range(1, 10)] +
                      ["LPT%d" % i for i in range(1, 10)])


def project_name_error(name):
    # "" when name is usable as a Windows file name stem, else the reason
    name = name or ""
    if name.lower().endswith(PROJECT_EXT):
        name = name[:-len(PROJECT_EXT)]
    if not name.strip():
        return "A project name is required."
    if _BAD_NAME_CHARS.search(name):
        return 'A project name cannot contain any of: < > : " / \\ | ? *'
    if name != name.rstrip(". ") or name != name.lstrip(" "):
        return "A project name cannot start with a space or end with a space or a dot."
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        return "%s is a reserved name on Windows." % name
    return ""


def project_path_for(folder, name):
    # folder + name + ".db" (a typed ".db" is not doubled)
    if name.lower().endswith(PROJECT_EXT):
        name = name[:-len(PROJECT_EXT)]
    return os.path.join(folder, name + PROJECT_EXT)


def ensure_project_schema(db_path):
    # BUILD 320: project-load schema check, run by Project Manager when a
    # project is opened (startup autoload and File > Open) before any window
    # reads the project. Never raises.
    # TEMP MIGRATION (BUILD 320): adds sequences.preview_size to old projects. Safe to delete once all projects have been opened in BUILD 320+; new_project.py _init_db already creates it.
    try:
        conn = sqlite3.connect(db_path)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sequences)").fetchall()]
            if cols and "preview_size" not in cols:
                # Nullable, no default: NULL = Project Default (Preview Size)
                conn.execute("ALTER TABLE sequences ADD COLUMN preview_size INTEGER")
                conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    # END TEMP MIGRATION (BUILD 320)
    return None


def create_project_db(db_path):
    # Create the project database at db_path (raises on failure)
    NewProjectWindow._init_db(None, db_path)


class NewProjectWindow(QMainWindow):
    project_created = Signal(str)  # emits the full path to the new .db

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.resize(520, 160)

        central = QWidget()
        layout = QVBoxLayout(central)

        label = QLabel("Full path to project file (example: C:\\Videos\\Projects\\MyVideo.db):")
        layout.addWidget(label)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("C:\\Videos\\Projects\\MyVideo.db")
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
        # databases (_AudioTmp.db, _Undo.db, _Preview.db, global settings)
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
                fps REAL NOT NULL,
                preview_size INTEGER
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
        cur.execute(ICONS_INDEX_SQL)

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
                sequence_id_ref INTEGER,
                -- BUILD 303: clip speed percent, 1 .. 1000 (clip_speed.py)
                speed_percent INTEGER NOT NULL DEFAULT 100
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
                link_id INTEGER,
                speed_percent INTEGER NOT NULL DEFAULT 100
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
                name TEXT,
                speed_percent INTEGER NOT NULL DEFAULT 100
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

        # BUILD 222 / 294: keys of type-border and type-move keyframe clips
        # (KEYFRAME_BORDER_SQL, KEYFRAME_MOVE_SQL, KEYFRAME_CROP_SQL,
        # KEYFRAME_CROP_EDGES_SQL above)
        cur.execute(KEYFRAME_BORDER_SQL)
        cur.execute(KEYFRAME_MOVE_SQL)
        cur.execute(KEYFRAME_CROP_SQL)
        cur.execute(KEYFRAME_CROP_EDGES_SQL)
        # BUILD 308: WYSIWYG Text clips
        cur.execute(TEXT_CLIPS_SQL)

        # Proxies of footage (path to the proxy file, size percentage).
        # BUILD 119: proxy_mode 'normal' / 'deint' / 'deint2x' / 'ivtc';
        # deinterlace 1 = deinterlaced or inverse-telecined. proxy_fps /
        # proxy_frames: the proxy file's rate and frame count; source_fps:
        # the original's rate when the proxy was made. Clip edit points
        # (in_frame, out) are sequence frames, read 1:1 as footage frames
        # unless footage.fps differs from the sequence fps (then conformed
        # by time, sequence_player.clip_source_frame); a proxy whose rate
        # differs (deint2x, ivtc) is read at proxy index =
        # floor(footage index / source_fps * proxy_fps + eps).
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
