# text_clip.py
# WYSIWYG Text clips (BUILD 308): one text box per clip on a 'text' track
# (WYSIWYG Text), stored in the text_clips table (new_project.TEXT_CLIPS_SQL).
# Per-clip style: font family, font size (sequence px), color '#RRGGBB';
# x / y are the box center in sequence pixels. This module holds the ids,
# the load / add / update helpers (link_id: sequence_timeline_captions.
# persist_caption_link_id) and the text image (QPainter -> RGBA
# QImage, cached). Timeline wiring: sequence_timeline_text.py. Style
# window: text_style_window.py. ASCII only.

import sqlite3

TEXT_TRACK_TYPE = "text"
TEXT_TRACK_LABEL = "WYSIWYG Text"
TEXT_TABLE = "text_clips"
# In-memory clip ids: -(TEXT_ID_BASE + text_clips.id), below the keyframe
# clip range (keyframe.KEYFRAME_ID_BASE) and the caption range (-captions.id)
TEXT_ID_BASE = 2000000000
DEFAULT_TEXT = "Text"
DEFAULT_COLOR = "#ffffff"
DEFAULT_SECONDS = 5
TEXT_STYLE_KEYS = ("text", "font_family", "font_size", "color", "x", "y")


def is_text_clip_id(clip_id):
    return clip_id is not None and int(clip_id) <= -TEXT_ID_BASE


def text_mem_id(db_id):
    return -(TEXT_ID_BASE + int(db_id))


def text_db_id(clip_id):
    return (-int(clip_id)) - TEXT_ID_BASE


def first_line(text):
    # Timeline label: the first line of the text
    return (text or "").split("\n", 1)[0]


def default_font_family():
    # The application's default (sans) font family
    try:
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.font().family()
    except Exception:
        return "Sans Serif"


def default_font_size(seq_h):
    # Sequence height / 12, in sequence px
    return max(1, int(round(int(seq_h or 1080) / 12.0)))


def load_text_clips(cur, sequence_id, clips):
    # Text clips into clips ({track_id: [entry]}): entries like captions,
    # [mem id, None, start, length, first line of text, 1, 0, 0, link_id]
    try:
        cur.execute(
            "SELECT id, track_id, start_frame, length_frames, text, link_id "
            "FROM text_clips WHERE sequence_id = ? ORDER BY start_frame, id",
            (sequence_id,))
    except sqlite3.OperationalError:
        return      # project without the table
    for tid, track_id, start, length, text, link_id in cur.fetchall():
        clips.setdefault(track_id, []).append(
            [text_mem_id(tid), None, max(0, int(start or 0)),
             max(1, int(length or 1)), first_line(text), 1, 0, 0, link_id])


def insert_text_clip(cur, sequence_id, track_id, start_frame, length_frames,
                     seq_w, seq_h, family=None):
    # New text_clips row with the defaults ('Text', default sans font,
    # size seq_h / 12, white, centered); returns its in-memory id
    cur.execute(
        "INSERT INTO text_clips (sequence_id, track_id, start_frame, "
        "length_frames, text, font_family, font_size, color, x, y) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(sequence_id), int(track_id), int(start_frame),
         int(length_frames), DEFAULT_TEXT, family or default_font_family(),
         default_font_size(seq_h), DEFAULT_COLOR,
         int(seq_w) // 2, int(seq_h) // 2))
    return text_mem_id(cur.lastrowid)


def read_text_style(project_db, clip_mem_id):
    # {text, font_family, font_size, color, x, y} of one text clip, or None
    try:
        conn = sqlite3.connect(project_db)
        try:
            row = conn.execute(
                "SELECT %s FROM text_clips WHERE id = ?" % ", ".join(
                    TEXT_STYLE_KEYS), (text_db_id(clip_mem_id),)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return dict(zip(TEXT_STYLE_KEYS, row)) if row else None


def write_text_field(cur, clip_mem_id, key, value):
    # One style field of one text clip (key from TEXT_STYLE_KEYS)
    if key not in TEXT_STYLE_KEYS:
        raise ValueError("unknown text clip field: %s" % key)
    cur.execute("UPDATE text_clips SET %s = ? WHERE id = ?" % key,
                (value, text_db_id(clip_mem_id)))


# ---- Text image: the clip's box drawn at its place in a sequence-size
# transparent frame (QImage ARGB32 premultiplied), the layer the
# compositor blends like an image clip. Cached per (clip, text, style,
# position, sequence size); a change of any of them makes a new entry.

_IMAGE_CACHE = {}
_IMAGE_CACHE_MAX = 64


def text_font(family, size_px):
    from PySide6.QtGui import QFont
    font = QFont(family or default_font_family())
    font.setPixelSize(max(1, int(size_px or 1)))
    return font


def text_box_rect(text, family, size_px, x, y):
    # QRect of the text box (all lines, centered on x / y) in sequence px
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtCore import QRect, Qt
    fm = QFontMetrics(text_font(family, size_px))
    r = fm.boundingRect(QRect(0, 0, 100000, 100000),
                        int(Qt.AlignHCenter | Qt.AlignTop), text or "")
    w, h = r.width(), r.height()
    return QRect(int(x) - w // 2, int(y) - h // 2, w, h)


def render_text_image(text, family, size_px, color, x, y, seq_w, seq_h,
                      clip_key=None):
    # Sequence-size RGBA image, transparent except the text (center-aligned
    # lines, box centered on x / y). clip_key (e.g. the clip id) only
    # separates cache entries.
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import Qt
    # x / y unset: the frame center
    x = int(seq_w) // 2 if x is None else x
    y = int(seq_h) // 2 if y is None else y
    key = (clip_key, text, family, int(size_px or 1), color, int(x), int(y),
           int(seq_w), int(seq_h))
    img = _IMAGE_CACHE.get(key)
    if img is not None:
        return img
    img = QImage(int(seq_w), int(seq_h), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    if (text or "").strip():
        p = QPainter(img)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            p.setFont(text_font(family, size_px))
            c = QColor(color or DEFAULT_COLOR)
            p.setPen(c if c.isValid() else QColor(DEFAULT_COLOR))
            p.drawText(text_box_rect(text, family, size_px, x, y),
                       int(Qt.AlignHCenter | Qt.AlignTop), text)
        finally:
            p.end()
    if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
    _IMAGE_CACHE[key] = img
    return img
