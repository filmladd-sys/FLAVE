# keyframe.py
# Keyframe clip tables, dialog, load/draw/persist helpers. ASCII only.

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QMessageBox,
)

# Stored on keyframe_clips.type. Not enforced beyond storing the string.
KEYFRAME_CLIP_TYPES = ("position X", "position Y", "scale")

# Combo display label -> stored type
KEYFRAME_TYPE_CHOICES = (
    ("X", "position X"),
    ("Y", "position Y"),
    ("Scale", "scale"),
)

# In-memory clip ids: -(KEYFRAME_ID_BASE + db_id) so they never collide
# with caption negatives (-captions.id) or positive clips.id.
KEYFRAME_ID_BASE = 1000000000

TITLE_BAR_H = 15


def ensure_keyframe_clips_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keyframe_clips ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sequence_id INTEGER NOT NULL, "
        "track_id INTEGER NOT NULL, "
        "type TEXT NOT NULL, "
        "link_id INTEGER, "
        "in_frame INTEGER NOT NULL, "
        "out_frame INTEGER NOT NULL, "
        "name TEXT)"
    )


def ensure_keyframes_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keyframes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "keyframe_clip_id INTEGER NOT NULL, "
        "frame INTEGER NOT NULL, "
        "value REAL NOT NULL)"
    )


def ensure_keyframe_tables(conn):
    ensure_keyframe_clips_table(conn)
    ensure_keyframes_table(conn)


def is_keyframe_clip_id(clip_id):
    return clip_id is not None and int(clip_id) <= -KEYFRAME_ID_BASE


def mem_id_from_db(db_id):
    return -(KEYFRAME_ID_BASE + int(db_id))


def db_id_from_mem(clip_id):
    return (-int(clip_id)) - KEYFRAME_ID_BASE


def type_display_label(stored_type):
    for label, stored in KEYFRAME_TYPE_CHOICES:
        if stored == stored_type:
            return label
    return stored_type or ""


def clip_title(name, stored_type):
    return "%s: %s" % ((name or ""), type_display_label(stored_type))


def load_keyframe_clips(cur, sequence_id, clips):
    # Into clips_by_track like captions: list
    # [mem_id, None, start, length, title, enabled, solo, in_src, link_id]
    try:
        cur.execute(
            "SELECT id, track_id, type, link_id, in_frame, out_frame, "
            "COALESCE(name, '') FROM keyframe_clips "
            "WHERE sequence_id = ? ORDER BY in_frame, id",
            (sequence_id,)
        )
    except sqlite3.OperationalError:
        return
    for row in cur.fetchall():
        db_id, track_id, ktype, link_id, in_f, out_f, name = row
        in_f = max(0, int(in_f or 0))
        out_f = max(in_f + 1, int(out_f or (in_f + 1)))
        length = max(1, out_f - in_f)
        clips.setdefault(track_id, []).append(
            [mem_id_from_db(db_id), None, in_f, length,
             clip_title(name, ktype), 1, 0, 0, link_id]
        )


def paint_keyframe_clip(painter, clip_rect, title):
    # Caption-like body fill, plus a 15 px title bar (black, white outline,
    # white centered Name: Type).
    body = QColor(30, 110, 110)
    painter.fillRect(clip_rect, body)
    h = min(TITLE_BAR_H, max(1, clip_rect.height()))
    title_rect = clip_rect.adjusted(0, 0, 0, -(clip_rect.height() - h))
    painter.fillRect(title_rect, QColor(0, 0, 0))
    painter.setPen(QPen(QColor(255, 255, 255)))
    painter.drawRect(title_rect.adjusted(0, 0, -1, -1))
    if title_rect.width() > 4:
        text = painter.fontMetrics().elidedText(
            " ".join((title or "").split()), Qt.ElideRight,
            max(0, title_rect.width() - 4))
        painter.drawText(title_rect, Qt.AlignCenter, text)


def keyframe_range_overlaps(cur, track_id, start, length, ignore_id=None):
    # [start, start+length) vs [in_frame, out_frame)
    end = int(start) + int(length)
    sql = (
        "SELECT 1 FROM keyframe_clips WHERE track_id = ? "
        "AND in_frame < ? AND out_frame > ?"
    )
    args = [int(track_id), end, int(start)]
    if ignore_id is not None:
        sql += " AND id != ?"
        args.append(int(ignore_id))
    sql += " LIMIT 1"
    cur.execute(sql, args)
    return cur.fetchone() is not None


def persist_keyframe_clip_time(cur, clip):
    # Memory start/length -> in_frame/out_frame
    db_id = db_id_from_mem(clip[0])
    in_f = max(0, int(clip[2]))
    length = max(1, int(clip[3]))
    cur.execute(
        "UPDATE keyframe_clips SET in_frame = ?, out_frame = ? WHERE id = ?",
        (in_f, in_f + length, db_id)
    )


def persist_keyframe_clip_track(cur, clip, track_id):
    db_id = db_id_from_mem(clip[0])
    cur.execute(
        "UPDATE keyframe_clips SET track_id = ? WHERE id = ?",
        (int(track_id), db_id)
    )


def persist_keyframe_link_id(cur, clip_mem_id, link_id):
    # Same link_id group semantics as clips.link_id (may be a clips.id or
    # another member's mem id). None clears membership.
    db_id = db_id_from_mem(clip_mem_id)
    cur.execute(
        "UPDATE keyframe_clips SET link_id = ? WHERE id = ?",
        (link_id, db_id)
    )


class AddKeyframeClipDialog(QDialog):
    # Name + type (X / Y / Scale) + Add
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Keyframe Clip")
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        row.addWidget(self.name_edit)
        layout.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        for label, _stored in KEYFRAME_TYPE_CHOICES:
            self.type_combo.addItem(label)
        row2.addWidget(self.type_combo)
        layout.addLayout(row2)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.accept)
        layout.addWidget(add_btn)

    def values(self):
        name = (self.name_edit.text() or "").strip()
        idx = self.type_combo.currentIndex()
        if idx < 0 or idx >= len(KEYFRAME_TYPE_CHOICES):
            return None
        stored = KEYFRAME_TYPE_CHOICES[idx][1]
        return name, stored


def add_keyframe_clip(timeline, track_id, click_frame):
    # Dialog then insert. out_frame = in_frame + 30 * fps.
    dlg = AddKeyframeClipDialog(timeline)
    if dlg.exec() != QDialog.Accepted:
        return
    vals = dlg.values()
    if vals is None:
        return
    name, stored_type = vals
    in_frame = max(0, int(click_frame))
    try:
        fps = float(getattr(timeline, "fps", 30) or 30)
    except Exception:
        fps = 30.0
    if fps <= 0:
        fps = 30.0
    length = max(1, int(round(30.0 * fps)))
    out_frame = in_frame + length
    project_db = timeline._project_db()
    if not project_db:
        QMessageBox.warning(timeline, "Add Keyframe Clip", "No project open")
        return
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        ensure_keyframe_tables(conn)
        if keyframe_range_overlaps(cur, track_id, in_frame, length):
            conn.close()
            timeline.status_bar.showMessage(
                "Add Keyframe Clip refused: overlaps a clip on this track", 3000)
            return
        cur.execute(
            "INSERT INTO keyframe_clips (sequence_id, track_id, type, link_id, "
            "in_frame, out_frame, name) VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (timeline.sequence_id, int(track_id), stored_type,
             in_frame, out_frame, name)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(
            timeline, "Add Keyframe Clip", "Could not add clip: %s" % e)
        return
    timeline._after_clip_change(
        "Added keyframe clip at frame %d" % in_frame)
