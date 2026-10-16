# Caption-only helpers for sequence_timeline.py (no window class here).
# Window methods take the SequenceTimeline instance as self.
import sqlite3
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

CAPTION_LANGUAGES = ["eng", "spa", "fra", "deu", "ita", "por", "jpn", "kor",
                     "zho", "rus", "ara", "hin"]


def ensure_captions_table(project_db):
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


def is_caption_id(clip_id):
    # Caption clips use negative in-memory ids (not keyframe negatives)
    if clip_id is None or clip_id >= 0:
        return False
    try:
        from keyframe import is_keyframe_clip_id
        if is_keyframe_clip_id(clip_id):
            return False
    except ImportError:
        pass
    return True


def clip_table_and_id(clip_id):
    # (table, row id) for caption / keyframe / clips
    try:
        from keyframe import is_keyframe_clip_id, db_id_from_mem
        if is_keyframe_clip_id(clip_id):
            return "keyframe_clips", db_id_from_mem(clip_id)
    except ImportError:
        pass
    if is_caption_id(clip_id):
        return "captions", -clip_id
    return "clips", clip_id


def load_captions(cur, sequence_id, clips):
    # Caption clips: captions table, negative in-memory ids; link_id for groups
    try:
        cur.execute(
            "SELECT id, track_id, start_frame, length_frames, text, link_id "
            "FROM captions WHERE sequence_id = ? ORDER BY start_frame, id",
            (sequence_id,)
        )
    except sqlite3.OperationalError:
        # Old scratch DB without captions.link_id: skip caption rows
        return
    for row in cur.fetchall():
        cap_id, track_id, start_frame, length_frames, text, link_id = row
        clips.setdefault(track_id, []).append(
            [-cap_id, None, max(0, int(start_frame or 0)),
             max(1, int(length_frames or 1)), text or "", 1, 0, 0, link_id]
        )


def persist_caption_link_id(cur, clip_mem_id, link_id):
    # clip_mem_id is -captions.id; None clears group membership
    cur.execute(
        "UPDATE captions SET link_id = ? WHERE id = ?",
        (link_id, -int(clip_mem_id))
    )


def paint_caption_bar(painter, clip_rect):
    painter.fillRect(clip_rect, QColor(30, 110, 110))


def caption_label(painter, label, text_rect):
    # Caption text on the bar, one line, elided to fit
    return painter.fontMetrics().elidedText(
        " ".join(label.split()), Qt.ElideRight,
        max(0, text_rect.width() - 6))


CAPTION_TYPES = ["CEA-608", "CEA-708", "mov_text", "WebVTT"]
DEFAULT_CAPTION_TYPE = "WebVTT"


class CaptionTrackDialog(QDialog):
    # BUILD 101: one popup for Name, Language and Type
    def __init__(self, parent, default_name):
        from PySide6.QtWidgets import (QFormLayout, QLineEdit, QComboBox,
                                       QDialogButtonBox)
        super().__init__(parent)
        self.setWindowTitle("+ Caption")
        form = QFormLayout(self)
        self.name_edit = QLineEdit(default_name or "")
        self.name_edit.selectAll()
        form.addRow("Track name:", self.name_edit)
        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.addItems(CAPTION_LANGUAGES)
        self.lang_combo.setCurrentIndex(0)
        form.addRow("Language (ISO 639-2):", self.lang_combo)
        self.type_combo = QComboBox()
        self.type_combo.addItems(CAPTION_TYPES)
        self.type_combo.setCurrentText(DEFAULT_CAPTION_TYPE)
        form.addRow("Type:", self.type_combo)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return ((self.name_edit.text() or "").strip(),
                (self.lang_combo.currentText() or "").strip().lower(),
                self.type_combo.currentText() or DEFAULT_CAPTION_TYPE)

    def _accept(self):
        name, lang, _t = self.values()
        if not name or not lang:
            QMessageBox.warning(self, "+ Caption", "Name and language are required")
            return
        self.accept()


def caption_type_text(language, caption_type, flags=""):
    # BUILD 101: Type column text for a caption track, three lines
    return "CC%s\n%s\n%s" % (flags, language or "",
                              caption_type or DEFAULT_CAPTION_TYPE)


def _add_caption_track(self):
    project_db = self._project_db()
    if not project_db:
        QMessageBox.warning(self, "+ Caption", "No project open")
        return
    from track_names import next_default_name, sequence_track_names
    default = next_default_name(
        "caption", sequence_track_names(project_db, self.sequence_id))
    dlg = CaptionTrackDialog(self, default)
    if dlg.exec() != QDialog.Accepted:
        return
    name, lang, ctype = dlg.values()
    if not name or not lang:
        return
    self._add_track("caption", "+ Caption", name=name, language=lang,
                    caption_type=ctype)


def _sequence_size(self):
    # (width, height) of this sequence, 1920x1080 if unknown
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute("SELECT width, height FROM sequences WHERE id = ?",
                    (self.sequence_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0] and row[1]:
            return int(row[0]), int(row[1])
    except Exception:
        pass
    return 1920, 1080

def add_caption(self, track_id, click_frame):
    # Add Caption: dialog, then a 1-second captions row at the mouse mark
    from caption_edit import CaptionEditDialog
    seq_w, seq_h = self._sequence_size()
    dlg = CaptionEditDialog(self, None, seq_w, seq_h)
    if dlg.exec() != QDialog.Accepted:
        return
    v = dlg.values()
    if v is None:
        return
    start_frame = max(0, int(click_frame))
    length_frames = max(1, int(round(self.fps)))
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        if self._range_overlaps(cur, track_id, start_frame, length_frames):
            conn.close()
            self.status_bar.showMessage(
                "Add Caption refused: overlaps a caption on this track", 3000)
            return
        cur.execute(
            "INSERT INTO captions (sequence_id, track_id, start_frame, length_frames, "
            "text, font_family, font_size, pos_x, pos_y, scale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.sequence_id, track_id, start_frame, length_frames,
             v["text"], v["font_family"], v["font_size"],
             v["pos_x"], v["pos_y"], v["scale"])
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Add Caption", f"Could not add caption: {e}")
        return
    self._after_clip_change(f"Added caption at frame {start_frame}")

def edit_caption(self, clip):
    # Edit: dialog filled from the captions row; OK saves the changes
    from caption_edit import CaptionEditDialog
    cap_id = -clip[0]
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute(
            "SELECT text, font_family, font_size, pos_x, pos_y, scale "
            "FROM captions WHERE id = ?", (cap_id,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Edit Caption", f"Could not read caption: {e}")
        return
    if row is None:
        return
    keys = ("text", "font_family", "font_size", "pos_x", "pos_y", "scale")
    seq_w, seq_h = self._sequence_size()
    dlg = CaptionEditDialog(self, dict(zip(keys, row)), seq_w, seq_h)
    if dlg.exec() != QDialog.Accepted:
        return
    v = dlg.values()
    if v is None:
        return
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute(
            "UPDATE captions SET text = ?, font_family = ?, font_size = ?, "
            "pos_x = ?, pos_y = ?, scale = ? WHERE id = ?",
            (v["text"], v["font_family"], v["font_size"],
             v["pos_x"], v["pos_y"], v["scale"], cap_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Edit Caption", f"Could not save caption: {e}")
        return
    self._after_clip_change("Caption updated")
